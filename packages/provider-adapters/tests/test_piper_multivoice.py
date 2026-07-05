"""P1b: multi-voice PiperTTS (FVD_PIPER_VOICES_DIR). When a voices dir is set, Piper serves EVERY
installed catalog voice covering a locale (the picker's M/F choices) instead of the single
FVD_PIPER_MODEL. Availability is a FILE fact (an uninstalled entry is never offered). Must be
fully BACKWARD-COMPATIBLE: with no voices dir, behavior matches the legacy single-model path.

No piper binary is invoked — synthesis is proved elsewhere; here we test the routing/coverage/model-
resolution logic with fake .onnx files + a monkeypatched has_binary.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from provider_adapters import list_tts_voices
from provider_adapters import tts as tts_mod
from provider_adapters.base import ProviderUnavailable
from provider_adapters.tts import (
    PiperTTS,
    _piper_installed,
    _resolve_piper_model,
    piper_model_covers,
)


def _touch(dirpath: Path, *names: str) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    for n in names:
        (dirpath / n).write_bytes(b"\x00")


def _multivoice(monkeypatch: pytest.MonkeyPatch, voices_dir: Path) -> None:
    """Enter multi-voice mode: a real FVD_PIPER_VOICES_DIR + a piper binary; clear the legacy
    single-model env so the two modes never overlap in a test."""
    monkeypatch.setenv("FVD_PIPER_VOICES_DIR", str(voices_dir))
    monkeypatch.delenv("FVD_PIPER_MODEL", raising=False)
    monkeypatch.delenv("FVD_PIPER_LANG", raising=False)
    monkeypatch.setattr(tts_mod, "has_binary", lambda name: name == "piper")


# ── multi-voice mode ──────────────────────────────────────────────────────────
def test_voices_for_returns_installed_catalog_voices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Install BOTH en catalog voices (ryan M / amy F). voices_for(en) must return exactly them.
    _touch(tmp_path, "en_US-ryan-medium.onnx", "en_US-amy-medium.onnx")
    _multivoice(monkeypatch, tmp_path)
    assert set(PiperTTS().voices_for("en")) == {"en_US-ryan-medium", "en_US-amy-medium"}
    assert PiperTTS().voices_for("en-GB") == PiperTTS().voices_for("en")  # base-subtag fold


def test_voices_for_only_offers_installed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the MALE en model is on disk; the female catalog entry is NOT installed -> not offered.
    _touch(tmp_path, "en_US-ryan-medium.onnx")
    _multivoice(monkeypatch, tmp_path)
    assert PiperTTS().voices_for("en") == ["en_US-ryan-medium"]


def test_voices_for_raises_for_uninstalled_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # en installed, de NOT -> voices_for(de) fails closed (routing then picks another provider).
    _touch(tmp_path, "en_US-ryan-medium.onnx")
    _multivoice(monkeypatch, tmp_path)
    with pytest.raises(ProviderUnavailable):
        PiperTTS().voices_for("de")


def test_piper_model_covers_reflects_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _touch(tmp_path, "en_US-ryan-medium.onnx")
    _multivoice(monkeypatch, tmp_path)
    assert piper_model_covers("en") is True
    assert piper_model_covers("en-GB") is True  # base-subtag fold
    assert piper_model_covers("de") is False    # no de model installed
    assert _piper_installed("en") == ["en_US-ryan-medium"]


def test_available_requires_at_least_one_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    _multivoice(monkeypatch, empty)
    assert PiperTTS().available() is False  # dir exists but has no *.onnx
    _touch(empty, "en_US-ryan-medium.onnx")
    assert PiperTTS().available() is True


def test_resolve_model_maps_basename_to_dir_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _touch(tmp_path, "en_US-ryan-medium.onnx")
    _multivoice(monkeypatch, tmp_path)
    assert _resolve_piper_model("en_US-ryan-medium") == str(tmp_path / "en_US-ryan-medium.onnx")
    # a bare id not installed stays as-is (fails at synth, not silently mis-resolved)
    assert _resolve_piper_model("not-installed") == "not-installed"
    # a path (separator) is never joined to the dir — defense-in-depth against traversal
    assert _resolve_piper_model("/abs/model.onnx") == "/abs/model.onnx"


def test_list_tts_voices_surfaces_piper_mf_with_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end: the picker source shows both installed piper voices with catalog gender/label.
    _touch(tmp_path, "en_US-ryan-medium.onnx", "en_US-amy-medium.onnx")
    _multivoice(monkeypatch, tmp_path)
    voices = [v for v in list_tts_voices("en") if v["provider"] == "piper"]
    by_id = {v["voice_id"]: v for v in voices}
    assert by_id["en_US-ryan-medium"]["gender"] == "male"
    assert by_id["en_US-amy-medium"]["gender"] == "female"
    assert all(v["commercial_safe"] is True and v["experimental"] is False for v in voices)


# ── legacy single-model mode (backward compatibility — must be unchanged) ──────
def test_legacy_single_model_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"\x00")
    monkeypatch.delenv("FVD_PIPER_VOICES_DIR", raising=False)
    monkeypatch.setenv("FVD_PIPER_MODEL", str(model))
    monkeypatch.setenv("FVD_PIPER_LANG", "en")  # pins the generic-named model's language
    monkeypatch.setattr(tts_mod, "has_binary", lambda name: name == "piper")
    assert PiperTTS().available() is True
    assert PiperTTS().voices_for("en") == [str(model)]  # the single model path, as before
    assert piper_model_covers("en") is True
    assert piper_model_covers("de") is False  # FVD_PIPER_LANG pins en -> de not covered
    with pytest.raises(ProviderUnavailable):
        PiperTTS().voices_for("de")
    assert _resolve_piper_model(str(model)) == str(model)  # no voices dir -> id used as-is
