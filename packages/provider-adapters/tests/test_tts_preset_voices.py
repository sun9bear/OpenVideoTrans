"""Direct unit tests for `tts_preset_voices` — the CLOSED free-preset membership source the worker's
soft-pin validation and the (P1) picker endpoint both consume. The worker tests monkeypatch it out,
so its own logic (unknown/unavailable -> [], exception-swallowing, isinstance narrowing, delegation
to voices_for) is proven HERE (open-core guardrail, plan §4)."""
from __future__ import annotations

import pytest
from provider_adapters import tts as tts_mod
from provider_adapters import tts_preset_voices
from provider_adapters.base import ProviderInfo, TTSProvider


class _FakeTts(TTSProvider):
    info = ProviderInfo("faketts", "tts", paid=False, requires="", languages="")

    def available(self) -> bool:
        return True

    def voices_for(self, lang: str) -> list[str]:
        return ["v-one", "v-two"]

    def synthesize(self, text: str, voice_id: str, lang: str, out_path: str) -> str:
        return out_path


def test_unknown_provider_returns_empty() -> None:
    # _build raises ProviderUnavailable for an unknown/crafted name -> caught -> [] (fails closed).
    assert tts_preset_voices("does_not_exist", "en") == []
    assert tts_preset_voices("../evil", "en") == []


def test_swallows_errors_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(kind: str, name: str) -> object:
        raise RuntimeError("registry blew up")
    monkeypatch.setattr(tts_mod, "_build", boom)
    assert tts_preset_voices("edge_tts", "en") == []  # membership source must never crash caller


def test_non_tts_provider_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defensive isinstance narrowing: a build result that is not a TTSProvider yields [] (never an
    # AttributeError on .voices_for).
    monkeypatch.setattr(tts_mod, "_build", lambda _k, _n: object())
    assert tts_preset_voices("whatever", "en") == []


def test_delegates_to_voices_for(monkeypatch: pytest.MonkeyPatch) -> None:
    # The closed set is exactly what the provider's own voices_for returns (matches synthesis).
    monkeypatch.setattr(tts_mod, "_build", lambda _k, _n: _FakeTts())
    assert tts_preset_voices("faketts", "en") == ["v-one", "v-two"]
