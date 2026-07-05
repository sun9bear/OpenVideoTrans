"""Whisper hallucination guard (asr.py). Whisper (esp. the base model) emits high-frequency
training-set boilerplate on non-speech segments (applause / music / silence) — often CJK
"subtitle-credit" text like 简体中文（大陆） even in an English recording. vad_filter alone does
not catch it, so a line that is predominantly CJK when the source language is NOT CJK is a
hallucination, not speech, and must be dropped before it reaches MT/TTS."""
from __future__ import annotations

import sys
import types

from ovt_schemas.contracts import TranscriptLine
from provider_adapters.asr import (
    FasterWhisperASR,
    _cjk_ratio,
    _drop_script_hallucinations,
)


def _line(index: int, text: str) -> TranscriptLine:
    return TranscriptLine(
        index=index, start_ms=index * 1000, end_ms=index * 1000 + 800,
        source_text=text, words=[], speaker_id="SPEAKER_00",
    )


class TestCjkRatio:
    def test_pure_english_is_zero(self) -> None:
        assert _cjk_ratio("Stay hungry, stay foolish.") == 0.0

    def test_accented_latin_is_zero(self) -> None:
        assert _cjk_ratio("café résumé naïve Straße") == 0.0

    def test_cjk_boilerplate_is_majority(self) -> None:
        assert _cjk_ratio("简体中文（大陆）") >= 0.5

    def test_japanese_credit_is_majority(self) -> None:
        assert _cjk_ratio("ご視聴ありがとうございました") >= 0.5

    def test_blank_is_zero(self) -> None:
        assert _cjk_ratio("   ") == 0.0

    def test_one_stray_cjk_in_english_stays_below_half(self) -> None:
        assert _cjk_ratio("the product is called 好") < 0.5


class TestDropScriptHallucinations:
    def test_drops_cjk_line_in_english_transcript_and_reindexes(self) -> None:
        lines = [
            _line(0, "Stay hungry, stay foolish."),
            _line(1, "简体中文（大陆）"),
            _line(2, "Thank you all very much."),
        ]
        out = _drop_script_hallucinations(lines, "en")
        assert [line.source_text for line in out] == [
            "Stay hungry, stay foolish.",
            "Thank you all very much.",
        ]
        assert [line.index for line in out] == [0, 1]  # contiguous re-index after the drop

    def test_noop_when_source_is_cjk(self) -> None:
        lines = [_line(0, "你好世界"), _line(1, "简体中文（大陆）")]
        out = _drop_script_hallucinations(lines, "zh-Hans")
        assert [line.source_text for line in out] == ["你好世界", "简体中文（大陆）"]

    def test_noop_when_source_unknown(self) -> None:
        lines = [_line(0, "简体中文（大陆）")]
        assert _drop_script_hallucinations(lines, "auto") == lines
        assert _drop_script_hallucinations(lines, None) == lines

    def test_unchanged_when_no_hallucination(self) -> None:
        lines = [_line(0, "Hello there."), _line(1, "General Kenobi.")]
        assert _drop_script_hallucinations(lines, "en") == lines

    def test_japanese_hallucination_dropped_in_spanish_source(self) -> None:
        lines = [_line(0, "Hola a todos"), _line(1, "ご視聴ありがとうございました")]
        out = _drop_script_hallucinations(lines, "es")
        assert [line.source_text for line in out] == ["Hola a todos"]


class TestFasterWhisperAppliesGuard:
    def _install_fake_whisper(self, monkeypatch, segs, detected: str) -> dict:  # noqa: ANN001
        calls: dict = {}

        class FakeModel:
            def __init__(self, *a, **k) -> None:  # noqa: ANN002, ANN003
                pass

            def transcribe(self, audio_path, **kwargs):  # noqa: ANN001, ANN003, ANN201
                calls.update(kwargs)
                return iter(segs), types.SimpleNamespace(language=detected)

        fake_mod = types.ModuleType("faster_whisper")
        fake_mod.WhisperModel = FakeModel  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)
        monkeypatch.setattr("provider_adapters.asr.has_module", lambda _name: True)
        return calls

    def test_transcribe_drops_cjk_hallucination_and_disables_context_bleed(
        self, monkeypatch
    ) -> None:
        def seg(start, end, text, words):  # noqa: ANN001, ANN202
            return types.SimpleNamespace(
                start=start, end=end, text=text,
                words=[types.SimpleNamespace(word=w, start=s, end=e) for (w, s, e) in words],
            )

        segs = [
            seg(0.0, 1.0, "Stay hungry.", [("Stay", 0.0, 0.4), ("hungry.", 0.5, 1.0)]),
            seg(1.2, 2.4, "简体中文（大陆）", [("简体中文（大陆）", 1.2, 2.4)]),
            seg(3.0, 4.0, "Thank you.", [("Thank", 3.0, 3.4), ("you.", 3.5, 4.0)]),
        ]
        calls = self._install_fake_whisper(monkeypatch, segs, detected="en")

        transcript = FasterWhisperASR().transcribe("/tmp/audio.wav", source_lang=None)

        assert calls.get("condition_on_previous_text") is False
        assert calls.get("vad_filter") is True
        assert transcript.source_language == "en"
        assert [line.source_text for line in transcript.lines] == ["Stay hungry.", "Thank you."]
        assert [line.index for line in transcript.lines] == [0, 1]
