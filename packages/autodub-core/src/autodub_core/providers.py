"""Provider seam (dependency inversion).

autodub-core is the kernel; it must NOT know which concrete providers exist or
which ones cost money. It declares here, structurally, only what each pipeline
stage needs from a provider, plus a ``Resolver`` interface for obtaining one.

The concrete adapters, the auto-ladder, the ``PAID_PROVIDERS`` set, and the
``select()`` triple-guard that enforces the paid-API red line all live in the
separate ``provider-adapters`` package (T1.2). These are ``typing.Protocol``s,
so that package satisfies them by shape — there is no import edge in either
direction, which keeps T1.1 and T1.2 independent (backlog DAG) and keeps the
kernel free of any paid-provider knowledge.
"""

from __future__ import annotations

from typing import Literal, Protocol, overload

from ovt_schemas.contracts import Transcript


class ProviderUnavailable(RuntimeError):
    """A selected/required provider (or local binary) is not configured."""


class ProviderInfo(Protocol):
    """The provider metadata the kernel reads (just the display name)."""

    name: str


class AsrProvider(Protocol):
    info: ProviderInfo

    def transcribe(self, audio_path: str, source_lang: str | None) -> Transcript: ...


class MtProvider(Protocol):
    info: ProviderInfo

    def translate(
        self,
        texts: list[str],
        source_lang: str,
        target_lang: str,
        budgets_ms: list[int] | None = None,
    ) -> list[str]: ...


class TtsProvider(Protocol):
    info: ProviderInfo
    # Raw-synthesis file extension hint (e.g. "mp3", "wav"). Part of the documented
    # seam; the tts stage falls back to "mp3" if an adapter omits it.
    ext: str

    def voices_for(self, lang: str) -> list[str]: ...

    def synthesize(self, text: str, voice_id: str, lang: str, out_path: str) -> str: ...


class Resolver(Protocol):
    """Resolves a provider for a capability. Implemented by ``provider-adapters``.

    Enforcing the paid-API red line (a paid provider is never auto-selected and
    requires explicit ``allow_paid``) is the resolver's job, not the kernel's.
    """

    @overload
    def select(
        self, kind: Literal["asr"], requested: str | None, allow_paid: bool
    ) -> AsrProvider: ...
    @overload
    def select(
        self, kind: Literal["mt"], requested: str | None, allow_paid: bool
    ) -> MtProvider: ...
    @overload
    def select(
        self, kind: Literal["tts"], requested: str | None, allow_paid: bool
    ) -> TtsProvider: ...
    def select(
        self, kind: str, requested: str | None, allow_paid: bool
    ) -> AsrProvider | MtProvider | TtsProvider: ...
