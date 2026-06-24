"""Characterization tests for the provider-using stages (transcribe / translate /
tts) driven through the injected ``Resolver`` seam with in-test fakes.

No ffmpeg, no network, no real providers — this both covers the orchestration
logic and documents the contract that ``provider-adapters`` (T1.2) must satisfy.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from autodub_core import JobPaths, stages
from autodub_core.jsonio import read_json
from autodub_core.providers import ProviderUnavailable
from ovt_schemas.contracts import Transcript, TranscriptLine


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
    result = stages.transcribe(paths, FakeResolver(), None, "en", allow_paid=False)
    assert paths.transcript.exists()
    assert len(result.lines) == 2
    reloaded = Transcript.model_validate(read_json(paths.transcript))
    assert reloaded.lines[1].speaker_id == "SPEAKER_01"
    assert reloaded.asr_provider == "fake_asr"


def test_translate_builds_segments_with_budgets(tmp_path: Path) -> None:
    res = FakeResolver()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en", allow_paid=False)
    tr = stages.translate(paths, res, None, "zh", "en", allow_paid=False)
    assert tr.target_language == "zh"
    assert [s.target_text for s in tr.segments] == ["HELLO", "WORLD"]
    assert all(not s.keep_original for s in tr.segments)
    assert tr.segments[0].target_duration_ms == 1000
    assert res.mt.last_budgets == [1000, 1000]  # per-line duration budgets
    assert paths.segments.exists()


def test_translate_empty_target_sets_keep_original(tmp_path: Path) -> None:
    class EmptyMt(_FakeMt):
        def translate(self, texts, source_lang, target_lang, budgets_ms=None):  # noqa: ANN001,ARG002
            return ["" for _ in texts]

    res = FakeResolver()
    res.mt = EmptyMt()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en", allow_paid=False)
    tr = stages.translate(paths, res, None, "zh", "en", allow_paid=False)
    assert all(s.keep_original for s in tr.segments)


def test_tts_assigns_voices_round_robin_and_synthesizes(tmp_path: Path) -> None:
    res = FakeResolver()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en", allow_paid=False)
    stages.translate(paths, res, None, "zh", "en", allow_paid=False)
    tr = stages.tts(paths, res, None, allow_paid=False)
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
    stages.transcribe(paths, res, None, "en", allow_paid=False)
    stages.translate(paths, res, None, "zh", "en", allow_paid=False)
    tr = stages.tts(paths, res, None, allow_paid=False)
    assert res.tts.calls == []  # nothing synthesized when every segment is keep_original
    assert all(s.keep_original for s in tr.segments)


def test_tts_no_voice_for_language_raises(tmp_path: Path) -> None:
    class NoVoiceTts(_FakeTts):
        def voices_for(self, lang: str) -> list[str]:
            return []

    res = FakeResolver()
    res.tts = NoVoiceTts()
    paths = JobPaths(tmp_path).ensure()
    stages.transcribe(paths, res, None, "en", allow_paid=False)
    stages.translate(paths, res, None, "zh", "en", allow_paid=False)
    with pytest.raises(ProviderUnavailable):
        stages.tts(paths, res, None, allow_paid=False)


def test_ingest_rejects_url_as_input_mode_error(tmp_path: Path) -> None:
    # the kernel is local-file only and network-free; a URL is an input-mode
    # error (ValueError), not a provider-availability one. URL ingest is the
    # local-runner CLI's job (T1.4).
    paths = JobPaths(tmp_path).ensure()
    with pytest.raises(ValueError, match="local file path only"):
        stages.ingest(paths, "https://example.com/video.mp4")
