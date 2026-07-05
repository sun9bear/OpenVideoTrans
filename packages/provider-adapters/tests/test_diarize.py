"""Tests for the sherpa-onnx speaker diarization adapter (P4b).

Covers the PURE, deterministic contract (label format, seconds->ms rounding, span filtering, sort
order), the availability gate, and the diarize() wiring — all WITHOUT sherpa-onnx or a real ONNX
model installed. The model-integration path is validated on the target box at P4b-bake. numpy is
not a declared dependency, so the wav-reading tests are importorskip-guarded.
"""

from __future__ import annotations

import struct
import wave
from dataclasses import dataclass

import pytest
from provider_adapters import build_diarizer
from provider_adapters.base import ProviderUnavailable
from provider_adapters.diarize import (
    SherpaOnnxDiarizer,
    _read_wav_mono_f32,
    _segments_to_turns,
    _speaker_label,
)


@dataclass
class _Seg:
    """Duck-types a sherpa OfflineSpeakerDiarizationSegment (start/end seconds, speaker int)."""

    start: float
    end: float
    speaker: int


def test_speaker_label_matches_asr_baseline():
    assert _speaker_label(0) == "SPEAKER_00"  # identical to the ASR single-speaker baseline
    assert _speaker_label(1) == "SPEAKER_01"
    assert _speaker_label(12) == "SPEAKER_12"


def test_segments_to_turns_rounds_sorts_and_labels():
    segs = [
        _Seg(1.2, 3.4, 1),
        _Seg(0.0, 1.2, 0),
        _Seg(3.4, 3.9004, 0),
    ]
    assert _segments_to_turns(segs) == [
        (0, 1200, "SPEAKER_00"),
        (1200, 3400, "SPEAKER_01"),
        (3400, 3900, "SPEAKER_00"),
    ]


def test_segments_to_turns_drops_nonpositive_spans():
    segs = [_Seg(1.0, 1.0, 0), _Seg(2.0, 1.9, 1), _Seg(2.0, 2.5, 2)]
    assert _segments_to_turns(segs) == [(2000, 2500, "SPEAKER_02")]


def test_segments_to_turns_empty():
    assert _segments_to_turns([]) == []


def test_single_speaker_reproduces_baseline_label():
    # A 1-speaker diarization labels everything SPEAKER_00, so enabling diarization on
    # single-speaker audio stays byte-identical to today (the diarize stage's no-op contract).
    turns = _segments_to_turns([_Seg(0.0, 5.0, 0), _Seg(6.0, 8.0, 0)])
    assert {t[2] for t in turns} == {"SPEAKER_00"}


def test_unavailable_without_models(monkeypatch):
    # No env + no explicit models -> not available; diarize() fails loud rather than half-running.
    for var in (
        "FVD_DIARIZE_SEGMENTATION_MODEL",
        "FVD_DIARIZE_EMBEDDING_MODEL",
        "FVD_DIARIZE_NUM_SPEAKERS",
        "FVD_DIARIZE_CLUSTER_THRESHOLD",
        "FVD_DIARIZE_NUM_THREADS",
    ):
        monkeypatch.delenv(var, raising=False)
    d = build_diarizer()
    assert isinstance(d, SherpaOnnxDiarizer)
    assert d.available() is False
    with pytest.raises(ProviderUnavailable):
        d.diarize("nonexistent.wav")


def test_available_requires_wheel_and_both_models(tmp_path, monkeypatch):
    seg = tmp_path / "seg.onnx"
    seg.write_bytes(b"x")
    emb = tmp_path / "emb.onnx"
    emb.write_bytes(b"x")
    d = build_diarizer(segmentation_model=str(seg), embedding_model=str(emb))
    # Wheel present + both files -> available.
    monkeypatch.setattr("provider_adapters.diarize.has_module", lambda name: True)
    assert d.available() is True
    # Wheel absent -> not available, even with both model files present.
    monkeypatch.setattr("provider_adapters.diarize.has_module", lambda name: False)
    assert d.available() is False
    # A missing model file also fails the gate (wheel present).
    d2 = build_diarizer(
        segmentation_model=str(seg), embedding_model=str(tmp_path / "missing.onnx")
    )
    monkeypatch.setattr("provider_adapters.diarize.has_module", lambda name: True)
    assert d2.available() is False


def test_diarize_wiring_without_sherpa(monkeypatch):
    # End-to-end wiring (ensure_available -> build_sd -> read wav -> process -> segments_to_turns)
    # with sherpa + numpy stubbed, so it runs in CI where neither the wheel nor a model exists.
    class _FakeResult:
        def sort_by_start_time(self):
            return [_Seg(0.0, 1.0, 0), _Seg(1.0, 2.0, 1)]

    class _FakeSD:
        sample_rate = 16000

        def process(self, samples):
            assert samples == "SAMPLES"  # got exactly what _read_wav_mono_f32 returned
            return _FakeResult()

    def _fake_read(path, sr):
        assert sr == 16000  # diarize() must pass the sd's sample_rate through
        return "SAMPLES"

    d = build_diarizer(segmentation_model="s", embedding_model="e")
    monkeypatch.setattr(d, "_ensure_available", lambda: None)
    monkeypatch.setattr(d, "_build_sd", lambda: _FakeSD())
    monkeypatch.setattr("provider_adapters.diarize._read_wav_mono_f32", _fake_read)
    assert d.diarize("ignored.wav") == [(0, 1000, "SPEAKER_00"), (1000, 2000, "SPEAKER_01")]


def test_read_wav_mono_f32(tmp_path):
    np = pytest.importorskip("numpy")
    path = tmp_path / "speech.wav"
    samples = [0, 16384, -16384, 32767, -32768]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", s) for s in samples))
    arr = _read_wav_mono_f32(str(path), 16000)
    assert arr.dtype == np.float32
    assert arr.shape == (5,)
    assert arr.max() <= 1.0
    assert arr.min() >= -1.0
    assert abs(float(arr[0])) < 1e-6  # 0 -> 0.0


def test_read_wav_rejects_sample_rate_mismatch(tmp_path):
    pytest.importorskip("numpy")
    path = tmp_path / "speech8k.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<h", 100))
    with pytest.raises(ProviderUnavailable):
        _read_wav_mono_f32(str(path), 16000)


def test_read_wav_rejects_unsupported_sample_width(tmp_path):
    pytest.importorskip("numpy")
    path = tmp_path / "speech8bit.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)  # 8-bit unsigned PCM: not produced by the kernel -> rejected
        w.setframerate(16000)
        w.writeframes(b"\x80\x80\x80")
    with pytest.raises(ProviderUnavailable):
        _read_wav_mono_f32(str(path), 16000)
