"""T1.3b — AIGC legal marking, conditional on output_mode. Test-first (red line
§3): a dubbed deliverable carries an AV voice mark (embedded container metadata);
a machine-translated subtitle leads with a light MT disclosure cue; the applied
method is recorded in the manifest. Marking off -> the output is unmarked.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from autodub_core import JobPaths, aigc, stages
from autodub_core.jsonio import read_json, write_json
from ovt_schemas.contracts import (
    AigcMarking,
    DubbingSegment,
    Job,
    JobArtifacts,
    JobPlan,
    Manifest,
    Transcript,
    TranscriptLine,
    TranslationResult,
)


def _marking(enabled: bool = True) -> AigcMarking:
    return AigcMarking(enabled=enabled, implicit=True, explicit=True, form="tail_notice")


def _write_segments(paths: JobPaths, pairs: list[tuple[str, str]]) -> None:
    segs = [
        DubbingSegment(
            segment_id=f"seg_{i:04d}", index=i, speaker_id="SPEAKER_00",
            start_ms=i * 1000, end_ms=i * 1000 + 1000, target_duration_ms=1000,
            source_text=s, target_text=t, keep_original=(not t),
        )
        for i, (s, t) in enumerate(pairs)
    ]
    write_json(paths.segments, TranslationResult(
        source_language="en", target_language="zh", mt_provider="m", segments=segs,
    ).model_dump())


def _mock_video_ffmpeg(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    monkeypatch.setattr(stages.ff, "assert_ffmpeg", lambda: None)
    monkeypatch.setattr(stages.ff, "assert_allowed_input_format", lambda v: None)  # noqa: ARG005
    monkeypatch.setattr(stages.ff, "probe_duration_ms", lambda p: 1000)  # noqa: ARG005
    monkeypatch.setattr(stages.ff, "stitch_timeline",
                        lambda pl, o, t: Path(o).write_bytes(b"a"))  # noqa: ARG005

    def fake_mux(video, audio, out, ambient=None, metadata=None):  # noqa: ANN001,ANN202
        captured["metadata"] = metadata
        Path(out).write_bytes(b"mp4")

    monkeypatch.setattr(stages.ff, "mux", fake_mux)

    def fake_watermark_video(video, out, wm, *, timeout_sec, crf=23, preset="veryfast"):  # noqa: ANN001,ANN202,ARG001
        captured["watermark"] = wm
        Path(out).write_bytes(b"wm")

    monkeypatch.setattr(stages.ff, "watermark_video", fake_watermark_video)


# --------------------------------------------------------------------------- #
# marking module (conditional on output_mode)
# --------------------------------------------------------------------------- #
def test_embed_method_is_voice_mark_for_dub_and_disclosure_for_subtitle() -> None:
    m = _marking()
    assert aigc.embed_method(m, "dub_only") == "av_voice_mark"
    assert aigc.embed_method(m, "both") == "av_voice_mark"
    assert aigc.embed_method(m, "subtitle_only") == "mt_disclosure"


def test_embed_method_none_when_marking_off() -> None:
    assert aigc.embed_method(_marking(enabled=False), "dub_only") is None
    assert aigc.embed_method(None, "subtitle_only") is None


def test_metadata_args_present_when_enabled_empty_when_off() -> None:
    args = aigc.metadata_args(_marking(), "dub_only")
    assert "-metadata" in args
    assert any("aigc_mark=av_voice_mark" in a for a in args)
    assert aigc.metadata_args(_marking(enabled=False), "dub_only") == []
    assert aigc.metadata_args(None, "both") == []


def test_subtitle_disclosure_text_only_when_enabled() -> None:
    assert aigc.subtitle_disclosure(_marking()) is not None
    assert aigc.subtitle_disclosure(_marking(enabled=False)) is None
    assert aigc.subtitle_disclosure(None) is None


def test_subtitle_disclosure_respects_subtitle_enabled_and_custom_text() -> None:
    # §14 owner-authorized reconfiguration: custom text is used when set.
    m = AigcMarking(
        enabled=True, implicit=True, explicit=True, form="disclosure_only",
        subtitle_enabled=True, subtitle_text="本视频由 AI 翻译",
    )
    assert aigc.subtitle_disclosure(m) == "本视频由 AI 翻译"
    # Per-channel off: subtitle_enabled=False -> None even though the master `enabled` is on.
    off = AigcMarking(
        enabled=True, implicit=True, explicit=True, form="disclosure_only", subtitle_enabled=False,
    )
    assert aigc.subtitle_disclosure(off) is None
    # Blank/whitespace custom text falls back to the kernel default line.
    blank = AigcMarking(
        enabled=True, implicit=True, explicit=True, form="disclosure_only", subtitle_text="   ",
    )
    assert aigc.subtitle_disclosure(blank) == "本字幕由机器翻译生成"


def test_subtitle_disabled_does_not_over_claim_embed_method() -> None:
    # aigcSubtitleEnabled=false on a subtitle_only job: no cue, and the AUDIT method must be None
    # (not mt_disclosure) so the manifest never over-claims an embedded disclosure the SRT lacks.
    off = AigcMarking(
        enabled=True, implicit=True, explicit=True, form="disclosure_only", subtitle_enabled=False,
    )
    assert aigc.subtitle_disclosure(off) is None
    assert aigc.embed_method(off, "subtitle_only") is None
    assert aigc.metadata_args(off, "subtitle_only") == []
    # The dub/both video channel is independent of subtitle_enabled.
    assert aigc.embed_method(off, "dub_only") == "av_voice_mark"
    # Custom subtitle text also flows into the container metadata comment (matches the cue).
    custom = AigcMarking(
        enabled=True, implicit=True, explicit=True, form="disclosure_only",
        subtitle_text="本视频由 AI 翻译",
    )
    assert any("本视频由 AI 翻译" in a for a in aigc.metadata_args(custom, "subtitle_only"))


# --------------------------------------------------------------------------- #
# mux applies the mark by mode
# --------------------------------------------------------------------------- #
def test_mux_dub_embeds_aigc_metadata(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    captured: dict = {}
    _mock_video_ffmpeg(monkeypatch, captured)
    marking = _marking()
    stages.mux(paths, output_mode="dub_only", marking=marking)
    assert any("aigc_mark=av_voice_mark" in a for a in captured["metadata"])
    assert marking.applied is True


def test_mux_subtitle_only_leads_with_mt_disclosure(tmp_path: Path) -> None:
    paths = JobPaths(tmp_path).ensure()
    _write_segments(paths, [("hello", "你好")])
    marking = _marking()
    stages.mux(paths, output_mode="subtitle_only", marking=marking)
    srt = paths.subtitles.read_text(encoding="utf-8")
    first_cue = srt.split("\n\n")[0].splitlines()  # [index, timing, disclosure]
    assert first_cue[0] == "1"
    assert "机器翻译" in first_cue[2]  # MT disclosure cue leads the file
    assert marking.applied is True


def test_mux_default_marks_when_marking_omitted(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # RED LINE §3 (CodeX bot P1): mux is exported and writes deliverables directly, so marking
    # MUST default ON — never an unmarked output by omission. marking=None marks by output_mode.
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    captured: dict = {}
    _mock_video_ffmpeg(monkeypatch, captured)
    stages.mux(paths, output_mode="both")  # marking=None -> default-on
    assert any("aigc_mark=av_voice_mark" in a for a in captured["metadata"])  # video marked
    assert "机器翻译" in paths.subtitles.read_text(encoding="utf-8")           # subtitle disclosed


def test_mux_explicit_disabled_marking_is_unmarked(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # The ONLY way to ship unmarked is an explicit AigcMarking(enabled=False) — the audited §14
    # acknowledgment, never omission.
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    captured: dict = {}
    _mock_video_ffmpeg(monkeypatch, captured)
    off = AigcMarking(enabled=False, implicit=False, explicit=False, form="tail_notice")
    stages.mux(paths, output_mode="both", marking=off)
    assert captured["metadata"] == []                                  # no AIGC metadata on video
    assert "机器翻译" not in paths.subtitles.read_text(encoding="utf-8")  # no disclosure cue


def test_mux_both_marks_video_and_subtitle(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    captured: dict = {}
    _mock_video_ffmpeg(monkeypatch, captured)
    stages.mux(paths, output_mode="both", subtitle_delivery="srt", marking=_marking())
    assert any("aigc_mark=av_voice_mark" in a for a in captured["metadata"])  # voice mark on video
    assert "机器翻译" in paths.subtitles.read_text(encoding="utf-8")           # disclosure on srt


def test_disclosure_rides_on_first_cue_when_no_gap(tmp_path: Path) -> None:
    # @CodeX bot P2: first real cue at 0ms -> no gap for a standalone notice, so the disclosure
    # rides on that cue's text rather than an overlapping [0,3000] cue (overlaps stack/hide).
    paths = JobPaths(tmp_path).ensure()
    _write_segments(paths, [("hello", "你好")])  # seg 0 at [0, 1000]
    stages.mux(paths, output_mode="subtitle_only")  # default-on marking
    blocks = [b for b in paths.subtitles.read_text(encoding="utf-8").split("\n\n") if b.strip()]
    assert len(blocks) == 1  # one cue only — no separate overlapping disclosure cue
    cue = blocks[0].splitlines()
    assert cue[1] == "00:00:00,000 --> 00:00:01,000"  # the real cue's own timing
    assert "本字幕由机器翻译生成" in cue and "你好" in cue  # disclosure folded onto it


def test_disclosure_standalone_cue_clamped_to_first_cue(tmp_path: Path) -> None:
    # @CodeX bot P2: with a gap before the first cue, the disclosure is a standalone leading cue
    # CLAMPED to end at the first cue's start, so it never overlaps real content.
    paths = JobPaths(tmp_path).ensure()
    seg = DubbingSegment(
        segment_id="seg_0000", index=0, speaker_id="SPEAKER_00",
        start_ms=2000, end_ms=4000, target_duration_ms=2000,
        source_text="hi", target_text="你好", keep_original=False,
    )
    write_json(paths.segments, TranslationResult(
        source_language="en", target_language="zh", mt_provider="m", segments=[seg]).model_dump())
    stages.mux(paths, output_mode="subtitle_only")  # default-on marking
    blocks = [b for b in paths.subtitles.read_text(encoding="utf-8").split("\n\n") if b.strip()]
    assert len(blocks) == 2
    assert blocks[0].splitlines()[1] == "00:00:00,000 --> 00:00:02,000"  # clamped to first cue
    assert "本字幕由机器翻译生成" in blocks[0]
    assert blocks[1].splitlines()[1] == "00:00:02,000 --> 00:00:04,000"  # real cue, no overlap


def test_bilingual_keeps_both_lines_when_target_equals_source(tmp_path: Path) -> None:
    # @CodeX bot P3: bilingual layout stays consistent — both lines kept even when MT returns
    # text identical to the source (names / acronyms / punctuation).
    paths = JobPaths(tmp_path).ensure()
    _write_segments(paths, [("OK", "OK")])  # MT == source
    off = AigcMarking(enabled=False, implicit=False, explicit=False, form="disclosure_only")
    stages.mux(paths, output_mode="subtitle_only", subtitle_lang="bilingual", marking=off)
    block = paths.subtitles.read_text(encoding="utf-8").split("\n\n")[0].splitlines()
    assert block[2:] == ["OK", "OK"]  # both lines present despite being identical


# --------------------------------------------------------------------------- #
# run_pipeline records the applied method in manifest.json
# --------------------------------------------------------------------------- #
def _minimal_job(
    marking: AigcMarking, output_mode: Literal["subtitle_only", "dub_only", "both"]
) -> Job:
    return Job(
        job_id="job_0001", anon_or_user_id="anon_demo", tier="tier1", status="done",
        source_type="upload", upload_session_id="up_0001", target_lang="zh-Hans",
        output_mode=output_mode, subtitle_delivery="srt", subtitle_lang="target",
        plan=JobPlan(asr="groq", mt="cloudflare", tts="piper"), settings_version=1,
        aigc_marking=marking, priority=0, enqueue_at=0, deadline_at=0, created_at=0,
        expires_at=0, artifacts=JobArtifacts(), attempt=1, claim_version=1,
        counted_job=True, counted_minutes=True, refunded=False,
    )


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


class _Resolver:
    def __init__(self) -> None:
        self.asr, self.mt, self.tts = _FakeAsr(), _FakeMt(), _FakeTts()

    def select(self, kind: str, requested: str | None, allow_paid: bool):  # noqa: ANN202,ARG002
        return {"asr": self.asr, "mt": self.mt, "tts": self.tts}[kind]


def test_run_pipeline_records_embed_method_in_manifest(
    tmp_path: Path, monkeypatch  # noqa: ANN001
) -> None:
    paths = JobPaths(tmp_path).ensure()
    marking = AigcMarking(enabled=True, implicit=True, explicit=False, form="disclosure_only")
    job = _minimal_job(marking, output_mode="subtitle_only")
    monkeypatch.setattr(stages, "ingest", lambda *a, **k: None)  # noqa: ARG005
    monkeypatch.setattr(stages, "prepare", lambda *a, **k: None)  # noqa: ARG005

    stages.run_pipeline(
        paths, _Resolver(), source="x", target_lang="zh",
        output_mode="subtitle_only", aigc_marking=marking, job=job,
    )
    manifest = Manifest.model_validate(read_json(paths.manifest))
    assert manifest.worker_meta.aigc_embed_method == "mt_disclosure"
    assert marking.applied is True


def test_run_pipeline_job_settings_override_kwarg_defaults(
    tmp_path: Path, monkeypatch  # noqa: ANN001
) -> None:
    # CodeX P1: the Job is authoritative — a subtitle_only job must run subtitle_only
    # even though the output_mode kwarg is left at its "both" default; the pipeline
    # must not diverge from the manifest it writes.
    paths = JobPaths(tmp_path).ensure()
    marking = AigcMarking(enabled=True, implicit=True, explicit=False, form="disclosure_only")
    job = _minimal_job(marking, output_mode="subtitle_only")
    called = {"tts": False, "align": False}
    monkeypatch.setattr(stages, "ingest", lambda *a, **k: None)  # noqa: ARG005
    monkeypatch.setattr(stages, "prepare", lambda *a, **k: None)  # noqa: ARG005
    monkeypatch.setattr(stages, "tts", lambda *a, **k: called.__setitem__("tts", True))  # noqa: ARG005, E501
    monkeypatch.setattr(stages, "align", lambda *a, **k: called.__setitem__("align", True))  # noqa: ARG005, E501

    # NOTE: no output_mode/aigc_marking kwargs -> kwarg defaults are "both"/None;
    # the job must override both.
    out = stages.run_pipeline(paths, _Resolver(), source="x", target_lang="en", job=job)
    assert called == {"tts": False, "align": False}  # subtitle_only skipped tts + align
    assert out == paths.subtitles and paths.subtitles.exists()
    assert not paths.dubbed_video.exists()
    manifest = Manifest.model_validate(read_json(paths.manifest))
    assert manifest.worker_meta.aigc_embed_method == "mt_disclosure"  # derived from job
    assert marking.applied is True


def test_mux_cache_hit_with_matching_settings_records_applied(
    tmp_path: Path, monkeypatch  # noqa: ANN001
) -> None:
    # CodeX R2: a resume of a MARKED run with the SAME settings hits the cache and
    # still records applied, without re-muxing.
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    captured: dict = {}
    _mock_video_ffmpeg(monkeypatch, captured)
    stages.mux(paths, output_mode="both", marking=_marking())  # first run writes the marker
    captured.clear()
    marking = _marking()
    out = stages.mux(paths, output_mode="both", marking=marking)  # same settings -> cache hit
    assert out == paths.dubbed_video
    assert "metadata" not in captured  # ff.mux NOT called again
    assert marking.applied is True     # cache hit still records the (verified) mark


def test_mux_marking_change_invalidates_cache(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # CodeX R3: artifacts from an earlier UNMARKED run must NOT be presented as marked
    # — a later marked call re-muxes WITH the mark instead of over-claiming the cache.
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    captured: dict = {}
    _mock_video_ffmpeg(monkeypatch, captured)
    stages.mux(paths, output_mode="both", marking=_marking(enabled=False))  # unmarked first run
    assert captured["metadata"] == []
    captured.clear()
    marking = _marking()
    stages.mux(paths, output_mode="both", marking=marking)  # marked -> key differs -> re-mux
    assert any("aigc_mark=av_voice_mark" in a for a in captured["metadata"])  # re-muxed marked
    assert marking.applied is True


def test_mux_subtitle_text_change_invalidates_cache(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # §14: changing the subtitle cue text must force a re-mux, not serve a stale SRT — the cache key
    # folds in the visible subtitle cue (for `both`, embed_method alone would not change).
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    captured: dict = {}
    _mock_video_ffmpeg(monkeypatch, captured)
    m1 = AigcMarking(
        enabled=True, implicit=True, explicit=True, form="tail_notice", subtitle_text="旧文案",
    )
    stages.mux(paths, output_mode="both", marking=m1)  # first run writes marker with subcue=旧文案
    captured.clear()
    m2 = AigcMarking(
        enabled=True, implicit=True, explicit=True, form="tail_notice", subtitle_text="新文案",
    )
    stages.mux(paths, output_mode="both", marking=m2)  # cue text differs -> key differs -> re-mux
    assert "metadata" in captured  # ff.mux WAS called again (not a stale cache hit)
    srt = paths.subtitles.read_text(encoding="utf-8")
    assert "新文案" in srt and "旧文案" not in srt  # SRT carries the NEW cue, not the stale one


def test_run_pipeline_defaults_to_marked_when_none_given(
    tmp_path: Path, monkeypatch  # noqa: ANN001
) -> None:
    # red line §3 (默认开): an ad-hoc run with no aigc_marking and no job still marks.
    paths = JobPaths(tmp_path).ensure()
    monkeypatch.setattr(stages, "ingest", lambda *a, **k: None)  # noqa: ARG005
    monkeypatch.setattr(stages, "prepare", lambda *a, **k: None)  # noqa: ARG005
    stages.run_pipeline(
        paths, _Resolver(), source="x", target_lang="zh", output_mode="subtitle_only"
    )
    assert "机器翻译" in paths.subtitles.read_text(encoding="utf-8")  # default-on disclosure


# --------------------------------------------------------------------------- #
# PR-2 visible AIGC watermark (policy + mux application)
# --------------------------------------------------------------------------- #
def _wm_marking(**over: object) -> AigcMarking:
    base: dict = dict(
        enabled=True, implicit=True, explicit=True, form="tail_notice",
        video_watermark_enabled=True,
    )
    base.update(over)
    return AigcMarking(**base)


def test_video_watermark_text_gated_and_custom() -> None:
    # DEFAULT-OFF: a plain marking (video_watermark_enabled defaults False) -> None.
    assert aigc.video_watermark_text(_marking()) is None
    assert aigc.video_watermark_text(None) is None
    # Master enabled=False -> None even if the per-channel flag is on (disabled marking = no mark).
    off = _wm_marking(enabled=False, implicit=False, explicit=False)
    assert aigc.video_watermark_text(off) is None
    # Enabled -> default notice when text is null/blank; custom text when set.
    assert aigc.video_watermark_text(_wm_marking()) == "本视频由 AI 合成"
    custom = aigc.video_watermark_text(_wm_marking(video_watermark_text="AI 生成·仅供参考"))
    assert custom == "AI 生成·仅供参考"
    assert aigc.video_watermark_text(_wm_marking(video_watermark_text="   ")) == "本视频由 AI 合成"


def test_mux_dub_no_watermark_by_default(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # DEFAULT-OFF: a normal marked dub does NOT trigger the watermark re-encode (stream-copy stays).
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    captured: dict = {}
    _mock_video_ffmpeg(monkeypatch, captured)
    stages.mux(paths, output_mode="dub_only", marking=_marking())
    assert "watermark" not in captured  # ff.watermark_video NOT called


def test_mux_dub_applies_video_watermark_when_enabled(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # Watermark on -> the delivered dub video is re-encoded with the resolved render spec + font;
    # the marking is recorded applied (the mark also lives in the container metadata).
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    captured: dict = {}
    _mock_video_ffmpeg(monkeypatch, captured)
    marking = _wm_marking(
        video_watermark_text="AI 合成", video_watermark_position="top_left",
        video_watermark_font_size=8, video_watermark_opacity=70, video_watermark_color="#00FF00",
    )
    stages.mux(paths, output_mode="dub_only", marking=marking, watermark_font="/f/noto.ttc")
    wm = captured["watermark"]
    assert wm is not None
    assert wm.text == "AI 合成" and wm.position == "top_left" and wm.size_pct == 8
    assert wm.opacity_pct == 70 and wm.color == "#00FF00" and wm.fontfile == "/f/noto.ttc"
    assert marking.applied is True


def test_mux_subtitle_only_srt_never_watermarks(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # No video deliverable (subtitle_only + srt) -> the watermark never applies even when enabled,
    # so an SRT-only job triggers no needless re-encode.
    paths = JobPaths(tmp_path).ensure()
    _write_segments(paths, [("hello", "你好")])
    captured: dict = {}
    _mock_video_ffmpeg(monkeypatch, captured)
    stages.mux(paths, output_mode="subtitle_only", marking=_wm_marking(form="disclosure_only"))
    assert "watermark" not in captured


def test_mux_watermark_change_invalidates_cache(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # The watermark params are folded into the mux cache key: editing the text forces a re-mux
    # (+ re-watermark) rather than serving the stale, differently-watermarked video.
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    captured: dict = {}
    _mock_video_ffmpeg(monkeypatch, captured)
    stages.mux(paths, output_mode="dub_only", marking=_wm_marking(video_watermark_text="旧水印"))
    captured.clear()
    stages.mux(paths, output_mode="dub_only", marking=_wm_marking(video_watermark_text="新水印"))
    assert captured.get("watermark") is not None and captured["watermark"].text == "新水印"


def test_mux_watermark_default_off_cache_key_unchanged(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # A default-off run's cache key must stay byte-identical to a pre-PR-2 run (no 'wm:' token), so
    # enabling PR-2 does not force a needless re-mux of already-cached default deliverables.
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    captured: dict = {}
    _mock_video_ffmpeg(monkeypatch, captured)
    stages.mux(paths, output_mode="dub_only", marking=_marking())  # writes the marker
    marker = (paths.output / ".mux_cache").read_text(encoding="utf-8")
    assert "wm:" not in marker
    captured.clear()
    stages.mux(paths, output_mode="dub_only", marking=_marking())  # same key -> cache hit
    assert "metadata" not in captured  # ff.mux NOT called again


def test_mux_burned_forwards_watermark_to_burn(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # subtitle_only + burned: the watermark rides the burn re-encode (one pass), so stages forwards
    # it to ff.burn_subtitles rather than running a separate watermark_video pass.
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    captured: dict = {}
    _mock_video_ffmpeg(monkeypatch, captured)

    def fake_burn(video, srt, out, **kwargs):  # noqa: ANN001,ANN003,ANN202
        captured["burn_watermark"] = kwargs.get("watermark")
        Path(out).write_bytes(b"burned")

    monkeypatch.setattr(stages.ff, "burn_subtitles", fake_burn)
    marking = _wm_marking(form="disclosure_only", video_watermark_text="AI 水印")
    stages.mux(paths, output_mode="subtitle_only", subtitle_delivery="burned", marking=marking,
               watermark_font="/f/noto.ttc")
    assert "watermark" not in captured  # no separate second pass
    wm = captured["burn_watermark"]
    assert wm is not None and wm.text == "AI 水印" and wm.fontfile == "/f/noto.ttc"


def test_run_pipeline_records_visible_watermark_as_only_applied_mark(
    tmp_path: Path, monkeypatch  # noqa: ANN001
) -> None:
    # §3 audit accuracy: subtitle_only + burned with the subtitle cue OFF but the watermark ON — the
    # burned video visibly carries the drawtext overlay while embed_method() is None. The manifest
    # must record "visible_watermark", never a self-contradictory None (applied=True + method=None).
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    marking = AigcMarking(
        enabled=True, implicit=True, explicit=True, form="disclosure_only",
        subtitle_enabled=False, video_watermark_enabled=True,
    )
    job = Job(
        job_id="job_wm", anon_or_user_id="anon", tier="tier1", status="done", source_type="upload",
        upload_session_id="up", target_lang="zh-Hans", output_mode="subtitle_only",
        subtitle_delivery="burned", subtitle_lang="target",
        plan=JobPlan(asr="a", mt="b", tts=None), settings_version=1, aigc_marking=marking,
        priority=0, enqueue_at=0, deadline_at=0, created_at=0, expires_at=0,
        artifacts=JobArtifacts(), attempt=1, claim_version=1,
        counted_job=True, counted_minutes=True, refunded=False,
    )
    monkeypatch.setattr(stages, "ingest", lambda *a, **k: None)  # noqa: ARG005
    monkeypatch.setattr(stages, "prepare", lambda *a, **k: None)  # noqa: ARG005

    def fake_burn(video, srt, out, **kwargs):  # noqa: ANN001,ANN003,ANN202
        Path(out).write_bytes(b"burned")

    monkeypatch.setattr(stages.ff, "burn_subtitles", fake_burn)
    stages.run_pipeline(paths, _Resolver(), source="x", target_lang="zh-Hans", job=job)
    assert marking.applied is True
    manifest = Manifest.model_validate(read_json(paths.manifest))
    # NOT None — §3 forbids under-claiming a mark the artifact visibly carries.
    assert manifest.worker_meta.aigc_embed_method == "visible_watermark"
