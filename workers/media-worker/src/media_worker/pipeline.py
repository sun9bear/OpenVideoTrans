"""media-worker: run the REAL autodub-core pipeline with FREE-POOL routing (M2-CLOSE #26).

Replaces the T2.2 stub copy-loop. Two responsibilities:

  * ``inject_provider_env`` — fold the SECRETS-pulled free-provider keys into the environment
    so the env-keyed provider-adapters ``select()`` resolves them. The keys live in-process env ONLY
    (SECRETS: never on the box disk).

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

import logging
import os
import shutil
import time
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path

from autodub_core import JobPaths
from autodub_core import run_pipeline as _run_pipeline
from ovt_schemas import Job
from provider_adapters import (
    COMMERCIAL_SAFE_TTS,
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

logger = logging.getLogger("media_worker")

_STAGE_KINDS = ("asr", "mt", "tts")
# Default circuit-break window when a 429 carried no Retry-After: hold the provider down ~1h so we
# don't immediately re-hit a quota'd API (the control plane clamps to its own max). 429 不复撞.
_DEFAULT_RESET_SEC = 3600.0

# The /internal/credentials provider payload uses GENERIC field names (apiKey/accountId/apiToken —
# apps/control-plane/src/credentials.ts ProviderCredentials); provider-adapters' adapters read
# ADAPTER-SPECIFIC env vars. This bridges (provider, payload field) -> the env var the adapter reads
# so an injected key actually configures the provider. A provider/field absent here can't be
# consumed by the Tier-1 free pool and is skipped (so a typo can't silently configure nothing).
_PROVIDER_ENV_MAP: dict[str, dict[str, str]] = {
    "groq": {"apiKey": "GROQ_API_KEY"},
    "cloudflare": {"accountId": "CLOUDFLARE_ACCOUNT_ID", "apiToken": "CLOUDFLARE_API_TOKEN"},
    "deepl": {"apiKey": "DEEPL_API_KEY"},
}


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
    """Translate the SECRETS ``/internal/credentials`` provider payload into the environment so the
    env-keyed provider-adapters ``select`` seam resolves the providers. The payload uses GENERIC
    field names (``apiKey`` / ``accountId`` / ``apiToken`` — see credentials.ts); provider-adapters
    reads ADAPTER-SPECIFIC env vars (``GROQ_API_KEY`` / ``CLOUDFLARE_ACCOUNT_ID`` /
    ``CLOUDFLARE_API_TOKEN`` / ``DEEPL_API_KEY``), so ``_PROVIDER_ENV_MAP`` bridges the two. A
    provider/field the Tier-1 free pool can't consume is skipped. Returns the configured provider
    NAMES (sorted, names only — never key values) for a redaction-safe startup log. The keys live
    in-process env only; SECRETS keeps them off the box disk."""
    env = environ if environ is not None else os.environ
    configured: list[str] = []
    for name, fields in providers.items():
        field_map = _PROVIDER_ENV_MAP.get(str(name))
        if field_map is None:
            continue  # not a Tier-1 free provider the worker can configure
        wrote = False
        for field, value in fields.items():
            env_name = field_map.get(str(field))
            if env_name is not None:
                env[env_name] = str(value)
                wrote = True
        if wrote:
            configured.append(str(name))
    return sorted(configured)


def _available_free_providers(kind: str) -> frozenset[str]:
    """Free provider names whose adapter is currently available (key/binary present) for ``kind``.
    Reads provider-adapters' registry probe — which checks os.environ AFTER inject_provider_env — so
    an unconfigured provider is never routed to. Paid names are excluded defensively (red line).

    For ``tts`` the set is further restricted to COMMERCIAL-SAFE voices (piper/cloudflare): tts is
    only routed for dub output, and edge_tts is the experimental non-commercial lane that must NEVER
    be a default dub voice (T1.3f / AD-6). Without this filter, routing on a host where piper is
    absent but edge_tts is installed would pick edge_tts and bypass the commercial-safe gate that
    assert_language_pair enforces at admission (CodeX P1)."""
    free = frozenset(name for name, avail, info in probe(kind) if avail and not info.paid)
    if kind == "tts":
        free &= COMMERCIAL_SAFE_TTS
    return free


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


def _snapshot(cp: ControlPlane, now_ms: Callable[[], int]) -> ProviderAvailability:
    """Hydrate one shared FREE-POOL availability snapshot from the control plane (now-anchored)."""
    snap = cp.get_provider_availability()
    return ProviderAvailability(
        now_ms=snap.now_ms or now_ms(), exhausted_until=dict(snap.exhausted_until)
    )


def _pick(
    kind: str,
    snapshot: ProviderAvailability,
    excluded: set[str],
    available_providers: Callable[[str], frozenset[str]],
) -> str | None:
    """Pick a free provider for ``kind`` via the FREE-POOL router, or None if none is usable.

    ``configured`` restricts route_free to providers whose adapter is available AND not locally
    excluded (one we already saw 429 on this run). route_free further skips any provider the
    snapshot marks circuit-broken, and NEVER returns a paid name (red line)."""
    configured = available_providers(kind) - excluded
    result = route_free(kind, snapshot, configured=configured)
    return result.provider if isinstance(result, ProviderResult) else None


def _emit(on_telemetry: Callable[..., None] | None, *, provider: str | None) -> None:
    # OBS (#24): report the routed provider + a successful routing outcome (redaction-safe: a ladder
    # NAME + a closed-enum result; the control plane re-validates).
    if on_telemetry is not None:
        on_telemetry(provider=provider, free_pool_result="ok")


def _emit_exhausted(on_telemetry: Callable[..., None] | None) -> None:
    # OBS (#24): mark the free pool exhausted so the last heartbeat before a free_pool_exhausted
    # failure reports the real routing outcome (not a stale "ok"); free_pool_exhausted is allowed.
    if on_telemetry is not None:
        on_telemetry(free_pool_result="free_pool_exhausted")


def _infer_kind(provider: str, plan: Mapping[str, str | None], kinds: tuple[str, ...]) -> str:
    for k in kinds:
        if plan.get(k) == provider:
            return k
    return kinds[-1]  # fall back to the deepest stage if the 429'd provider isn't in the plan


def _clear_dir(d: Path) -> None:
    """Remove the contents of a scratch dir (keep the dir). Used to drop a stage's partial outputs
    so a re-routed provider re-produces them uniformly."""
    if not d.exists():
        return
    for child in d.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


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
    run_pipeline_fn: Callable[..., Path] | None = None,
    available_providers: Callable[[str], frozenset[str]] | None = None,
    now_ms: Callable[[], int] | None = None,
    max_reroutes: int | None = None,
) -> dict[str, str]:
    """Run one claimed job through the REAL pipeline and return ``{artifact_field: r2_key}``.

    Raises ``LanguageError`` (unsupported pair, fail-closed -> the worker maps ``.code``),
    ``FreePoolExhausted`` (no free provider left -> ``free_pool_exhausted``), or ``PipelineError`` /
    other (-> ``internal_error``). ``run_pipeline_fn`` / ``available_providers`` / ``now_ms`` are
    injected so the routing/rotation logic is testable in CI without ffmpeg; the real ffmpeg run is
    proved by ``just dev`` (DEVLOOP) + autodub-core's e2e smoke."""
    resolver = resolver if resolver is not None else Resolver()
    run_fn: Callable[..., Path] = run_pipeline_fn if run_pipeline_fn is not None else _run_pipeline
    avail_fn = available_providers if available_providers is not None else _available_free_providers
    clock = now_ms if now_ms is not None else _now_ms
    # Fail-closed language gate (T1.3f) BEFORE any provider/transcode work.
    assert_language_pair(job.source_lang_hint, job.target_lang, job.output_mode)
    paths = JobPaths(Path(workdir) / "pipeline").ensure()
    kinds = _stage_kinds(job.output_mode)
    excluded: dict[str, set[str]] = {k: set() for k in _STAGE_KINDS}
    plan: dict[str, str | None] = {"asr": job.plan.asr, "mt": job.plan.mt, "tts": job.plan.tts}
    # Reroute backstop: at most one rotation per free provider of each active stage. A kind whose
    # providers are ALL excluded fails closed via _pick -> None first; this budget is sized to the
    # active ladders so a worst-case burst (every non-tail provider of every stage 429s) can't trip
    # the backstop before each stage has tried all its free providers (off-by-one fix).
    if max_reroutes is None:
        max_reroutes = sum(len(avail_fn(k)) for k in kinds) + 1
    # Initial route: ONE shared availability snapshot for all stages (re-routes refetch fresh).
    snapshot = _snapshot(cp, clock)
    for kind in kinds:
        chosen = _pick(kind, snapshot, excluded[kind], avail_fn)
        if chosen is None:
            _emit_exhausted(on_telemetry)
            raise FreePoolExhausted(kind)
        plan[kind] = chosen
    _emit(on_telemetry, provider=plan[kinds[0]])
    for _ in range(max_reroutes + 1):
        routed = job.model_copy(update={"plan": job.plan.model_copy(update=plan)})
        try:
            run_fn(paths, resolver, source=str(in_path), job=routed)
            return _collect(storage, job, claim_version, paths, make_key)
        except QuotaExhausted as exc:
            # Circuit-break THIS stage's provider and re-route just that stage. The kernel's
            # resumable skip-gates reuse the completed stages on the re-run, so only the failed
            # stage onward repeats with the new provider (429 不复撞).
            kind = exc.kind if exc.kind in _STAGE_KINDS else _infer_kind(exc.provider, plan, kinds)
            reported = plan.get(kind) or exc.provider  # the FREE provider we routed (never paid)
            reset_ms = clock() + int((exc.retry_after_sec or _DEFAULT_RESET_SEC) * 1000)
            # A transient report blip must NOT abort the local re-route — excluded[kind] already
            # prevents re-picking the 429'd provider, so the rotation proceeds either way.
            try:
                cp.report_provider_exhausted(reported, reset_at_ms=reset_ms, reason="429")
            except Exception:
                logger.warning("provider-exhausted report failed; re-routing %s locally", kind)
            excluded[kind].add(reported)
            # TTS persists per-segment raws; clear them on a provider switch so the new voice is
            # uniform across the whole deliverable (no mixed-timbre output across segments).
            if kind == "tts":
                _clear_dir(paths.tts)
            chosen = _pick(kind, _snapshot(cp, clock), excluded[kind], avail_fn)
            if chosen is None:
                _emit_exhausted(on_telemetry)
                raise FreePoolExhausted(kind) from exc
            plan[kind] = chosen
            _emit(on_telemetry, provider=chosen)  # report the rotated provider (per-stage fidelity)
    _emit_exhausted(on_telemetry)
    raise FreePoolExhausted("max_reroutes")  # bounded backstop (sized above so this is unreachable)
