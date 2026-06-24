"""Job admission — the orchestration-layer gates the kernel cannot run itself.

autodub-core (the kernel) has no import edge to provider-adapters, so the language-capability
gate (T1.3f) and the commercial-safe TTS enforcement run HERE, before the pipeline starts. This
is where the fail-closed checks that provider-adapters only *exposes* actually fire (wiring the
@CodeX review findings that flagged the gate as un-enforced in the kernel):

  - ``assert_language_pair`` (T1.3f): an unsupported target / unavailable subtitle / no-dub-voice
    locale fails closed with the schema ``ErrorCode``.
  - commercial-safe dub TTS (R9): a DEFAULT dub output must use a license-vetted voice
    (piper / Cloudflare MeloTTS), never the non-commercial experimental ``edge_tts`` lane. If no
    commercial-safe voice is *configured*, the job fails ``no_tts_model_for_language`` rather than
    silently dubbing with a disallowed fallback.
"""

from __future__ import annotations

from dataclasses import dataclass

from provider_adapters import (
    LanguageError,
    ProviderUnavailable,
    assert_language_pair,
    resolve_source_language,
)

# Commercial-safe TTS for a DEFAULT dub (mirrors provider_adapters.languages._COMMERCIAL_SAFE_TTS):
# piper (per-model license-checked, T1.3g) + Cloudflare MeloTTS. edge_tts stays experimental.
_COMMERCIAL_SAFE_TTS = ("piper", "cloudflare")
_DUB_MODES = ("dub_only", "both")


@dataclass(frozen=True)
class Admission:
    """The outcome of admitting a job: the resolved source language and the chosen dub TTS
    provider (``None`` for a subtitle-only job)."""

    source_lang: str
    tts_provider: str | None


def admit(
    *,
    target_lang: str,
    output_mode: str,
    resolver: object,
    source_hint: str | None = None,
) -> Admission:
    """Validate a job's language pair and choose its dub TTS provider. ``resolver`` is anything
    with a ``select(kind, requested, allow_paid)`` method (the provider-adapters ``Resolver``).

    Raises ``LanguageError`` (fail-closed) on an unsupported pair, or when a dub is requested but
    no commercial-safe TTS voice is configured."""
    cap = assert_language_pair(source_hint, target_lang, output_mode)  # T1.3f capability gate
    source_lang = resolve_source_language(source_hint)
    if output_mode not in _DUB_MODES:
        return Admission(source_lang=source_lang, tts_provider=None)
    # Dub: choose the first commercial-safe provider the registry vets for this locale AND that is
    # actually configured. Never fall through to edge_tts (experimental) for a default dub (R9).
    for name in _COMMERCIAL_SAFE_TTS:
        if name not in cap.tts_models:
            continue
        try:
            resolver.select("tts", name, allow_paid=False)  # type: ignore[attr-defined]
        except ProviderUnavailable:
            continue  # vetted for this locale but not configured on this host — try the next
        return Admission(source_lang=source_lang, tts_provider=name)
    raise LanguageError(
        "no_tts_model_for_language",
        f"no commercial-safe TTS provider for {target_lang!r} is configured (edge_tts is an "
        f"experimental lane, not a default dub output); configure piper or Cloudflare, or use "
        f"output_mode=subtitle_only",
    )
