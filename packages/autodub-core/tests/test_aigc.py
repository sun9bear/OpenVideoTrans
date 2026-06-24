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
    monkeypatch.setattr(stages.ff, "probe_duration_ms", lambda p: 1000)  # noqa: ARG005
    monkeypatch.setattr(stages.ff, "stitch_timeline",
                        lambda pl, o, t: Path(o).write_bytes(b"a"))  # noqa: ARG005

    def fake_mux(video, audio, out, ambient=None, metadata=None):  # noqa: ANN001,ANN202
        captured["metadata"] = metadata
        Path(out).write_bytes(b"mp4")

    monkeypatch.setattr(stages.ff, "mux", fake_mux)


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


def test_mux_without_marking_is_unmarked(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    captured: dict = {}
    _mock_video_ffmpeg(monkeypatch, captured)
    stages.mux(paths, output_mode="both")  # marking=None
    assert captured["metadata"] == []           # no AIGC metadata on the video
    srt = paths.subtitles.read_text(encoding="utf-8")
    assert "机器翻译" not in srt                  # no disclosure cue


def test_mux_both_marks_video_and_subtitle(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    _write_segments(paths, [("hello", "你好")])
    captured: dict = {}
    _mock_video_ffmpeg(monkeypatch, captured)
    stages.mux(paths, output_mode="both", subtitle_delivery="srt", marking=_marking())
    assert any("aigc_mark=av_voice_mark" in a for a in captured["metadata"])  # voice mark on video
    assert "机器翻译" in paths.subtitles.read_text(encoding="utf-8")           # disclosure on srt


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


def test_mux_cache_hit_still_records_marking_applied(tmp_path: Path) -> None:
    # CodeX P2: a resume that hits the mux cache (deliverables already written) must
    # still record the mark as applied, so the manifest doesn't under-claim.
    paths = JobPaths(tmp_path).ensure()
    paths.dubbed_video.write_bytes(b"mp4")
    paths.subtitles.write_text("1\n", encoding="utf-8")  # both deliverables present -> cache hit
    _write_segments(paths, [("hello", "你好")])
    marking = _marking()
    out = stages.mux(paths, output_mode="both", marking=marking)
    assert out == paths.dubbed_video
    assert marking.applied is True  # cache hit still records the mark
