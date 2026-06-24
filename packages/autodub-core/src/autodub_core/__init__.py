"""autodub-core — the language-agnostic video translation + dubbing kernel.

Pure stdlib + ffmpeg + ``ovt_schemas`` contracts. Providers are injected via a
``Resolver`` (see ``autodub_core.providers``); the paid-API safety gate lives in
``provider-adapters`` (T1.2), never here. Hard boundary (AD-13/14): no gateway /
control-plane / billing / payment / entitlement / credentials imports.
"""

from __future__ import annotations

from .config import (
    CANON_CHANNELS,
    CANON_SAMPLE_FMT,
    CANON_SR,
    DEFAULT_CHARS_PER_SEC,
    MAX_SPEEDUP,
    JobPaths,
)
from .isolation import (
    PaidPinViolation,
    PathEscapeError,
    ensure_within,
    job_root,
    pin_resolver,
    safe_component,
)
from .manifest import write_manifest
from .providers import (
    AsrProvider,
    MtProvider,
    ProviderInfo,
    ProviderUnavailable,
    Resolver,
    TtsProvider,
)
from .stages import (
    TimingPlan,
    align,
    assign_timing,
    ingest,
    mux,
    prepare,
    run_pipeline,
    transcribe,
    translate,
    tts,
)

__version__ = "0.0.1"

__all__ = [
    "CANON_CHANNELS",
    "CANON_SAMPLE_FMT",
    "CANON_SR",
    "DEFAULT_CHARS_PER_SEC",
    "MAX_SPEEDUP",
    "JobPaths",
    "PaidPinViolation",
    "PathEscapeError",
    "ensure_within",
    "job_root",
    "pin_resolver",
    "safe_component",
    "write_manifest",
    "AsrProvider",
    "MtProvider",
    "ProviderInfo",
    "ProviderUnavailable",
    "Resolver",
    "TtsProvider",
    "TimingPlan",
    "align",
    "assign_timing",
    "ingest",
    "mux",
    "prepare",
    "run_pipeline",
    "transcribe",
    "translate",
    "tts",
    "__version__",
]
