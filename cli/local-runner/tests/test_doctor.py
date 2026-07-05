"""T1.4 — ``ovt doctor`` preflight: provider availability + the supply-chain pin/license gate
(wiring the T1.3g mechanisms into a real caller). Test-first.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from local_runner.doctor import run_doctor
from provider_adapters import sha256_file
from provider_adapters import supply_chain as sc
from provider_adapters.supply_chain import PinnedArtifact


def test_doctor_reports_providers_and_supply_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FVD_PIPER_MODEL", raising=False)
    lines: list[str] = []
    assert run_doctor(out=lines.append) == 0
    text = "\n".join(lines)
    assert "[asr]" in text and "[mt]" in text and "[tts]" in text
    assert "piper model pin: skipped" in text
    assert "license[piper]: allowed" in text  # MIT


def test_doctor_verifies_a_correct_piper_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"\x00\x01piper")
    monkeypatch.setenv("FVD_PIPER_MODEL", str(model))
    monkeypatch.setenv("FVD_PIPER_MODEL_SHA256", sha256_file(str(model)))
    lines: list[str] = []
    assert run_doctor(out=lines.append) == 0
    assert any("piper model pin: OK" in ln for ln in lines)


def test_doctor_fails_on_a_bad_piper_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"real")
    monkeypatch.setenv("FVD_PIPER_MODEL", str(model))
    monkeypatch.setenv("FVD_PIPER_MODEL_SHA256", "a" * 64)  # operator pinned a different file
    lines: list[str] = []
    assert run_doctor(out=lines.append) == 1  # doctor reports a non-zero rc on a failed pin
    assert any("piper model pin: FAIL" in ln for ln in lines)


def test_doctor_reports_stale_piper_path_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # @CodeX CLI P2: FVD_PIPER_MODEL pointing at a moved/deleted file makes verify_piper_model
    # raise OSError, not SupplyChainError; doctor must REPORT a failed pin, not crash.
    monkeypatch.setenv("FVD_PIPER_MODEL", str(tmp_path / "gone.onnx"))  # does not exist
    monkeypatch.setenv("FVD_PIPER_MODEL_SHA256", "a" * 64)
    lines: list[str] = []
    assert run_doctor(out=lines.append) == 1
    assert any("piper model pin: FAIL" in ln for ln in lines)


def test_doctor_enforces_ffmpeg_pin_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    # @CodeX CLI P2: doctor is the supply-chain preflight, so it must enforce the ffmpeg binary
    # pin too — a wrong/absent pin reports FAIL (rc 1), not a silent pass.
    monkeypatch.delenv("FVD_PIPER_MODEL", raising=False)
    monkeypatch.setenv("FVD_FFMPEG_SHA256", "a" * 64)  # mismatch (or ffmpeg absent) -> FAIL
    lines: list[str] = []
    assert run_doctor(out=lines.append) == 1
    assert any("ffmpeg pin: FAIL" in ln for ln in lines)


def test_doctor_verifies_multivoice_dir_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # P1b F1: with FVD_PIPER_VOICES_DIR set, doctor verifies EVERY baked *.onnx pin (the
    # single-model FVD_PIPER_MODEL check no-ops in multi-voice mode).
    monkeypatch.delenv("FVD_PIPER_MODEL", raising=False)
    monkeypatch.delenv("FVD_FFMPEG_SHA256", raising=False)
    d = tmp_path / "voices"
    d.mkdir()
    ryan = d / "en_US-ryan-medium.onnx"
    ryan.write_bytes(b"\x00ryan-weights")
    monkeypatch.setenv("FVD_PIPER_VOICES_DIR", str(d))
    monkeypatch.setattr(sc, "_PINNED",
                        {"en_US-ryan-medium": PinnedArtifact(sha256_file(str(ryan)), "MIT")})
    lines: list[str] = []
    assert run_doctor(out=lines.append) == 0
    assert any("piper voices pin: OK" in ln for ln in lines)


def test_doctor_fails_on_unpinned_multivoice_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FVD_PIPER_MODEL", raising=False)
    d = tmp_path / "voices"
    d.mkdir()
    (d / "en_US-ryan-medium.onnx").write_bytes(b"\x00ryan-weights")
    monkeypatch.setenv("FVD_PIPER_VOICES_DIR", str(d))
    monkeypatch.setattr(sc, "_PINNED", {})  # no committed pin -> fail closed
    lines: list[str] = []
    assert run_doctor(out=lines.append) == 1
    assert any("piper voices pin: FAIL" in ln for ln in lines)
