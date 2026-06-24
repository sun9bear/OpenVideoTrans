"""Characterization tests for the provider-using stages (transcribe / translate /
tts) driven through the injected ``Resolver`` seam with in-test fakes.

No ffmpeg, no network, no real providers — this both covers the orchestration
logic and documents the contract that ``provider-adapters`` (T1.2) must satisfy.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from autodub_core import JobPaths, stages
from autodub_core.jsonio import read_json, write_json
from autodub_core.providers import ProviderUnavailable
from ovt_schemas.contracts import Transcript, TranscriptLine, TranslationResult


class _Info:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeAsr:
    info = _Info("fake_asr")

    def transcribe(self, audio_path: str, source_lang: str | None) -> Transcript:
        return Transcript(
            source_language=source_lang or "en", asr_provider="fake_asr",
            lines=[
                TranscriptLine(index=0, start_ms=0, end_ms=1000,
                               source_text="hello", words=[], speaker_id="SPEAKER_00"),
                TranscriptLine(index=1, start_ms=1000, end_ms=2000,
                               source_text="world", words=[], speaker_id="SPEAKER_01"),
            ],
        )


class _FakeMt:
    info = _Info("fake_mt")

    def translate(self, texts: list[str], source_lang: str, target_lang: str,
                  budgets_ms: list[int] | None = None) -> list[str]:
        self.last_budgets = budgets_ms
        return [t.upper() for t in texts]


class _FakeTts:
    info = _Info("fake_tts")
    ext = "wav"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    def voices_for(self, lang: str) -> list[str]:
        return ["voiceA", "voiceB"]

    def synthesize(self, text: str, voice_id: str, lang: str, out_path: str) -> str:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"RIFFfake")  # placeholder; align (ffmpeg) not run here
        self.calls.append((text, voice_id, lang, out_path))
        return out_path


class FakeResolver:
    def __init__(self) -> None:
        self.asr = _FakeAsr()
        self.mt = _FakeMt()
        self.tts = _FakeTts()

    def select(self, kind: str, requested: str | None, allow_paid: bool):  # noqa: ARG002
        return {"asr": self.asr, "mt": self.mt, "tts": self.tts}[kind]


def test_transcribe_writes_transcript(tmp_path: Path) -> None:
    paths = JobPaths(tmp_path).ensure()
    result = stages.transcribe(paths, FakeResolver(), None, "en")
    assert paths.transcript.exists()
    assert len(result.lines) == 2
    reloaded = Transcript.model_validate(read_json(paths.transcript))
    assert reloaded.lines[1].speaker_id == "SPEAKER_01"
    assert reloaded.asr_provider == "fake_asr"


def test_translate_builds_segments_with_budgets(tmp_path: Path) -> None:
    res = FakeResolver()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en")
    tr = stages.translate(paths, res, None, "zh", "en")
    assert tr.target_language == "zh"
    assert [s.target_text for s in tr.segments] == ["HELLO", "WORLD"]
    assert all(not s.keep_original for s in tr.segments)
    assert tr.segments[0].target_duration_ms == 1000
    assert res.mt.last_budgets == [1000, 1000]  # per-line duration budgets
    assert paths.segments.exists()


def test_translate_no_lines_skips_mt_resolution(tmp_path: Path) -> None:
    # silent / no-speech video: ASR returns zero lines, so translate must produce
    # a valid empty result WITHOUT resolving an MT provider (it may be
    # unavailable / quota-exhausted) (CodeX @PR P2).
    class EmptyAsr(_FakeAsr):
        def transcribe(self, audio_path: str, source_lang: str | None) -> Transcript:
            return Transcript(source_language="en", asr_provider="fake_asr", lines=[])

    class NoMtResolver(FakeResolver):
        def __init__(self) -> None:
            super().__init__()
            self.asr = EmptyAsr()

        def select(self, kind: str, requested: str | None, allow_paid: bool):  # noqa: ANN202
            if kind == "mt":
                raise AssertionError("MT must not be resolved when there are no lines")
            return super().select(kind, requested, allow_paid)

    res = NoMtResolver()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en")
    tr = stages.translate(paths, res, None, "zh", "en")  # must NOT raise
    assert tr.segments == []
    assert tr.mt_provider == ""
    assert paths.segments.exists()


def test_translate_empty_target_sets_keep_original(tmp_path: Path) -> None:
    class EmptyMt(_FakeMt):
        def translate(self, texts, source_lang, target_lang, budgets_ms=None):  # noqa: ANN001,ARG002
            return ["" for _ in texts]

    res = FakeResolver()
    res.mt = EmptyMt()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en")
    tr = stages.translate(paths, res, None, "zh", "en")
    assert all(s.keep_original for s in tr.segments)


def test_tts_assigns_voices_round_robin_and_synthesizes(tmp_path: Path) -> None:
    res = FakeResolver()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en")
    stages.translate(paths, res, None, "zh", "en")
    tr = stages.tts(paths, res, None)
    voices = {s.speaker_id: s.voice_id for s in tr.segments}
    assert voices == {"SPEAKER_00": "voiceA", "SPEAKER_01": "voiceB"}
    assert all(s.tts_provider == "fake_tts" for s in tr.segments)
    assert len(res.tts.calls) == 2
    assert paths.find_tts_raw(0) is not None


def test_tts_skips_synthesis_for_keep_original(tmp_path: Path) -> None:
    class EmptyMt(_FakeMt):
        def translate(self, texts, source_lang, target_lang, budgets_ms=None):  # noqa: ANN001,ARG002
            return ["" for _ in texts]

    res = FakeResolver()
    res.mt = EmptyMt()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en")
    stages.translate(paths, res, None, "zh", "en")
    tr = stages.tts(paths, res, None)
    assert res.tts.calls == []  # nothing synthesized when every segment is keep_original
    assert all(s.keep_original for s in tr.segments)


def test_tts_no_voice_for_language_raises(tmp_path: Path) -> None:
    class NoVoiceTts(_FakeTts):
        def voices_for(self, lang: str) -> list[str]:
            return []

    res = FakeResolver()
    res.tts = NoVoiceTts()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en")
    stages.translate(paths, res, None, "zh", "en")
    with pytest.raises(ProviderUnavailable):
        stages.tts(paths, res, None)


def test_ingest_rejects_url_as_input_mode_error(tmp_path: Path) -> None:
    # the kernel is local-file only and network-free; a URL is an input-mode
    # error (ValueError), not a provider-availability one. URL ingest is the
    # local-runner CLI's job (T1.4).
    paths = JobPaths(tmp_path).ensure()
    with pytest.raises(ValueError, match="local file path only"):
        stages.ingest(paths, "https://example.com/video.mp4")


def test_tts_all_keep_original_completes_without_a_voice(tmp_path: Path) -> None:
    # Subtitle-only / all-empty-translation jobs need no synthesis, so a target
    # locale with NO tts voice must still complete — tts must not resolve a voice
    # when there is nothing to synthesize (CodeX P2).
    class EmptyMt(_FakeMt):
        def translate(self, texts, source_lang, target_lang, budgets_ms=None):  # noqa: ANN001,ARG002
            return ["" for _ in texts]

    class NoVoiceTts(_FakeTts):
        def voices_for(self, lang: str) -> list[str]:
            return []

    res = FakeResolver()
    res.mt = EmptyMt()
    res.tts = NoVoiceTts()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en")
    stages.translate(paths, res, None, "zh", "en")
    tr = stages.tts(paths, res, None)  # must NOT raise
    assert res.tts.calls == []
    assert all(s.keep_original for s in tr.segments)


def test_mux_not_cached_when_srt_missing(tmp_path: Path) -> None:
    # mp4 present but srt lost to a crash: mux must NOT treat the job as cached
    # (it proceeds and, here, fails on the absent source video) (CodeX P2).
    paths = JobPaths(tmp_path).ensure()
    paths.dubbed_video.write_bytes(b"\x00")
    write_json(paths.segments, TranslationResult(
        source_language="en", target_language="zh", mt_provider="m", segments=[],
    ).model_dump())
    with pytest.raises(RuntimeError, match="no original video"):
        stages.mux(paths, force=False)


def test_mux_cached_when_both_outputs_exist(tmp_path: Path) -> None:
    # both promised outputs present AND the cache marker matches the requested
    # settings -> cached early-return before any ffmpeg work (no segments.json /
    # video needed). marking defaults ON at the mux boundary (§3), so a default `both`
    # run's key carries the av_voice_mark method.
    paths = JobPaths(tmp_path).ensure()
    paths.dubbed_video.write_bytes(b"\x00")
    paths.subtitles.write_text("1\n", encoding="utf-8")
    (paths.output / ".mux_cache").write_text("av_voice_mark|both|target|srt", encoding="utf-8")
    assert stages.mux(paths, force=False) == paths.dubbed_video


def test_ingest_handles_already_staged_source(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # caller already placed the source at the canonical video/original.* location
    # (worker pre-stage / re-extract): ingest must not copy a file onto itself
    # (shutil.SameFileError) and must still extract audio from it (CodeX P2).
    paths = JobPaths(tmp_path).ensure()
    staged = paths.video / "original.mp4"
    staged.write_bytes(b"fakevideo")

    def fake_extract(src, out, sr=16000, mono=True):  # noqa: ANN001,ANN202,ARG001
        Path(out).write_bytes(b"wav")

    monkeypatch.setattr(stages.ff, "assert_ffmpeg", lambda: None)
    monkeypatch.setattr(stages.ff, "assert_allowed_input_format", lambda v: None)  # noqa: ARG005
    monkeypatch.setattr(stages.ff, "extract_audio", fake_extract)

    stages.ingest(paths, str(staged))  # must NOT raise SameFileError
    assert paths.original_audio.exists()


def test_ingest_force_replaces_stale_original_of_other_ext(
    tmp_path: Path, monkeypatch,  # noqa: ANN001
) -> None:
    # a forced re-ingest pointing at a new suffix must drop the prior original.*
    # so original_video() can't return the stale file (CodeX P2).
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.avi").write_bytes(b"old-avi")  # prior run's source
    new_src = tmp_path / "new.mp4"
    new_src.write_bytes(b"new-mp4")

    def fake_extract(src, out, sr=16000, mono=True):  # noqa: ANN001,ANN202,ARG001
        Path(out).write_bytes(b"wav")

    monkeypatch.setattr(stages.ff, "assert_ffmpeg", lambda: None)
    monkeypatch.setattr(stages.ff, "assert_allowed_input_format", lambda v: None)  # noqa: ARG005
    monkeypatch.setattr(stages.ff, "extract_audio", fake_extract)

    stages.ingest(paths, str(new_src), force=True)

    originals = sorted(p.name for p in paths.video.glob("original.*"))
    assert originals == ["original.mp4"]  # stale .avi removed, only the new source
    video = paths.original_video()
    assert video is not None and video.name == "original.mp4"


def test_tts_raw_atomic_on_synthesize_failure(tmp_path: Path) -> None:
    # a provider that dies after a partial write must leave NO cached raw in the
    # job's tts dir, so a resume re-synthesizes instead of consuming garbage (CodeX P2).
    res = FakeResolver()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en")
    stages.translate(paths, res, None, "zh", "en")

    class BoomTts(_FakeTts):
        def synthesize(self, text: str, voice_id: str, lang: str, out_path: str) -> str:
            Path(out_path).write_bytes(b"partial")  # partial write...
            raise RuntimeError("provider died mid-synthesis")  # ...then crash

    res.tts = BoomTts()
    with pytest.raises(RuntimeError, match="mid-synthesis"):
        stages.tts(paths, res, None)
    # nothing cached in the canonical tts location (partial lived in a temp subdir)
    assert paths.find_tts_raw(0) is None


def test_ingest_force_invalidates_stale_audio_on_extract_failure(
    tmp_path: Path, monkeypatch,  # noqa: ANN001
) -> None:
    # if a forced re-ingest stages a new video but audio extraction fails, the
    # prior audio must NOT survive (else a later non-force retry pairs new video
    # with stale audio) (CodeX P2).
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.avi").write_bytes(b"old-video")
    paths.original_audio.write_bytes(b"old-audio")  # stale audio from a prior run
    new_src = tmp_path / "new.mp4"
    new_src.write_bytes(b"new-video")

    def boom_extract(src, out, sr=16000, mono=True):  # noqa: ANN001,ANN202,ARG001
        raise stages.ff.FfmpegError("ffmpeg killed")

    monkeypatch.setattr(stages.ff, "assert_ffmpeg", lambda: None)
    monkeypatch.setattr(stages.ff, "assert_allowed_input_format", lambda v: None)  # noqa: ARG005
    monkeypatch.setattr(stages.ff, "extract_audio", boom_extract)

    with pytest.raises(stages.ff.FfmpegError):
        stages.ingest(paths, str(new_src), force=True)
    assert not paths.original_audio.exists()  # stale audio invalidated


def test_align_fully_cached_is_noop_without_ffmpeg(
    tmp_path: Path, monkeypatch,  # noqa: ANN001
) -> None:
    # a fully-cached align() must be a true no-op even if ffmpeg/ffprobe are no
    # longer on PATH (no-op-on-cache contract) (CodeX P2).
    res = FakeResolver()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en")
    stages.translate(paths, res, None, "zh", "en")
    for i in (0, 1):  # pre-create aligned outputs (simulate a prior align)
        paths.tts_aligned(i).write_bytes(b"aligned")

    def boom() -> None:
        raise stages.ff.FfmpegError("ffmpeg gone from PATH")

    monkeypatch.setattr(stages.ff, "assert_ffmpeg", boom)
    result = stages.align(paths, force=False)  # must NOT raise
    assert len(result.segments) == 2


def test_kernel_never_enables_paid(tmp_path: Path) -> None:
    # red line §1/§14: the Tier 1 kernel has no caller-controlled paid opt-in and
    # must always pass allow_paid=False to the resolver.
    seen: list[bool] = []

    class RecordingResolver(FakeResolver):
        def select(self, kind: str, requested: str | None, allow_paid: bool):  # noqa: ANN202
            seen.append(allow_paid)
            return super().select(kind, requested, allow_paid)

    res = RecordingResolver()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en")
    stages.translate(paths, res, None, "zh", "en")
    stages.tts(paths, res, None)
    assert seen and all(flag is False for flag in seen)


def test_original_video_ignores_atomic_temp(tmp_path: Path) -> None:
    # a half-copied original.part.<ext> must not be treated as the staged source
    # (else a non-force re-ingest pairs a partial video with stale audio) (CodeX P2).
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.part.mp4").write_bytes(b"partial")
    assert paths.original_video() is None
    (paths.video / "original.mp4").write_bytes(b"complete")
    video = paths.original_video()
    assert video is not None and video.name == "original.mp4"


def test_align_persists_metadata_before_writing_aligned_wav(
    tmp_path: Path, monkeypatch,  # noqa: ANN001
) -> None:
    # each segment's metadata must be durable in segments.json BEFORE its aligned
    # wav exists, so a crash mid-align can't lose a needs_review flag (CodeX P2).
    res = FakeResolver()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en")
    stages.translate(paths, res, None, "zh", "en")
    paths.tts_raw(0, "wav").write_bytes(b"raw0")
    paths.tts_raw(1, "wav").write_bytes(b"raw1")

    monkeypatch.setattr(stages, "_media_duration_ms", lambda p: 3000)  # ratio 3 -> review
    monkeypatch.setattr(stages.ff, "assert_ffmpeg", lambda: None)

    seen: dict[int, bool] = {}

    def checking_canonical(src, out, chain=None):  # noqa: ANN001,ANN202
        idx = int(Path(out).name.split("_")[1])  # segment_000X_aligned.wav
        on_disk = read_json(paths.segments)["segments"]
        seen[idx] = on_disk[idx]["needs_review"]  # already persisted at wav-write time?
        Path(out).write_bytes(b"aligned")

    monkeypatch.setattr(stages.ff, "to_canonical_wav", checking_canonical)
    stages.align(paths, force=False)
    assert seen == {0: True, 1: True}


def test_align_force_failure_removes_stale_aligned(
    tmp_path: Path, monkeypatch,  # noqa: ANN001
) -> None:
    # a failed force re-align must not leave the old aligned wav behind (it would
    # satisfy the non-force cache check and mux stale audio on resume) (CodeX @PR P2).
    res = FakeResolver()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en")
    stages.translate(paths, res, None, "zh", "en")
    paths.tts_raw(0, "wav").write_bytes(b"raw0")
    paths.tts_raw(1, "wav").write_bytes(b"raw1")
    paths.tts_aligned(0).write_bytes(b"OLD-aligned")  # stale from a prior run

    monkeypatch.setattr(stages.ff, "assert_ffmpeg", lambda: None)
    monkeypatch.setattr(stages, "_media_duration_ms", lambda p: 500)  # ratio 0.5 -> fit

    def boom_canonical(src, out, chain=None):  # noqa: ANN001,ANN202,ARG001
        raise stages.ff.FfmpegError("ffmpeg killed")

    monkeypatch.setattr(stages.ff, "to_canonical_wav", boom_canonical)
    with pytest.raises(stages.ff.FfmpegError):
        stages.align(paths, force=True)
    assert not paths.tts_aligned(0).exists()  # stale aligned invalidated


def test_mux_force_clears_stale_pair_on_failure(
    tmp_path: Path, monkeypatch,  # noqa: ANN001
) -> None:
    # a failed forced mux must not leave the old mp4+srt pair as a "cached" set
    # (it would return stale deliverables on a non-force resume) (CodeX @PR P2).
    paths = JobPaths(tmp_path).ensure()
    (paths.video / "original.mp4").write_bytes(b"vid")
    write_json(paths.segments, TranslationResult(
        source_language="en", target_language="zh", mt_provider="m", segments=[],
    ).model_dump())
    paths.dubbed_video.write_bytes(b"OLD-mp4")
    paths.subtitles.write_text("OLD srt", encoding="utf-8")

    monkeypatch.setattr(stages.ff, "assert_ffmpeg", lambda: None)
    monkeypatch.setattr(stages.ff, "assert_allowed_input_format", lambda v: None)  # noqa: ARG005
    monkeypatch.setattr(stages.ff, "probe_duration_ms", lambda p: 1000)  # noqa: ARG005
    monkeypatch.setattr(stages.ff, "stitch_timeline",
                        lambda placements, out, total: Path(out).write_bytes(b"a"))  # noqa: ARG005

    def boom_mux(*a, **k):  # noqa: ANN002,ANN003,ANN202
        raise stages.ff.FfmpegError("ffmpeg killed")

    monkeypatch.setattr(stages.ff, "mux", boom_mux)
    with pytest.raises(stages.ff.FfmpegError):
        stages.mux(paths, force=True)
    assert not paths.dubbed_video.exists()  # stale deliverables cleared
    assert not paths.subtitles.exists()


def test_prepare_full_mix_clears_stale_ambient(tmp_path: Path) -> None:
    # full-mix speech (no separation) must drop a prior separated run's ambient so
    # mux(keep_ambient=True) can't mix stale background (CodeX P2).
    paths = JobPaths(tmp_path).ensure()
    paths.original_audio.write_bytes(b"orig")
    paths.ambient.write_bytes(b"stale-ambient")  # leftover from a prior separated run
    stages.prepare(paths, separate=False, force=True)
    assert paths.speech.exists()
    assert not paths.ambient.exists()


def test_tts_all_raw_cached_skips_provider_resolution(tmp_path: Path) -> None:
    # a resumed job whose pending segments already have raw tts files must be a
    # no-op that never resolves a provider (availability may have changed) (CodeX P2).
    res = FakeResolver()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en")
    stages.translate(paths, res, None, "zh", "en")
    paths.tts_raw(0, "wav").write_bytes(b"raw0")
    paths.tts_raw(1, "wav").write_bytes(b"raw1")

    class FailingResolver:
        def select(self, kind, requested, allow_paid):  # noqa: ANN001,ANN202,ARG002
            raise AssertionError("provider must not be resolved on a fully-cached resume")

    tr = stages.tts(paths, FailingResolver(), None)  # must NOT raise
    assert len(tr.segments) == 2


def test_translate_raises_on_mt_count_mismatch(tmp_path: Path) -> None:
    # the provider contract is 1 output per input line; a short batch must surface
    # as an error, not silently leave segments untranslated (CodeX P2).
    class ShortMt(_FakeMt):
        def translate(self, texts, source_lang, target_lang, budgets_ms=None):  # noqa: ANN001,ARG002
            return ["only one"]  # 2 input lines, 1 output

    res = FakeResolver()
    res.mt = ShortMt()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en")
    with pytest.raises(RuntimeError, match="expected 1:1"):
        stages.translate(paths, res, None, "zh", "en")


def test_tts_force_clears_stale_raw_variant(tmp_path: Path) -> None:
    # a force re-run after a prior provider wrote a different extension must drop
    # the stale raw so align()'s find_tts_raw() can't pick it up (CodeX P2).
    res = FakeResolver()  # _FakeTts.ext == "wav"
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en")
    stages.translate(paths, res, None, "zh", "en")
    stale = paths.tts_raw(0, "mp3")  # simulate a prior provider's .mp3 output
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"old")

    stages.tts(paths, res, None, force=True)

    assert not stale.exists()                 # stale .mp3 removed
    assert paths.tts_raw(0, "wav").exists()    # fresh .wav written
    raws = [p for p in paths.tts.glob("segment_0000.*") if not p.name.endswith("_aligned.wav")]
    assert len(raws) == 1                       # exactly one raw variant remains
