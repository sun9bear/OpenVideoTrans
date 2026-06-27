"""media-worker: run the REAL autodub-core pipeline with FREE-POOL routing (M2-CLOSE #26).

Replaces the T2.2 stub copy-loop. Two responsibilities:

  * ``inject_provider_env`` — fold the SECRETS-pulled free-provider keys into the environment
    so the env-keyed provider-adapters ``select()`` resolves them. The keys live in-process
    env ONLY (SECRETS: never on the box disk).

  * ``run_real_pipeline`` — route each needed ASR/MT/TTS stage through the FREE-POOL circuit-breaker
    (``route_free`` against the control plane's shared availability snapshot), override ``job.plan``
    with the routed free providers, run autodub-core's ``run_pipeline``, and on a provider 429
    (``QuotaExhausted``) report it exhausted + re-route THAT stage + re-run — the kernel's resumable
    skip-gates reuse the already-completed stages, so only the failed stage onward repeats. Fail
    closed on an unsupported language pair (``assert_language_pair``) and on total free-pool
    exhaustion (``FreePoolExhausted``). The kernel always runs with ``allow_paid=False`` — routing
    walks the $0 ladder only and NEVER escalates to a paid provider (red line §1/§14).

The worker is an orchestrator-side consumer: it may import BOTH autodub-core and provider-adapters,
but neither of those imports the other (the kernel<->adapters seam stays structural) and neither
imports the gateway/billing/control-plane (kernel hard boundary). All real provider HTTP lives in
provider-adapters; this module only chooses which free provider each stage uses.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path

from autodub_core import JobPaths
from autodub_core import run_pipeline as _run_pipeline
from ovt_schemas import Job
from provider_adapters import (
    ProviderAvailability,
    ProviderResult,
    QuotaExhausted,
    Resolver,
    assert_language_pair,
    probe,
    route_free,
)

from .control_plane import ControlPlane
from .storage import Storage

_STAGE_KINDS = ("asr", "mt", "tts")
# Default circuit-break window when a 429 carried no Retry-After: hold the provider down ~1h so we
# don't immediately re-hit a quota'd API (the control plane clamps to its own max). 429 不复撞.
_DEFAULT_RESET_SEC = 3600.0


class FreePoolExhausted(RuntimeError):
    """No free provider remains for a needed stage (every candidate is exhausted or unconfigured).
    The worker maps this to ``cp.fail(error_code='free_pool_exhausted')`` — it is the fail-closed
    terminal, NEVER a paid escalation (red line §1/§14)."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"no free provider available for {kind}")
        self.kind = kind


class PipelineError(RuntimeError):
    """The pipeline ran but did not produce an expected deliverable (worker -> internal_error)."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def inject_provider_env(
    providers: Mapping[str, Mapping[str, str]],
    *,
    environ: MutableMapping[str, str] | None = None,
) -> list[str]:
    """Set each free provider's keys into the environment (SECRETS -> the env-keyed
    provider-adapters ``select`` seam). The control plane sends inner keys ALREADY NAMED as the env
    vars provider-adapters reads (``GROQ_API_KEY`` / ``CLOUDFLARE_ACCOUNT_ID`` /
    ``CLOUDFLARE_API_TOKEN`` / ``DEEPL_API_KEY`` / ...), so the worker sets them verbatim.
    Returns the configured provider NAMES (sorted, names only — never key values) for a
    redaction-safe startup log. The keys live in-process env only; SECRETS keeps them off disk."""
    env = environ if environ is not None else os.environ
    configured: list[str] = []
    for name, fields in providers.items():
        wrote = False
        for env_name, value in fields.items():
            env[str(env_name)] = str(value)
            wrote = True
        if wrote:
            configured.append(str(name))
    return sorted(configured)


def _available_free_providers(kind: str) -> frozenset[str]:
    """Free provider names whose adapter is currently available (key/binary present) for ``kind``.
    Reads provider-adapters' registry probe — which checks os.environ AFTER inject_provider_env — so
    an unconfigured provider is never routed to. Paid names are excluded defensively (red line)."""
    return frozenset(name for name, avail, info in probe(kind) if avail and not info.paid)


def _stage_kinds(output_mode: str) -> tuple[str, ...]:
    # subtitle_only skips tts entirely (no dub); dub_only / both run asr -> mt -> tts.
    return ("asr", "mt") if output_mode == "subtitle_only" else ("asr", "mt", "tts")


def _deliverables(job: Job, paths: JobPaths) -> list[tuple[str, str, str, Path]]:
    # (artifact field, output filename, content-type, produced source path) per output_mode. Field
    # names + filenames MUST match the control plane's complete() contract (video_key/srt_key,
    # output.mp4/output.srt) — the same convention the T2.2 stub used.
    out: list[tuple[str, str, str, Path]] = []
    if job.output_mode in ("dub_only", "both"):
        out.append(("video_key", "output.mp4", "video/mp4", paths.dubbed_video))
    if job.output_mode in ("subtitle_only", "both"):
        out.append(("srt_key", "output.srt", "application/x-subrip", paths.subtitles))
    return out


def _route(
    cp: ControlPlane,
    kind: str,
    excluded: set[str],
    available_providers: Callable[[str], frozenset[str]],
    now_ms: Callable[[], int],
) -> str | None:
    """Pick a free provider for ``kind`` via the FREE-POOL router, or None if none is usable.

    ``configured`` restricts route_free to providers whose adapter is actually available AND not
    locally excluded (one we already saw 429 on this run). route_free further skips any provider the
    shared snapshot marks circuit-broken, and NEVER returns a paid name (red line)."""
    snap = cp.get_provider_availability()
    avail = ProviderAvailability(
        now_ms=snap.now_ms or now_ms(), exhausted_until=dict(snap.exhausted_until)
    )
    configured = available_providers(kind) - excluded
    result = route_free(kind, avail, configured=configured)
    return result.provider if isinstance(result, ProviderResult) else None


def _emit(
    on_telemetry: Callable[..., None] | None, plan: Mapping[str, str | None], kinds: tuple[str, ...]
) -> None:
    # OBS (#24): report a representative routed provider + the routing outcome. Redaction-safe by
    # construction (a ladder NAME + a closed-enum result; the control plane re-validates). Per-stage
    # accuracy is deferred — run_pipeline runs the stages internally (kernel stays frozen).
    if on_telemetry is not None:
        on_telemetry(provider=plan[kinds[0]], free_pool_result="ok")


def _infer_kind(provider: str, plan: Mapping[str, str | None], kinds: tuple[str, ...]) -> str:
    for k in kinds:
        if plan.get(k) == provider:
            return k
    return kinds[-1]  # fall back to the deepest stage if the 429'd provider isn't in the plan


def _collect(
    storage: Storage,
    job: Job,
    claim_version: int,
    paths: JobPaths,
    make_key: Callable[[str, int, str], str],
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for field, name, content_type, src in _deliverables(job, paths):
        if not src.exists():
            raise PipelineError(f"pipeline produced no {field}")
        key = make_key(job.job_id, claim_version, name)
        storage.upload(key, src.read_bytes(), content_type=content_type)
        artifacts[field] = key
    return artifacts


def run_real_pipeline(
    cp: ControlPlane,
    storage: Storage,
    job: Job,
    claim_version: int,
    *,
    in_path: Path,
    workdir: Path | str,
    make_key: Callable[[str, int, str], str],
    resolver: object | None = None,
    on_telemetry: Callable[..., None] | None = None,
    run_pipeline_fn: Callable[..., Path] = _run_pipeline,
    available_providers: Callable[[str], frozenset[str]] = _available_free_providers,
    now_ms: Callable[[], int] = _now_ms,
    max_reroutes: int = 6,
) -> dict[str, str]:
    """Run one claimed job through the REAL pipeline and return ``{artifact_field: r2_key}``.

    Raises ``LanguageError`` (unsupported pair, fail-closed -> the worker maps ``.code``),
    ``FreePoolExhausted`` (no free provider left -> ``free_pool_exhausted``), or ``PipelineError`` /
    other (-> ``internal_error``). ``run_pipeline_fn`` / ``available_providers`` are injected so the
    routing/rotation logic is testable in CI without ffmpeg; the real ffmpeg run is proved by
    ``just dev`` (DEVLOOP) + autodub-core's e2e smoke."""
    resolver = resolver if resolver is not None else Resolver()
    # Fail-closed language gate (T1.3f) BEFORE any provider/transcode work.
    assert_language_pair(job.source_lang_hint, job.target_lang, job.output_mode)
    paths = JobPaths(Path(workdir) / "pipeline").ensure()
    kinds = _stage_kinds(job.output_mode)
    excluded: dict[str, set[str]] = {k: set() for k in _STAGE_KINDS}
    plan: dict[str, str | None] = {"asr": job.plan.asr, "mt": job.plan.mt, "tts": job.plan.tts}
    # Initial route of every needed stage (a stage with no free provider fails closed up front).
    for kind in kinds:
        chosen = _route(cp, kind, excluded[kind], available_providers, now_ms)
        if chosen is None:
            raise FreePoolExhausted(kind)
        plan[kind] = chosen
    _emit(on_telemetry, plan, kinds)
    for _ in range(max_reroutes + 1):
        routed = job.model_copy(update={"plan": job.plan.model_copy(update=plan)})
        try:
            run_pipeline_fn(paths, resolver, source=str(in_path), job=routed)
            return _collect(storage, job, claim_version, paths, make_key)
        except QuotaExhausted as exc:
            # Circuit-break THIS stage's provider (report shared + exclude locally) and re-route
            # just that stage. The kernel's resumable skip-gates reuse the completed stages on the
            # re-run, so only the failed stage onward repeats with the new provider (429 不复撞).
            kind = exc.kind if exc.kind in _STAGE_KINDS else _infer_kind(exc.provider, plan, kinds)
            reported = plan.get(kind) or exc.provider  # the FREE provider we routed (never paid)
            reset_ms = now_ms() + int((exc.retry_after_sec or _DEFAULT_RESET_SEC) * 1000)
            cp.report_provider_exhausted(reported, reset_at_ms=reset_ms, reason="429")
            excluded[kind].add(reported)
            chosen = _route(cp, kind, excluded[kind], available_providers, now_ms)
            if chosen is None:
                raise FreePoolExhausted(kind) from exc
            plan[kind] = chosen
            _emit(on_telemetry, plan, kinds)
    raise FreePoolExhausted("max_reroutes")  # bounded rotation budget spent without converging
