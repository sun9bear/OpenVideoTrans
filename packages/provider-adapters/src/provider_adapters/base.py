"""Provider registry, the $0 auto-ladder selector, and the paid-API safety gate.

This is the concrete side of the seam that ``autodub-core`` declares only
structurally (``autodub_core.providers`` Protocols): the kernel never imports this
package and never knows which providers cost money. The paid-API red line
(CLAUDE.md §1/§14 — a paid API is NEVER auto-invoked, ``allow_paid`` is a constant
``False`` from the kernel) is enforced HERE, in ``select()``'s triple guard.

There is no import edge in either direction between this package and autodub-core,
which keeps T1.1 ∥ T1.2 (backlog DAG); compatibility with the kernel's ``Resolver``
Protocol is by shape (the ``Resolver`` class below).
"""

from __future__ import annotations

import importlib.util
import shutil
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from ovt_schemas.contracts import Transcript

from .ladder import AUTO_LADDER, is_paid_provider


@dataclass(frozen=True)
class ProviderInfo:
    """Static metadata for one adapter. ``paid`` is the authoritative per-provider
    flag the red-line invariants check against ``PAID_PROVIDERS`` membership."""

    name: str
    kind: str  # 'asr' | 'mt' | 'tts'
    paid: bool
    requires: str  # human-readable: what must be present (env var / pip pkg / binary)
    languages: str
    notes: str = ""


class ProviderUnavailable(RuntimeError):
    """A selected/required provider (or its key/binary) is not configured."""


class PaidProviderBlocked(RuntimeError):
    """A paid provider was requested without explicit ``allow_paid`` (red line §1/§14)."""


def has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def has_binary(name: str) -> bool:
    return shutil.which(name) is not None


# --------------------------------------------------------------------------- #
# Capability interfaces
# --------------------------------------------------------------------------- #
class _BaseProvider(ABC):
    info: ProviderInfo

    @property
    def name(self) -> str:
        """Convenience alias for ``info.name`` (the red-line invariants read ``.name``
        on the resolved provider; the kernel reads ``.info.name``)."""
        return self.info.name

    @abstractmethod
    def available(self) -> bool:
        """True when this provider's key/binary is present and it can run."""


class ASRProvider(_BaseProvider):
    @abstractmethod
    def transcribe(self, audio_path: str, source_lang: str | None) -> Transcript: ...


class MTProvider(_BaseProvider):
    @abstractmethod
    def translate(
        self,
        texts: list[str],
        source_lang: str,
        target_lang: str,
        budgets_ms: list[int] | None = None,
    ) -> list[str]: ...


class TTSProvider(_BaseProvider):
    # Raw-synthesis file extension hint (e.g. "mp3", "wav"); the kernel's tts stage
    # falls back to "mp3" if an adapter omits it.
    ext: str = "mp3"

    @abstractmethod
    def voices_for(self, lang: str) -> list[str]:
        """Preset voice ids appropriate for the target language."""

    @abstractmethod
    def synthesize(self, text: str, voice_id: str, lang: str, out_path: str) -> str:
        """Write synthesized audio to a file and return the actual path written
        (the extension may differ from out_path, e.g. .mp3)."""


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
Factory = Callable[[], _BaseProvider]
_REGISTRY: dict[str, dict[str, Factory]] = {"asr": {}, "mt": {}, "tts": {}}


def register(kind: str, name: str, factory: Factory) -> None:
    _REGISTRY[kind][name] = factory


def _build(kind: str, name: str) -> _BaseProvider:
    factory = _REGISTRY[kind].get(name)
    if factory is None:
        raise ProviderUnavailable(f"unknown {kind} provider: {name!r}")
    return factory()


def list_providers(kind: str) -> list[str]:
    return list(_REGISTRY[kind].keys())


def probe(kind: str) -> list[tuple[str, bool, ProviderInfo]]:
    """(name, available, info) for every registered provider of a kind — the
    primitive a ``doctor``/diagnostics command (T1.4 CLI) renders."""
    rows: list[tuple[str, bool, ProviderInfo]] = []
    for name in list_providers(kind):
        prov = _build(kind, name)
        try:
            avail = bool(prov.available())
        except Exception:  # noqa: BLE001 - availability probing must never crash doctor
            avail = False
        rows.append((name, avail, prov.info))
    return rows


def build_registry() -> dict[str, ProviderInfo]:
    """Flatten kind→name→factory into the public name→ProviderInfo map.

    A provider name (e.g. 'cloudflare') can serve several kinds; its ``paid`` flag
    is invariant across them (asserted here), so a single name→info entry is well
    defined for the red-line membership check (inv①)."""
    registry: dict[str, ProviderInfo] = {}
    for kind in _REGISTRY:
        for name in _REGISTRY[kind]:
            info = _build(kind, name).info
            existing = registry.get(name)
            if existing is not None and existing.paid != info.paid:
                raise AssertionError(
                    f"provider {name!r} has an inconsistent paid flag across kinds"
                )
            registry.setdefault(name, info)
    return registry


# --------------------------------------------------------------------------- #
# Selector — the paid-API triple guard
# --------------------------------------------------------------------------- #
def _blocked_msg(name: str) -> str:
    return (
        f"{name!r} is a paid provider. Paid APIs are never invoked automatically "
        f"(hard safety rule — CLAUDE.md §1/§14). Pick a free provider, or use an "
        f"explicit, user-initiated opt-in to authorise billing."
    )


def select(kind: str, requested: str | None, allow_paid: bool) -> _BaseProvider:
    """Resolve a provider for ``kind`` ('asr' | 'mt' | 'tts').

    Triple guard on the paid-API red line:
      ① string-level — a name in ``PAID_PROVIDERS`` is blocked BEFORE any factory
         build, so even a paid name with no registered adapter raises
         ``PaidProviderBlocked`` (covers 'backend'/'deepgram'/… — plan §5 ④/⑤).
      ② instance-level — the built provider's own ``info.paid`` is re-checked, in
         case a paid provider is ever registered without updating ``PAID_PROVIDERS``.
      ③ ladder-level — the AUTO ladder skips any paid provider defensively, so an
         auto-select can never return paid even if a paid name leaks into a ladder.

    ``allow_paid`` arrives from the kernel as a constant ``False`` (the kernel has
    no opt-in); a ``True`` value is only ever reachable from a future explicit,
    user-initiated BYOK path — never from a fallback / batch / retry.
    """
    if requested:
        if is_paid_provider(requested) and not allow_paid:  # ①
            raise PaidProviderBlocked(_blocked_msg(requested))
        prov = _build(kind, requested)
        if prov.info.paid and not allow_paid:  # ②
            raise PaidProviderBlocked(_blocked_msg(requested))
        if not prov.available():
            raise ProviderUnavailable(
                f"{requested!r} selected but not configured: needs {prov.info.requires}."
            )
        return prov

    # AUTO mode: walk the $0 ladder and return the first AVAILABLE free provider.
    free_default: _BaseProvider | None = None
    for name in AUTO_LADDER[kind]:
        if name not in _REGISTRY[kind]:
            continue
        prov = _build(kind, name)
        if is_paid_provider(name) or prov.info.paid:  # ③ defensive: never auto-paid
            continue
        if free_default is None:
            free_default = prov  # remember the ladder's default free provider
        if prov.available():
            return prov
    if free_default is not None:
        # Nothing is configured yet: hand back the kind's DEFAULT free provider
        # (ladder head) — never paid. The caller receives a concrete provider and a
        # precise "needs <setup>" error when it actually calls it, rather than a
        # generic "no provider configured". Deliberate and loud-at-use, never a
        # silent skip and never a paid auto-call.
        return free_default
    raise ProviderUnavailable(f"no free {kind} provider is registered")


class Resolver:
    """Concrete resolver injected into the autodub-core kernel.

    Structurally satisfies ``autodub_core.providers.Resolver`` — there is no import
    edge in either direction; the kernel depends on the Protocol shape only."""

    def select(self, kind: str, requested: str | None, allow_paid: bool) -> _BaseProvider:
        return select(kind, requested, allow_paid)
