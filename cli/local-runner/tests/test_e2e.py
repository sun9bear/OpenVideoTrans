"""T1.4 = M1 acceptance — one local command produces a MARKED mp4 + srt.

Drives the CLI orchestration (``run_job``) end-to-end with fake providers (real sine clips for
TTS) over an ffmpeg-generated source, exercising the real admission gate (which selects the
commercial-safe ``piper`` voice) + the kernel pipeline + AIGC marking (default-on). Skipped when
ffmpeg/ffprobe are absent — the deterministic behavior is locked by autodub-core's ffmpeg-free
golden tests; this is the integration milestone check.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from autodub_core import JobPaths
from autodub_core import ffmpeg_utils as ff
from local_runner.runner import run_job
from ovt_schemas.contracts import Transcript, TranscriptLine

pytestmark = pytest.mark.skipif(
    not (ff.have("ffmpeg") and ff.have("ffprobe")),
    reason="end-to-end smoke needs ffmpeg + ffprobe on PATH",
)


class _Info:
    def __init__(self, name: str) -> None:
        self.name = name


class _Asr:
    info = _Info("e2e_asr")

    def transcribe(self, audio_path: str, source_lang: str | None) -> Transcript:  # noqa: ARG002
        return Transcript(
            source_language="en", asr_provider="e2e_asr",
            lines=[TranscriptLine(index=0, start_ms=0, end_ms=1500, source_text="hello",
                                  words=[], speaker_id="SPEAKER_00")],
        )


class _Mt:
    info = _Info("e2e_mt")

    def translate(self, texts: list[str], source_lang: str, target_lang: str,  # noqa: ARG002
                  budgets_ms: list[int] | None = None) -> list[str]:
        return ["你好"]


class _Tts:
    info = _Info("piper")  # the admission picks "piper" (commercial-safe); the kernel resolves it
    ext = "wav"

    def voices_for(self, lang: str) -> list[str]:  # noqa: ARG002
        return ["v1"]

    def synthesize(self, text: str, voice_id: str, lang: str, out_path: str) -> str:  # noqa: ARG002
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=1.0",
                        out_path], check=True, capture_output=True)
        return out_path


class _Resolver:
    def __init__(self) -> None:
        self.asr, self.mt, self.tts = _Asr(), _Mt(), _Tts()

    def select(self, kind: str, requested: str | None, allow_paid: bool):  # noqa: ARG002, ANN201
        return {"asr": self.asr, "mt": self.mt, "tts": self.tts}[kind]


def _make_source_video(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=15",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest", "-pix_fmt",
         "yuv420p", str(path)],
        check=True, capture_output=True,
    )


def test_cli_run_produces_marked_mp4_and_srt(tmp_path: Path) -> None:
    src = tmp_path / "src.mp4"
    _make_source_video(src)
    job_dir = tmp_path / "job"

    result = run_job(source=str(src), target_lang="zh-Hans", out_dir=str(job_dir),
                     output_mode="both", resolver=_Resolver())

    # admission chose the commercial-safe dub voice (not edge_tts)
    assert result.admission.tts_provider == "piper"
    # the deliverable is a real dubbed mp4
    assert result.primary == JobPaths(job_dir).dubbed_video
    assert result.primary.exists() and result.primary.stat().st_size > 0

    paths = JobPaths(job_dir)
    srt = paths.subtitles.read_text(encoding="utf-8")
    assert "-->" in srt and "你好" in srt
    # MARKED: the machine-translation disclosure rides the subtitle (AIGC marking default-on)
    assert "本字幕由机器翻译生成" in srt
    # MARKED: the dubbed mp4 carries the AIGC voice-mark in its container comment metadata
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags=comment",
         "-of", "default=nw=1:nk=1", str(result.primary)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert "AIGC" in probe.stdout


def test_run_job_subtitle_only_emits_srt_no_video(tmp_path: Path) -> None:
    src = tmp_path / "src.mp4"
    _make_source_video(src)
    job_dir = tmp_path / "job"
    result = run_job(source=str(src), target_lang="zh-Hans", out_dir=str(job_dir),
                     output_mode="subtitle_only", resolver=_Resolver())
    assert result.admission.tts_provider is None
    assert result.primary == JobPaths(job_dir).subtitles
    assert result.primary.exists()
    assert not JobPaths(job_dir).dubbed_video.exists()
