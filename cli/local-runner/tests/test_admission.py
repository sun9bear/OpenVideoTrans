"""T1.4 — job admission: the orchestration-layer fail-closed gates (wiring the T1.3f language
gate + the R9 commercial-safe dub enforcement into the real run path). Test-first.
"""
from __future__ import annotations

import pytest
from local_runner.admission import admit
from provider_adapters import LanguageError, ProviderUnavailable


class _FakeResolver:
    """A resolver whose only configured TTS providers are ``available``; others raise (mirrors
    provider_adapters.select for an explicit unavailable provider)."""

    def __init__(self, available_tts: set[str]) -> None:
        self._available = available_tts

    def select(self, kind: str, requested: str | None, allow_paid: bool):  # noqa: ANN201, ARG002
        if kind == "tts" and requested in self._available:
            return object()
        raise ProviderUnavailable(f"{requested!r} not configured")


def test_subtitle_only_admits_with_no_tts_provider() -> None:
    adm = admit(target_lang="es", output_mode="subtitle_only", resolver=_FakeResolver(set()))
    assert adm.tts_provider is None
    assert adm.source_lang == "auto"


def test_dub_picks_commercial_safe_piper() -> None:
    adm = admit(target_lang="es", output_mode="dub_only", resolver=_FakeResolver({"piper"}))
    assert adm.tts_provider == "piper"


def test_dub_picks_cloudflare_when_piper_absent() -> None:
    # es is covered by Cloudflare MeloTTS; with piper unconfigured, CF is the commercial-safe pick.
    adm = admit(target_lang="es", output_mode="both", resolver=_FakeResolver({"cloudflare"}))
    assert adm.tts_provider == "cloudflare"


def test_dub_refuses_edge_tts_only_host() -> None:
    # R9: edge_tts configured but no piper/CF -> a default dub must fail closed, NOT use edge_tts.
    with pytest.raises(LanguageError) as ei:
        admit(target_lang="es", output_mode="dub_only", resolver=_FakeResolver({"edge_tts"}))
    assert ei.value.code == "no_tts_model_for_language"


def test_dub_for_subtitle_only_locale_fails_closed() -> None:
    # hi has only an experimental edge_tts voice (tts_supported=False) -> dub fails at the gate.
    with pytest.raises(LanguageError) as ei:
        admit(target_lang="hi", output_mode="dub_only", resolver=_FakeResolver({"piper"}))
    assert ei.value.code == "no_tts_model_for_language"


def test_unsupported_target_fails_closed() -> None:
    with pytest.raises(LanguageError) as ei:
        admit(target_lang="tlh", output_mode="subtitle_only", resolver=_FakeResolver(set()))
    assert ei.value.code == "unsupported_language_pair"


def test_source_hint_is_resolved() -> None:
    adm = admit(target_lang="es", output_mode="subtitle_only", resolver=_FakeResolver(set()),
                source_hint="pt-BR")
    assert adm.source_lang == "pt-BR"
