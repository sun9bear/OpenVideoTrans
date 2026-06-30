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
    AigcMarking,
    DubbingSegment,
    Transcript,
    TranscriptLine,
    TranslationResult,
)

# Marking is default-ON at the mux boundary (§3); these tests isolate output-mode / bilingual
# STRUCTURE from the AIGC disclosure cue by passing an explicit disabled marking.
_NO_MARK = AigcMarking(enabled=False, implicit=False, explicit=False, form="disclosure_only")


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
    monkeypatch.setattr(stages.ff, "assert_allowed_input_format", lambda v: None)  # noqa: ARG005
    monkeypatch.setattr(stages.ff, "probe_duration_ms", lambda p: 1000)  # noqa: ARG005
    monkeypatch.setattr(stages.ff, "stitch_timeline",
                        lambda placements, out, total: Path(out).write_bytes(b"a"))  # noqa: ARG005
    monkeypatch.setattr(stages.ff, "mux",
                        lambda v, a, o, ambient=None, metadata=None: Path(o).write_bytes(b"mp4"))  # noqa: ARG005, E501
    monkeypatch.setattr(stages.ff, "probe_dimensions", lambda p: (1920, 1080))  # noqa: ARG005
    monkeypatch.setattr(stages.ff, "burn_subtitles",
                        lambda video, srt, out, **kw: Path(out).write_bytes(b"burned"))  # noqa: ARG005, E501


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
    stages.mux(paths, output_mode="subtitle_only", subtitle_lang="bilingual", marking=_NO_MARK)
    srt = paths.subtitles.read_text(encoding="utf-8")
    block = srt.split("\n\n")[0].splitlines()  # [index, timing, target, source]
    assert block[2] == "你好"  # target on top (the deliverable language)
    assert block[3] == "hello"  # source below


def test_mux_subtitle_lang_change_invalidates_cache(tmp_path: Path) -> None:
    # CodeX R5: a target-only run then a bilingual run on the SAME job dir must rewrite
    # the srt with the source line, not serve the stale target-only cached srt.
    paths = JobPaths(tmp_path).ensure()
    _write_segments(paths, [("hello", "你好")])
    stages.mux(paths, output_mode="subtitle_only", subtitle_lang="target")
    assert "hello" not in paths.subtitles.read_text(encoding="utf-8")  # target only
    stages.mux(paths, output_mode="subtitle_only", subtitle_lang="bilingual")
    srt = paths.subtitles.read_text(encoding="utf-8")
    assert "hello" in srt and "你好" in srt  # cache invalidated -> re-written bilingual


def test_mux_bilingual_falls_back_to_one_line_when_no_target(tmp_path: Path) -> None:
    # keep_original (empty target): even bilingual emits a single source line.
    paths = JobPaths(tmp_path).ensure()
    _write_segments(paths, [("hello", "")])
    stages.mux(paths, output_mode="subtitle_only", subtitle_lang="bilingual", marking=_NO_MARK)
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


def test_mux_clears_stale_deliverable_when_mode_narrows(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001, E501
    # CodeX bot P2: reuse a job dir from `both`, then re-run subtitle_only -> the old
    # dubbed_video.mp4 must be removed, not left for presence-based packaging to publish.
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    _mock_ffmpeg(monkeypatch)
    stages.mux(paths, output_mode="both")  # -> dubbed_video.mp4 + subtitles.srt
    assert paths.dubbed_video.exists()
    stages.mux(paths, output_mode="subtitle_only")  # mode narrows: video is no longer requested
    assert paths.subtitles.exists()
    assert not paths.dubbed_video.exists()  # stale deliverable cleared, not left behind


def test_mux_both_burned_delivers_burned_video_not_srt(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # M2.1 (flag ON): output_mode=both + delivery=burned -> the deliverable is the burned
    # video (dub + burned subs); no srt is delivered, and burned_video is the primary.
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    _mock_ffmpeg(monkeypatch)
    out = stages.mux(paths, output_mode="both", subtitle_delivery="burned")
    assert out == paths.burned_video
    assert paths.burned_video.exists()
    assert not paths.subtitles.exists()  # delivery=burned carries no srt deliverable


def test_mux_delivery_both_produces_srt_and_burned_video(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # delivery=both: deliver the srt AND a burned video; burned_video is the primary.
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    _mock_ffmpeg(monkeypatch)
    out = stages.mux(paths, output_mode="both", subtitle_delivery="both")
    assert out == paths.burned_video
    assert paths.burned_video.exists()
    assert paths.subtitles.exists()


def test_mux_subtitle_only_burned_burns_onto_original(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # subtitle_only + burned: no dub; burn onto the ORIGINAL video; deliver only the burned video.
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    _mock_ffmpeg(monkeypatch)
    seen: dict[str, Path] = {}

    def _capture(video, srt, out, **kw) -> None:  # noqa: ANN001
        seen["src"] = Path(video)
        Path(out).write_bytes(b"burned")

    monkeypatch.setattr(stages.ff, "burn_subtitles", _capture)
    out = stages.mux(paths, output_mode="subtitle_only", subtitle_delivery="burned")
    assert out == paths.burned_video
    assert paths.burned_video.exists()
    assert not paths.dubbed_video.exists()  # subtitle_only produces no dub
    assert seen["src"] == paths.original_video()  # burned onto the original video


def test_mux_burn_cap_change_invalidates_cache(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # A BURN_MAX_HEIGHT change must re-burn (the cap shapes the pixels), not serve the stale video.
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    _mock_ffmpeg(monkeypatch)
    calls = {"n": 0}

    def _count(video, srt, out, **kw) -> None:  # noqa: ANN001
        calls["n"] += 1
        Path(out).write_bytes(b"burned")

    monkeypatch.setattr(stages.ff, "burn_subtitles", _count)
    stages.mux(paths, output_mode="subtitle_only", subtitle_delivery="burned")
    stages.mux(paths, output_mode="subtitle_only", subtitle_delivery="burned")  # cached
    assert calls["n"] == 1  # second run hit the cache
    monkeypatch.setattr(stages.config, "BURN_MAX_HEIGHT", 720)
    stages.mux(paths, output_mode="subtitle_only", subtitle_delivery="burned")  # cap changed
    assert calls["n"] == 2  # re-burned, not served stale


def test_mux_burn_off_falls_back_to_srt(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # Ops fallback: BURN_SUBTITLES_ENABLED off + delivery=both -> srt + plain dub, no burned video.
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    _mock_ffmpeg(monkeypatch)
    monkeypatch.setattr(stages.config, "BURN_SUBTITLES_ENABLED", False)
    out = stages.mux(paths, output_mode="both", subtitle_delivery="both")
    assert out == paths.dubbed_video  # the plain dub is the video deliverable
    assert paths.subtitles.exists()
    assert not paths.burned_video.exists()  # burn disabled -> no burned artifact


def test_mux_burned_only_flag_off_raises(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # subtitle_only + burned with the burn OFF and no srt channel: fail explicitly
    # (no deliverable channel) rather than complete with a dead primary path.
    paths = JobPaths(tmp_path).ensure()
    _write_segments(paths, [("hello", "你好")])
    monkeypatch.setattr(stages.config, "BURN_SUBTITLES_ENABLED", False)
    with pytest.raises(NotImplementedError, match="no srt fallback"):
        stages.mux(paths, output_mode="subtitle_only", subtitle_delivery="burned")
    assert not paths.subtitles.exists()


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
