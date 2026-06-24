"""T1.3d — output-mode conditional pipeline. Test-first: subtitle_only skips
tts + align and emits only an srt; bilingual subtitles carry two lines; dub_only
emits a dubbed video and no srt; burned delivery is a guarded M2.1 placeholder.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from autodub_core import JobPaths, stages
from autodub_core.jsonio import write_json
from ovt_schemas.contracts import (
    DubbingSegment,
    Transcript,
    TranscriptLine,
    TranslationResult,
)


def _segments(pairs: list[tuple[str, str]]) -> TranslationResult:
    segs = [
        DubbingSegment(
            segment_id=f"seg_{i:04d}", index=i, speaker_id="SPEAKER_00",
            start_ms=i * 1000, end_ms=i * 1000 + 1000, target_duration_ms=1000,
            source_text=s, target_text=t, keep_original=(not t),
        )
        for i, (s, t) in enumerate(pairs)
    ]
    return TranslationResult(
        source_language="en", target_language="zh", mt_provider="m", segments=segs
    )


def _write_segments(paths: JobPaths, pairs: list[tuple[str, str]]) -> None:
    write_json(paths.segments, _segments(pairs).model_dump())


def _mock_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stages.ff, "assert_ffmpeg", lambda: None)
    monkeypatch.setattr(stages.ff, "probe_duration_ms", lambda p: 1000)  # noqa: ARG005
    monkeypatch.setattr(stages.ff, "stitch_timeline",
                        lambda placements, out, total: Path(out).write_bytes(b"a"))  # noqa: ARG005
    monkeypatch.setattr(stages.ff, "mux",
                        lambda v, a, o, ambient=None, metadata=None: Path(o).write_bytes(b"mp4"))  # noqa: ARG005, E501


# --------------------------------------------------------------------------- #
# subtitle_only / bilingual (no ffmpeg needed)
# --------------------------------------------------------------------------- #
def test_mux_subtitle_only_writes_srt_only(tmp_path: Path) -> None:
    paths = JobPaths(tmp_path).ensure()
    _write_segments(paths, [("hello", "你好")])
    out = stages.mux(paths, output_mode="subtitle_only")
    assert out == paths.subtitles
    assert paths.subtitles.exists()
    assert not paths.dubbed_video.exists()
    srt = paths.subtitles.read_text(encoding="utf-8")
    assert "你好" in srt
    assert "hello" not in srt  # target-only subtitle does not show the source


def test_mux_subtitle_only_bilingual_has_target_and_source_lines(tmp_path: Path) -> None:
    paths = JobPaths(tmp_path).ensure()
    _write_segments(paths, [("hello", "你好")])
    stages.mux(paths, output_mode="subtitle_only", subtitle_lang="bilingual")
    srt = paths.subtitles.read_text(encoding="utf-8")
    block = srt.split("\n\n")[0].splitlines()  # [index, timing, target, source]
    assert block[2] == "你好"  # target on top (the deliverable language)
    assert block[3] == "hello"  # source below


def test_mux_bilingual_falls_back_to_one_line_when_no_target(tmp_path: Path) -> None:
    # keep_original (empty target): even bilingual emits a single source line.
    paths = JobPaths(tmp_path).ensure()
    _write_segments(paths, [("hello", "")])
    stages.mux(paths, output_mode="subtitle_only", subtitle_lang="bilingual")
    block = paths.subtitles.read_text(encoding="utf-8").split("\n\n")[0]
    assert block.splitlines()[2:] == ["hello"]


# --------------------------------------------------------------------------- #
# dub_only / both / burned placeholder (ffmpeg mocked)
# --------------------------------------------------------------------------- #
def test_mux_dub_only_produces_video_and_no_srt(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    _mock_ffmpeg(monkeypatch)
    out = stages.mux(paths, output_mode="dub_only")
    assert out == paths.dubbed_video
    assert paths.dubbed_video.exists()
    assert not paths.subtitles.exists()  # dub_only delivers no subtitle


def test_mux_both_produces_video_and_srt(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    _mock_ffmpeg(monkeypatch)
    out = stages.mux(paths, output_mode="both")
    assert out == paths.dubbed_video
    assert paths.dubbed_video.exists()
    assert paths.subtitles.exists()


def test_mux_burned_delivery_deferred_no_crash(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # delivery=both (srt + burned) with the burn flag off: srt + video still
    # produced, burn-in deferred to M2.1 (no exception, no burned artifact).
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    _mock_ffmpeg(monkeypatch)
    out = stages.mux(paths, output_mode="both", subtitle_delivery="both")
    assert out == paths.dubbed_video
    assert paths.dubbed_video.exists()
    assert paths.subtitles.exists()


def test_mux_burn_flag_on_is_not_implemented(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # locks the placeholder: flipping the flag without the M2.1 burn impl raises,
    # rather than silently shipping a plain (un-burned) video.
    paths = JobPaths(tmp_path).ensure()
    _write_segments(paths, [("hello", "你好")])
    monkeypatch.setattr(stages.config, "BURN_SUBTITLES_ENABLED", True)
    with pytest.raises(NotImplementedError):
        stages.mux(paths, output_mode="subtitle_only", subtitle_delivery="burned")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"output_mode": "bogus"},
        {"subtitle_lang": "klingon"},
        {"subtitle_delivery": "telepathy"},
    ],
)
def test_mux_rejects_unknown_modes(tmp_path: Path, kwargs: dict) -> None:
    paths = JobPaths(tmp_path).ensure()
    _write_segments(paths, [("hello", "你好")])
    with pytest.raises(ValueError):
        stages.mux(paths, **kwargs)


# --------------------------------------------------------------------------- #
# run_pipeline orchestration: subtitle_only skips tts + align
# --------------------------------------------------------------------------- #
class _Info:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeAsr:
    info = _Info("fake_asr")

    def transcribe(self, audio_path: str, source_lang: str | None) -> Transcript:
        return Transcript(
            source_language=source_lang or "en", asr_provider="fake_asr",
            lines=[TranscriptLine(index=0, start_ms=0, end_ms=1000,
                                  source_text="hello", words=[], speaker_id="SPEAKER_00")],
        )


class _FakeMt:
    info = _Info("fake_mt")

    def translate(self, texts: list[str], source_lang: str, target_lang: str,
                  budgets_ms: list[int] | None = None) -> list[str]:
        return [t.upper() for t in texts]


class _FakeTts:
    info = _Info("fake_tts")
    ext = "wav"

    def voices_for(self, lang: str) -> list[str]:
        return ["v1"]

    def synthesize(self, text: str, voice_id: str, lang: str, out_path: str) -> str:
        return out_path


class _SubtitleResolver:
    def __init__(self) -> None:
        self.asr, self.mt, self.tts = _FakeAsr(), _FakeMt(), _FakeTts()

    def select(self, kind: str, requested: str | None, allow_paid: bool):  # noqa: ANN202,ARG002
        if kind == "tts":
            raise AssertionError("subtitle_only must not resolve a TTS provider")
        return {"asr": self.asr, "mt": self.mt, "tts": self.tts}[kind]


def test_run_pipeline_subtitle_only_skips_tts_and_align(
    tmp_path: Path, monkeypatch  # noqa: ANN001
) -> None:
    paths = JobPaths(tmp_path).ensure()
    called = {"tts": False, "align": False}
    monkeypatch.setattr(stages, "ingest", lambda *a, **k: None)  # noqa: ARG005
    monkeypatch.setattr(stages, "prepare", lambda *a, **k: None)  # noqa: ARG005
    monkeypatch.setattr(stages, "tts", lambda *a, **k: called.__setitem__("tts", True))  # noqa: ARG005
    monkeypatch.setattr(stages, "align", lambda *a, **k: called.__setitem__("align", True))  # noqa: ARG005

    out = stages.run_pipeline(
        paths, _SubtitleResolver(), source="x", target_lang="zh", output_mode="subtitle_only"
    )
    assert called == {"tts": False, "align": False}  # neither stage ran
    assert out == paths.subtitles
    assert paths.subtitles.exists()
    assert not paths.dubbed_video.exists()
