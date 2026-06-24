"""End-to-end local smoke: the full run_pipeline produces a playable dubbed
mp4 + an srt, using a synthetic ffmpeg-generated source and fake providers
(real sine clips for TTS). This is the T1.1 acceptance ("本地端到端出 mp4+srt").

Skipped when ffmpeg/ffprobe are not on PATH (e.g. CI without ffmpeg); the
deterministic timing/stitch/SRT behavior is locked by the ffmpeg-free golden
tests, so this smoke is an integration check rather than the unit gate.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from autodub_core import JobPaths, stages
from autodub_core import ffmpeg_utils as ff
from ovt_schemas.contracts import Transcript, TranscriptLine

pytestmark = pytest.mark.skipif(
    not (ff.have("ffmpeg") and ff.have("ffprobe")),
    reason="end-to-end smoke needs ffmpeg + ffprobe on PATH",
)


class _Info:
    def __init__(self, name: str) -> None:
        self.name = name


class _E2EAsr:
    info = _Info("e2e_asr")

    def transcribe(self, audio_path: str, source_lang: str | None) -> Transcript:
        return Transcript(
            source_language="en", asr_provider="e2e_asr",
            lines=[
                TranscriptLine(index=0, start_ms=0, end_ms=1500,
                               source_text="hello", words=[], speaker_id="SPEAKER_00"),
                TranscriptLine(index=1, start_ms=1500, end_ms=3000,
                               source_text="world", words=[], speaker_id="SPEAKER_00"),
            ],
        )


class _E2EMt:
    info = _Info("e2e_mt")

    def translate(self, texts: list[str], source_lang: str, target_lang: str,
                  budgets_ms: list[int] | None = None) -> list[str]:
        # translate the first line; leave the second empty to exercise the
        # keep_original (silence) align branch alongside the synthesized one
        return ["你好", ""]


class _E2ETts:
    info = _Info("e2e_tts")
    ext = "wav"

    def voices_for(self, lang: str) -> list[str]:
        return ["v1"]

    def synthesize(self, text: str, voice_id: str, lang: str, out_path: str) -> str:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=1.0", out_path],
            check=True, capture_output=True,
        )
        return out_path


class _E2EResolver:
    def __init__(self) -> None:
        self.asr = _E2EAsr()
        self.mt = _E2EMt()
        self.tts = _E2ETts()

    def select(self, kind: str, requested: str | None, allow_paid: bool):  # noqa: ARG002
        return {"asr": self.asr, "mt": self.mt, "tts": self.tts}[kind]


def _make_source_video(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=15",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-shortest", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )


def test_local_end_to_end_produces_mp4_and_srt(tmp_path: Path) -> None:
    src = tmp_path / "src.mp4"
    _make_source_video(src)

    paths = JobPaths(tmp_path / "job").ensure()
    out = stages.run_pipeline(paths, _E2EResolver(), source=str(src), target_lang="zh")

    assert out == paths.dubbed_video
    assert out.exists() and out.stat().st_size > 0
    assert paths.subtitles.exists()

    srt = paths.subtitles.read_text(encoding="utf-8")
    assert "-->" in srt
    assert "你好" in srt   # translated line
    assert "world" in srt  # empty target falls back to the source text

    # dubbed video keeps ~ the source duration (3s); both segments are composed in
    assert abs(ff.probe_duration_ms(out) - 3000) < 600
    assert paths.tts_aligned(0).exists()
    assert paths.tts_aligned(1).exists()
