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
from functools import partial
from pathlib import Path

from autodub_core import JobPaths
from autodub_core import ProviderUnavailable as KernelProviderUnavailable
from autodub_core import config as autodub_config
from autodub_core import run_pipeline as _run_pipeline
from ovt_schemas import Job
from provider_adapters import (
    COMMERCIAL_SAFE_TTS,
    LanguageError,
    ProviderAvailability,
    ProviderResult,
    ProviderUnavailable,
    QuotaExhausted,
    Resolver,
    assert_language_pair,
    build_diarizer,
    deepl_target_code,
    get_capability,
    piper_model_covers,
    probe,
    route_free,
    tts_preset_voices,
)

from .control_plane import ControlPlane
from .storage import Storage

logger = logging.getLogger("media_worker")

_STAGE_KINDS = ("asr", "mt", "tts")
# Default circuit-break window when a 429 carried no Retry-After: hold the provider down ~1h so we
# don't immediately re-hit a quota'd API (the control plane clamps to its own max). 429 不复撞.
_DEFAULT_RESET_SEC = 3600.0
# Sentinel plan value for an MT/TTS stage with NO free provider available up front. The kernel SKIPS
# translate()/tts() entirely for a no-speech / nothing-to-synth job (autodub-core stages.py), so
# failing such a stage up front would wrongly reject a job the kernel could complete. Routing it to
# this sentinel makes _FreePoolSelectResolver fail closed (FreePoolExhausted) ONLY if the kernel
# actually reaches the stage — never re-hitting a circuit-broken provider (@CodeX bot M2-CLOSE).
_FREE_POOL_EXHAUSTED = "__free_pool_exhausted__"

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


def _locale_commercial_safe_tts(target_lang: str) -> frozenset[str]:
    """Commercial-safe TTS providers that COVER ``target_lang`` (the capability's tts_models ∩
    COMMERCIAL_SAFE_TTS). CloudflareTTS/MeloTTS covers 6 languages, so routing must not offer it
    for a piper-only locale (de/pt-BR/ru/it) — that would raise in voices_for -> internal_error."""
    cap = get_capability(target_lang)
    if cap is None:
        return frozenset()
    return frozenset(m for m in cap.tts_models if m in COMMERCIAL_SAFE_TTS)


def _deepl_supports(target_lang: str) -> bool:
    """Whether DeepL offers ``target_lang``. DeepL fails CLOSED (LanguageError from
    deepl_target_code) for targets it doesn't offer (hi/ar/eo), so this lets routing drop it
    before route_free picks it (@CodeX bot R6 P2)."""
    try:
        deepl_target_code(target_lang)
        return True
    except LanguageError:
        return False


def _available_free_providers(kind: str, target_lang: str) -> frozenset[str]:
    """Free provider names whose adapter is currently available (key/binary present) for ``kind``.
    Reads provider-adapters' registry probe — which checks os.environ AFTER inject_provider_env — so
    an unconfigured provider is never routed to. Paid names are excluded defensively (red line).

    For ``tts`` the set is restricted to COMMERCIAL-SAFE voices that COVER ``target_lang``: tts is
    only routed for dub output; edge_tts is the non-commercial lane (never a default dub voice,
    T1.3f / AD-6), and a commercial-safe provider that does NOT serve the locale (Cloudflare
    MeloTTS for de/pt-BR/ru/it) must not be picked — else routing bypasses the commercial-safe gate
    or fails internal_error at voices_for (CodeX P1/P2)."""
    free = frozenset(name for name, avail, info in probe(kind) if avail and not info.paid)
    if kind == "tts":
        free &= _locale_commercial_safe_tts(target_lang)
        # The capability registry lists "piper" for a locale generically, but Piper exposes ONE
        # installed model and probe() can't see its language. Drop piper unless the installed model
        # actually covers the locale, so routing picks another commercial-safe provider (or fails
        # closed) instead of synthesizing the WRONG language (@CodeX bot M2-CLOSE).
        if "piper" in free and not piper_model_covers(target_lang):
            free -= {"piper"}
    elif kind == "mt":
        # DeepL fails CLOSED (LanguageError) for targets it doesn't offer (hi/ar/eo). Unlike the
        # other MT providers' input rejections (ProviderUnavailable, which the run loop reroutes),
        # a LanguageError is the product-level fail-closed terminal, so drop DeepL at routing time
        # when it can't serve the target — route_free then picks the next free MT (@CodeX R6 P2).
        if "deepl" in free and not _deepl_supports(target_lang):
            free -= {"deepl"}
    return free


def _tts_pin_serviceable(provider: str, voice_id: str | None, target_lang: str) -> bool:
    """Whether an EXPLICITLY user-pinned (``provider``, ``voice_id``) can structurally serve
    ``target_lang`` on THIS box — deliberately BYPASSING the commercial-safe gate (plan §8 dec.4: a
    user may pin the experimental, non-commercial edge voice; the owner bears the ToS risk, and edge
    is still never auto-routed). Enforced:
      (a) the adapter is available (installed / keyed, via probe) AND NOT paid — the paid red line
          holds even for a pin;
      (b) the capability registry lists the provider as covering the locale;
      (c) for piper, the installed model actually covers the locale;
      (d) open-core guardrail (§4) — ``voice_id`` is a member of the provider's CLOSED preset set
          for the locale (the same set the picker offers); an ARBITRARY voice string / model path
          (Tier 2/3) is rejected, so a pin can never smuggle an off-catalog voice.
    A miss is a STRUCTURAL gap the picker should never have offered, so the worker fails the job
    closed (``tts_provider_unavailable``) rather than substituting — distinct from a transient
    run-time exhaustion, which reroutes + flags voice_substituted."""
    if provider not in {name for name, avail, info in probe("tts") if avail and not info.paid}:
        return False
    cap = get_capability(target_lang)
    if cap is None or provider not in cap.tts_models:
        return False
    if provider == "piper" and not piper_model_covers(target_lang):
        return False
    return voice_id in tts_preset_voices(provider, target_lang)


def _stage_kinds(output_mode: str) -> tuple[str, ...]:
    # subtitle_only skips tts entirely (no dub); dub_only / both run asr -> mt -> tts.
    return ("asr", "mt") if output_mode == "subtitle_only" else ("asr", "mt", "tts")


def _deliverables(job: Job, paths: JobPaths) -> list[tuple[str, str, str, Path]]:
    # (artifact field, output filename, content-type, produced source path) per
    # output_mode + subtitle_delivery. Field names + filenames MUST match the control plane's
    # complete() contract (video_key/srt_key, output.mp4/output.srt). This mirrors
    # stages.mux()'s deliver_video choice EXACTLY so we upload precisely what mux produced:
    # a burned video reuses video_key (M2.1 — no new artifact field, CodeX decision), and
    # dubbed_video is only its intermediate.
    want_subs = job.output_mode in ("subtitle_only", "both")
    burn = (
        want_subs
        and job.subtitle_delivery in ("burned", "both")
        and autodub_config.BURN_SUBTITLES_ENABLED
    )
    out: list[tuple[str, str, str, Path]] = []
    if burn:
        out.append(("video_key", "output.mp4", "video/mp4", paths.burned_video))
    elif job.output_mode in ("dub_only", "both"):
        out.append(("video_key", "output.mp4", "video/mp4", paths.dubbed_video))
    if want_subs and job.subtitle_delivery in ("srt", "both"):
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


class _FreePoolSelectResolver:
    """Wraps the kernel's Resolver so a stage routed to the FREE-POOL sentinel fails closed at the
    MOMENT the kernel calls ``select()`` for it — not up front. ASR always runs (routed eagerly),
    but MT/TTS are data-dependent: the kernel skips ``translate()``/``tts()`` for an empty
    transcript, so a sentinel-routed MT/TTS stage that's never reached doesn't fail the job. Every
    other call delegates to the wrapped resolver unchanged (@CodeX bot M2-CLOSE)."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        # The (kind, provider) of the most recent real selection. The kernel selects a provider
        # then immediately uses it, so on a provider rejection (ProviderUnavailable) this attributes
        # the failure to the right stage+provider for a local reroute (@CodeX bot R4 P1).
        self.last_select: tuple[str, str | None] | None = None

    def select(self, kind: str, requested: str | None = None, allow_paid: bool = False) -> object:
        # Signature MIRRORS the kernel's Resolver.select (kind, requested, allow_paid — all
        # positional): pin_resolver wraps this and forwards allow_paid POSITIONALLY, so a
        # keyword-only param here would TypeError on the first real selection (@CodeX bot R3 P1).
        if requested == _FREE_POOL_EXHAUSTED:
            raise FreePoolExhausted(kind)
        self.last_select = (kind, requested)
        return self._inner.select(kind, requested, allow_paid)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        # Delegate anything else the kernel / pin_resolver needs to the real resolver.
        return getattr(object.__getattribute__(self, "_inner"), name)


def _route_plan(
    kind: str,
    snapshot: ProviderAvailability,
    excluded: set[str],
    avail_fn: Callable[[str], frozenset[str]],
    on_telemetry: Callable[..., None] | None,
) -> str:
    """Plan value for ``kind``: a picked free provider, else fail-closed handling. ASR always runs,
    so no free provider fails the job NOW; MT/TTS may be skipped by the kernel (no-speech), so they
    defer to the sentinel and fail closed only if the kernel actually reaches them."""
    chosen = _pick(kind, snapshot, excluded, avail_fn)
    if chosen is not None:
        return chosen
    if kind == "asr":
        _emit_exhausted(on_telemetry)
        raise FreePoolExhausted(kind)
    return _FREE_POOL_EXHAUSTED


def _refresh_snapshot(
    cp: ControlPlane, clock: Callable[[], int], prior: ProviderAvailability
) -> ProviderAvailability:
    """Re-fetch the FREE-POOL availability snapshot for a reroute; a transient blip on the GET must
    not abort the reroute, so fall back to the prior snapshot (the local ``excluded`` set still
    prevents re-picking the failed provider) — @CodeX bot M2-CLOSE."""
    try:
        return _snapshot(cp, clock)
    except Exception:
        logger.warning("availability refresh failed; reusing prior snapshot for reroute")
        return prior


def _exclude_and_reroute(
    kind: str,
    failed: str,
    *,
    kinds: tuple[str, ...],
    plan: dict[str, str | None],
    excluded: dict[str, set[str]],
    snapshot: ProviderAvailability,
    avail_fn: Callable[[str], frozenset[str]],
    on_telemetry: Callable[..., None] | None,
    paths: JobPaths,
    provider_wide: bool,
) -> None:
    """Exclude ``failed`` and re-pick each affected stage currently planned on it. Mutates
    ``plan`` / ``excluded``.

    ``provider_wide`` selects the exclusion SCOPE by failure type:
      * 429 (``QuotaExhausted``, ``provider_wide=True``): the provider is GLOBALLY rate-limited,
        so exclude it from every active stage from ``kind`` onward (stages before ``kind`` already
        produced cached output, so their plan is moot) — never re-hit anywhere (429 不复撞).
      * input rejection (``ProviderUnavailable``, ``provider_wide=False``): the provider is HEALTHY
        and only THIS capability rejected the input (e.g. Cloudflare MT on an 'auto' source), so
        exclude/reroute ONLY the failed stage — a sibling stage it could still serve (e.g. the same
        provider's TTS) must keep using it (@CodeX bot R5 P1)."""
    affected = kinds[kinds.index(kind):] if provider_wide else (kind,)
    for k in affected:
        excluded[k].add(failed)
        if plan.get(k) != failed:
            continue
        # TTS persists per-segment raws; clear them on a provider switch so the new voice is uniform
        # across the whole deliverable (no mixed-timbre output across segments).
        if k == "tts":
            _clear_dir(paths.tts)
        plan[k] = _route_plan(k, snapshot, excluded[k], avail_fn, on_telemetry)
    _emit(on_telemetry, provider=plan[kinds[0]])


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
    build_diarizer_fn: Callable[[], object] | None = None,
) -> dict[str, str]:
    """Run one claimed job through the REAL pipeline and return ``{artifact_field: r2_key}``.

    Raises ``LanguageError`` (unsupported pair, fail-closed -> the worker maps ``.code``),
    ``FreePoolExhausted`` (no free provider left -> ``free_pool_exhausted``), or ``PipelineError`` /
    other (-> ``internal_error``). ``run_pipeline_fn`` / ``available_providers`` / ``now_ms`` are
    injected so the routing/rotation logic is testable in CI without ffmpeg; the real ffmpeg run is
    proved by ``just dev`` (DEVLOOP) + autodub-core's e2e smoke."""
    resolver = resolver if resolver is not None else Resolver()
    # The kernel calls resolver.select() per stage only when it actually needs that provider
    # (translate()/tts() skip it for an empty job). Wrap so a sentinel-routed stage fails closed
    # lazily, at the call, rather than up front (@CodeX bot M2-CLOSE).
    kernel_resolver = _FreePoolSelectResolver(resolver)
    run_fn: Callable[..., Path] = run_pipeline_fn if run_pipeline_fn is not None else _run_pipeline
    avail_fn: Callable[[str], frozenset[str]] = (
        available_providers
        if available_providers is not None
        else partial(_available_free_providers, target_lang=job.target_lang)
    )
    clock = now_ms if now_ms is not None else _now_ms
    # Fail-closed language gate (T1.3f) BEFORE any provider/transcode work.
    assert_language_pair(job.source_lang_hint, job.target_lang, job.output_mode)
    # Per-locale burn font for the libass overlay (M2.1). Resolved HERE — the worker may import the
    # language registry; the kernel may NOT (boundary). None when the locale needs no special font
    # (Latin) or is uncatalogued; the kernel then uses the libass default.
    burn_cap = get_capability(job.target_lang)
    burn_font = burn_cap.burn_font if burn_cap is not None else None
    # PR-2 visible AIGC watermark font: a DEPLOYMENT-WIDE font PATH (env OVT_WATERMARK_FONT) so the
    # burned-in overlay can render CJK. Unlike burn_font it is NOT per-locale — the operator's
    # watermark text (default zh) is independent of the target language. A missing/unset/non-file
    # path leaves it None (drawtext falls back to the ffmpeg default face) rather than failing every
    # watermarked job on a misconfigured box.
    watermark_font = os.environ.get("OVT_WATERMARK_FONT") or None
    if watermark_font is not None and not Path(watermark_font).is_file():
        logger.warning(
            "OVT_WATERMARK_FONT %s is not a file; AIGC watermark falls back to the ffmpeg default "
            "face (CJK may not render)", watermark_font,
        )
        watermark_font = None
    paths = JobPaths(Path(workdir) / "pipeline").ensure()
    # P4 (分角色配音, opt-in): when the job requests diarization, build + inject the diarizer so
    # the kernel relabels speakers before translate. Gated on available() (wheel + baked ONNX models
    # present) — never inject a diarizer that would fail mid-job. Requested-but-unavailable (a
    # misconfigured box) logs + proceeds single-speaker rather than failing the dub (diarization can
    # only ADD speakers — plan §5). Built ONCE (constant across reroutes); the heavy ~30MB sherpa
    # model loads lazily INSIDE diarize(), only when the kernel reaches that stage, and runs
    # sequentially within the job's single pipeline pass — with worker_concurrency=1 (§14 2GB
    # baseline) two model-loaded CPU tasks never synthesize at once (plan §7 constraint 1).
    diarizer: object | None = None
    if job.plan.diarization:
        factory = build_diarizer_fn if build_diarizer_fn is not None else build_diarizer
        candidate = factory()
        available = getattr(candidate, "available", None)
        if callable(available) and available():
            diarizer = candidate
        else:
            logger.warning(
                "job %s requested diarization but no diarizer is available on this box; "
                "proceeding single-speaker", job.job_id,
            )
    kinds = _stage_kinds(job.output_mode)
    excluded: dict[str, set[str]] = {k: set() for k in _STAGE_KINDS}
    plan: dict[str, str | None] = {"asr": job.plan.asr, "mt": job.plan.mt, "tts": job.plan.tts}
    # Soft-pin (P0): an explicit user-picked dub voice (JobPlan.tts_voice) HONORS the pinned tts
    # provider instead of auto-routing. The pin bypasses the commercial-safe gate — a user may
    # explicitly select the experimental edge voice (owner bears the ToS risk; edge is still NEVER
    # auto-routed) — plan §8 decision 4. A STRUCTURAL miss (provider not installed / doesn't cover
    # the locale) fails closed NOW (tts_provider_unavailable, the picker shouldn't have offered it);
    # a TRANSIENT exhaustion/rejection reroutes to an auto commercial-safe voice and records
    # voice_substituted (reconciled at the top of the run loop below). A pin needs BOTH a provider
    # (job.plan.tts) and a voice id (job.plan.tts_voice); a bare provider is not a pin.
    # A pin needs a CONCRETE provider (not the "auto" auto-route sentinel from defaultPlan) AND a
    # voice id; an "auto" + voice combo is contradictory, so treat it as no pin and auto-route (the
    # orphan voice is ignored) rather than fail closed on a misleading "provider 'auto'" error.
    # A pin needs a concrete provider AND at least one voice — either a single tts_voice OR a P4c
    # voice_pool (an ordered list distributed across diarized speakers). The picker sends one voice
    # form or the other, never both.
    tts_pin = (
        job.plan.tts
        if (
            "tts" in kinds
            and (job.plan.tts_voice or job.plan.voice_pool)
            and job.plan.tts
            and job.plan.tts != "auto"
        )
        else None
    )
    tts_voice = job.plan.tts_voice if tts_pin else None
    # P4c voice pool (分角色配音): the pinned provider's ordered voices, distributed across diarized
    # speakers by the kernel. Copied so a reroute can drop it without mutating the Job.
    voice_pool = list(job.plan.voice_pool) if (tts_pin and job.plan.voice_pool) else None
    voice_substituted = False
    # Validate EVERY pinned voice (the pool, else the single voice): each must be an installed,
    # non-paid, locale-covering, CLOSED-preset voice of the pinned provider (open-core §4). A
    # structural miss is the picker's fault → fail closed (tts_provider_unavailable), unlike a
    # transient run-time exhaustion (which reroutes + substitutes below).
    _pinned_voices = voice_pool if voice_pool else ([tts_voice] if tts_voice else [])
    if tts_pin is not None and not all(
        _tts_pin_serviceable(tts_pin, v, job.target_lang) for v in _pinned_voices
    ):
        raise LanguageError(
            "tts_provider_unavailable",
            f"the pinned dub voice provider {tts_pin!r} is unavailable for {job.target_lang!r} on "
            f"this deployment (not installed / no locale coverage / an off-catalog voice)",
        )
    # Reroute backstop: at most one rotation per free provider of each active stage. A kind whose
    # providers are ALL excluded fails closed via _pick -> None first; this budget is sized to the
    # active ladders so a worst-case burst (every non-tail provider of every stage 429s) can't trip
    # the backstop before each stage has tried all its free providers (off-by-one fix).
    if max_reroutes is None:
        max_reroutes = sum(len(avail_fn(k)) for k in kinds) + 1
    # Initial route: ONE shared availability snapshot for all stages (re-routes refetch fresh).
    snapshot = _snapshot(cp, clock)
    for kind in kinds:
        if kind == "tts" and tts_pin is not None:
            plan["tts"] = tts_pin  # honor the pin; skip auto-route (commercial-gate bypass)
            continue
        plan[kind] = _route_plan(kind, snapshot, excluded[kind], avail_fn, on_telemetry)
    _emit(on_telemetry, provider=plan[kinds[0]])
    for _ in range(max_reroutes + 1):
        # A pinned tts provider that got rerouted (its provider was excluded on a 429 / input
        # rejection) means the user's chosen voice can't be used: fall back to the auto-routed
        # commercial-safe voice and flag voice_substituted so the UI can note it. Drop the pin so we
        # don't re-substitute or resurrect the dead voice id (wrong for the new engine). By design a
        # SHARED-provider 429 also drops the pin — e.g. cloudflare serving both asr+tts is circuit-
        # broken provider-wide (account-wide quota), so an asr 429 correctly retires its tts too.
        if tts_pin is not None and plan.get("tts") != tts_pin:
            # Drop the pool too: its voices belong to the now-dead pinned provider, so the auto
            # replacement must round-robin ITS own voices, not resurrect off-engine ids.
            voice_substituted, tts_voice, voice_pool, tts_pin = True, None, None, None
        routed_plan: dict[str, str | bool | list[str] | None] = {
            **plan, "tts_voice": tts_voice, "voice_pool": voice_pool,
            "voice_substituted": voice_substituted}
        routed = job.model_copy(update={"plan": job.plan.model_copy(update=routed_plan)})
        try:
            # target_lang is a REQUIRED kwarg of autodub_core.run_pipeline (it derives the rest from
            # `job`, but the signature still requires it) — omitting it raises TypeError (CodeX P1).
            run_fn(paths, kernel_resolver, source=str(in_path),
                   target_lang=routed.target_lang, job=routed, burn_font=burn_font,
                   watermark_font=watermark_font, diarizer=diarizer)
            return _collect(storage, job, claim_version, paths, make_key)
        except FreePoolExhausted:
            # A sentinel-routed MT/TTS stage the kernel actually REACHED (the job had speech /
            # needed a dub) — fail closed now and emit the real routing outcome (@CodeX bot).
            _emit_exhausted(on_telemetry)
            raise
        except QuotaExhausted as exc:
            # A FREE provider returned 429: the circuit-breaker state is PER-PROVIDER (shared), so
            # REPORT it exhausted (global circuit-break, clamped control-plane side) and re-route
            # every not-yet-completed stage planned on it — never re-hit (429 不复撞). A transient
            # report/refresh blip must NOT abort the re-route (exclusion still prevents re-picking).
            kind = exc.kind if exc.kind in _STAGE_KINDS else _infer_kind(exc.provider, plan, kinds)
            reported = plan.get(kind) or exc.provider  # the FREE provider we routed (never paid)
            reset_ms = clock() + int((exc.retry_after_sec or _DEFAULT_RESET_SEC) * 1000)
            try:
                cp.report_provider_exhausted(reported, reset_at_ms=reset_ms, reason="429")
            except Exception:
                logger.warning("provider-exhausted report failed; re-routing locally")
            snapshot = _refresh_snapshot(cp, clock, snapshot)
            _exclude_and_reroute(kind, reported, kinds=kinds, plan=plan, excluded=excluded,
                                 snapshot=snapshot, avail_fn=avail_fn, on_telemetry=on_telemetry,
                                 paths=paths, provider_wide=True)
        except (ProviderUnavailable, KernelProviderUnavailable):
            # A provider that is HEALTHY but can't serve THIS input (e.g. Cloudflare MT rejecting an
            # 'auto' source, a TTS voice gap) raises ProviderUnavailable — NOT a 429. Reroute it
            # like a quota hit, but exclude it LOCALLY (this job only): NEVER
            # report_provider_exhausted, since circuit-breaking a healthy provider globally would be
            # wrong (@CodeX bot R4 P1). The kernel selects a provider then uses it, so last_select
            # attributes the failure to the right stage+provider. If it isn't attributable to a
            # selected stage (no select preceded it), surface it (-> internal_error), don't reroute.
            # Catch BOTH the adapters' ProviderUnavailable (provider_adapters.base) AND the kernel's
            # OWN ProviderUnavailable (autodub_core.providers, raised by _assign_voices on a TTS
            # voice gap): the kernel<->adapters seam keeps the two classes unrelated, so a single
            # bind would silently miss the kernel-raised voice-gap reroute (self-review P3).
            sel = kernel_resolver.last_select
            if sel is None or sel[0] not in _STAGE_KINDS or not sel[1]:
                raise
            snapshot = _refresh_snapshot(cp, clock, snapshot)
            # provider_wide=False: a healthy provider rejected only THIS capability's input, so
            # exclude/reroute the failed stage ALONE (don't strip it from sibling stages it can
            # still serve) — @CodeX bot R5 P1.
            _exclude_and_reroute(sel[0], sel[1], kinds=kinds, plan=plan, excluded=excluded,
                                 snapshot=snapshot, avail_fn=avail_fn, on_telemetry=on_telemetry,
                                 paths=paths, provider_wide=False)
    _emit_exhausted(on_telemetry)
    raise FreePoolExhausted("max_reroutes")  # bounded backstop (sized above so this is unreachable)
