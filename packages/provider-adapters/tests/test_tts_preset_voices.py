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


# ── list_tts_voices — rich, picker-facing enumeration (P1) ────────────────────
class _Info:
    def __init__(self, name: str, paid: bool = False) -> None:
        self.name = name
        self.paid = paid


def test_list_tts_voices_annotates_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    from provider_adapters import list_tts_voices

    monkeypatch.setattr(tts_mod, "probe", lambda _k: [
        ("piper", True, _Info("piper")),
        ("edge_tts", True, _Info("edge_tts")),
        ("elevenlabs", True, _Info("elevenlabs", paid=True)),  # paid -> excluded
        ("cloudflare", False, _Info("cloudflare")),            # unavailable -> excluded
    ])
    monkeypatch.setattr(tts_mod, "tts_preset_voices", lambda p, _l: {
        "piper": ["zh_CN-huayan-medium"],
        "edge_tts": ["zh-CN-YunxiNeural", "uncatalogued-voice"],
    }.get(p, []))
    monkeypatch.setattr(tts_mod, "_voice_catalog", lambda: {
        "piper": {"zh-Hans": [{"id": "zh_CN-huayan-medium", "gender": "female", "label": "HY"}]},
        "edge_tts": {"zh-Hans": [{"id": "zh-CN-YunxiNeural", "gender": "male", "label": "Yunxi"}]},
    })
    out = list_tts_voices("zh-Hans")
    # paid + unavailable providers are excluded
    assert {o["provider"] for o in out} == {"piper", "edge_tts"}
    piper = next(o for o in out if o["provider"] == "piper")
    assert piper == {"provider": "piper", "voice_id": "zh_CN-huayan-medium", "gender": "female",
                     "label": "HY", "commercial_safe": True, "experimental": False}
    yunxi = next(o for o in out if o["voice_id"] == "zh-CN-YunxiNeural")
    assert yunxi["gender"] == "male" and yunxi["experimental"] is True
    assert yunxi["commercial_safe"] is False  # edge is the non-commercial lane
    # an installed voice absent from the catalog is still offered (unknown gender, id as label)
    stray = next(o for o in out if o["voice_id"] == "uncatalogued-voice")
    assert stray["gender"] == "unknown" and stray["label"] == "uncatalogued-voice"


def test_shipped_catalog_cloudflare_ids_match_melotts_codes() -> None:
    # Guard: the shipped voices.json CF entries MUST use the MeloTTS lang code as the id, because
    # CloudflareTTS.voices_for now reads the catalog and passes the id straight to synthesize as the
    # `lang` param. A drift here would silently break CF dub routing. (en/es/fr/zh/jp/kr.)
    cat = tts_mod._voice_catalog()
    cf = cat["cloudflare"]
    assert cf["zh-Hans"][0]["id"] == "zh"
    assert cf["ja"][0]["id"] == "jp" and cf["ko"][0]["id"] == "kr"
    for loc in ("en", "es", "fr"):
        assert cf[loc][0]["id"] == loc
    # every CF locale lists exactly one voice (MeloTTS is single-voice-per-language)
    assert all(len(v) == 1 for v in cf.values())
