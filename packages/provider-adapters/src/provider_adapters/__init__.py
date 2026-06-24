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
    Resolver,
    build_registry,
    has_binary,
    has_module,
    list_providers,
    probe,
    select,
)
from .ladder import AUTO_LADDER, PAID_PROVIDERS, is_paid_provider
from .languages import (
    CAPABILITIES,
    LanguageError,
    assert_language_pair,
    deepl_target_code,
    get_capability,
    resolve_source_language,
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
    "PAID_PROVIDERS",
    "REGISTRY",
    "LanguageError",
    "PaidProviderBlocked",
    "ProviderInfo",
    "ProviderUnavailable",
    "Resolver",
    "assert_language_pair",
    "deepl_target_code",
    "get_capability",
    "has_binary",
    "has_module",
    "is_paid_provider",
    "list_providers",
    "probe",
    "resolve_source_language",
    "select",
]
