"""T1.3g — supply-chain integrity pin (sha256) + non-commercial model license gate.
Test-first (backlog T1.3g acceptance: sha256 校验 + 非商用模型禁入).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from provider_adapters import (
    SupplyChainError,
    assert_default_image_allowed,
    is_non_commercial,
    license_for,
    sha256_file,
    verify_sha256,
)
from provider_adapters.supply_chain import verify_piper_model


# ── sha256 integrity pin ─────────────────────────────────────────────────────
def test_verify_sha256_passes_on_match_and_returns_digest(tmp_path: Path) -> None:
    f = tmp_path / "model.onnx"
    f.write_bytes(b"voice-weights")
    digest = sha256_file(str(f))
    assert verify_sha256(str(f), digest) == digest
    assert verify_sha256(str(f), digest.upper()) == digest  # case-insensitive


def test_verify_sha256_raises_on_mismatch(tmp_path: Path) -> None:
    f = tmp_path / "model.onnx"
    f.write_bytes(b"voice-weights")
    with pytest.raises(SupplyChainError, match="sha256 mismatch"):
        verify_sha256(str(f), "0" * 64)  # tampered / wrong download


def test_verify_pinned_uses_env_override_and_emits_modelref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "voice.onnx"
    f.write_bytes(b"\x00\x01\x02piper")
    monkeypatch.setenv("FVD_PIPER_MODEL_SHA256", sha256_file(str(f)))
    ref = verify_piper_model(str(f))
    assert ref.name == "piper_model" and ref.sha256 == sha256_file(str(f))


def test_verify_pinned_fails_closed_when_unpinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No curated pin and no FVD_*_SHA256 override -> refuse (no "run whatever's on disk" path).
    monkeypatch.delenv("FVD_PIPER_MODEL_SHA256", raising=False)
    f = tmp_path / "voice.onnx"
    f.write_bytes(b"unpinned")
    with pytest.raises(SupplyChainError, match="not pinned"):
        verify_piper_model(str(f))


def test_verify_pinned_raises_on_wrong_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "voice.onnx"
    f.write_bytes(b"real")
    monkeypatch.setenv("FVD_PIPER_MODEL_SHA256", "a" * 64)  # operator pinned a different file
    with pytest.raises(SupplyChainError, match="sha256 mismatch"):
        verify_piper_model(str(f))


# ── non-commercial model license gate ────────────────────────────────────────
def test_permissive_model_allowed_in_default_image() -> None:
    assert license_for("piper") == "MIT"
    assert_default_image_allowed("piper")  # MIT: no raise
    assert_default_image_allowed("whisper")


def test_non_commercial_model_blocked_from_default_image() -> None:
    # XTTS / F5-TTS / OpenVoice (CC-BY-NC family) are 禁入默认镜像 unless audited-acknowledged.
    for model in ("xtts", "xtts-v2", "f5-tts", "openvoice"):
        with pytest.raises(SupplyChainError, match="non-commercial"):
            assert_default_image_allowed(model)
        assert_default_image_allowed(model, acknowledged=True)  # audited opt-in passes


def test_unvetted_model_fails_closed() -> None:
    with pytest.raises(SupplyChainError, match="not license-vetted"):
        assert_default_image_allowed("some-random-model")


def test_is_non_commercial_classification() -> None:
    assert is_non_commercial("coqui-cpml")
    assert is_non_commercial("CC-BY-NC-4.0")
    assert is_non_commercial("cc-by-nc-sa")
    assert not is_non_commercial("MIT")
    assert not is_non_commercial("apache-2.0")
