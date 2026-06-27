"""provider-adapters — concrete ASR/MT/TTS adapters + the paid-API safety gate.

Importing this package registers every adapter and exposes the paid-safety surface
the red-line invariants (plan §5) and the kernel's ``Resolver`` seam depend on:
``PAID_PROVIDERS`` / ``AUTO_LADDER`` / ``REGISTRY`` / ``is_paid_provider`` /
``select`` (triple guard) / ``PaidProviderBlocked``.

autodub-core never imports this package (and this package never imports autodub-core):
the seam is structural (``Resolver`` satisfies the kernel's Protocol by shape), which
keeps T1.1 ∥ T1.2 (backlog DAG) and keeps the kernel free of paid-provider knowledge.
"""

from __future__ import annotations

from . import asr, mt, tts
from .base import (
    PaidProviderBlocked,
    ProviderInfo,
    ProviderUnavailable,
    QuotaExhausted,
    Resolver,
    build_registry,
    has_binary,
    has_module,
    list_providers,
    probe,
    select,
)
from .circuit import (
    FREE_LADDER_PROVIDERS,
    FREE_POOL_EXHAUSTED,
    NO_FREE_PROVIDER,
    ProviderAvailability,
    ProviderFailure,
    ProviderResult,
    configured_free_providers,
    route_free,
)
from .ladder import AUTO_LADDER, PAID_PROVIDERS, is_paid_provider
from .languages import (
    CAPABILITIES,
    COMMERCIAL_SAFE_TTS,
    LanguageError,
    assert_language_pair,
    deepl_target_code,
    get_capability,
    resolve_source_language,
)
from .supply_chain import (
    SupplyChainError,
    assert_default_image_allowed,
    is_non_commercial,
    license_for,
    sha256_file,
    verify_ffmpeg,
    verify_pinned,
    verify_piper_model,
    verify_sha256,
)

# Registering all adapters populates the kind→name→factory registry that select()
# walks; REGISTRY is then flattened from it (name→ProviderInfo).
asr.register_all()
mt.register_all()
tts.register_all()

REGISTRY: dict[str, ProviderInfo] = build_registry()

__all__ = [
    "AUTO_LADDER",
    "CAPABILITIES",
    "COMMERCIAL_SAFE_TTS",
    "FREE_LADDER_PROVIDERS",
    "FREE_POOL_EXHAUSTED",
    "NO_FREE_PROVIDER",
    "PAID_PROVIDERS",
    "REGISTRY",
    "LanguageError",
    "PaidProviderBlocked",
    "ProviderAvailability",
    "ProviderFailure",
    "ProviderInfo",
    "ProviderResult",
    "ProviderUnavailable",
    "QuotaExhausted",
    "Resolver",
    "SupplyChainError",
    "assert_default_image_allowed",
    "assert_language_pair",
    "configured_free_providers",
    "deepl_target_code",
    "get_capability",
    "has_binary",
    "has_module",
    "is_non_commercial",
    "is_paid_provider",
    "route_free",
    "license_for",
    "list_providers",
    "probe",
    "resolve_source_language",
    "select",
    "sha256_file",
    "verify_ffmpeg",
    "verify_pinned",
    "verify_piper_model",
    "verify_sha256",
]
