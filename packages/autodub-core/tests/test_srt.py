"""Golden tests for SRT writing: timestamp format, numbering, empty-cue skip,
and the target->source text fallback. Pure (no ffmpeg, no providers).
"""
from __future__ import annotations

from pathlib import Path

from autodub_core import stages
from ovt_schemas.contracts import DubbingSegment, TranslationResult


def _seg(i: int, start: int, end: int, target: str = "", source: str = "") -> DubbingSegment:
    return DubbingSegment(
        segment_id=f"seg_{i:04d}", index=i, speaker_id="SPEAKER_00",
        start_ms=start, end_ms=end, target_duration_ms=max(0, end - start),
        source_text=source, target_text=target,
    )


def test_srt_format_and_numbering(tmp_path: Path) -> None:
    result = TranslationResult(
        source_language="en", target_language="zh", mt_provider="fake",
        segments=[
            _seg(0, 0, 1500, target="你好"),
            _seg(1, 1500, 3200, target="世界"),
        ],
    )
    out = tmp_path / "subs.srt"
    stages._write_srt(result, out)
    expected = (
        "1\n00:00:00,000 --> 00:00:01,500\n你好\n\n"
        "2\n00:00:01,500 --> 00:00:03,200\n世界\n"
    )
    assert out.read_text(encoding="utf-8") == expected


def test_srt_skips_empty_and_falls_back_to_source(tmp_path: Path) -> None:
    result = TranslationResult(
        source_language="en", target_language="zh", mt_provider="fake",
        segments=[
            _seg(0, 0, 1000, target="", source=""),         # both empty -> skipped
            _seg(1, 1000, 2000, target="", source="hello"),  # no target -> source fallback
            _seg(2, 2000, 3000, target="bonjour"),
        ],
    )
    out = tmp_path / "s.srt"
    stages._write_srt(result, out)
    content = out.read_text(encoding="utf-8")
    # the empty segment is dropped and numbering restarts from the first kept cue
    assert content.startswith("1\n00:00:01,000 --> 00:00:02,000\nhello\n")
    assert "bonjour" in content
    assert content.count("-->") == 2


def test_srt_timestamp_rolls_over_hours_and_minutes(tmp_path: Path) -> None:
    # 1h 02m 03s 004ms = 3_723_004 ms
    result = TranslationResult(
        source_language="en", target_language="zh", mt_provider="fake",
        segments=[_seg(0, 3_723_004, 3_724_000, target="x")],
    )
    out = tmp_path / "s.srt"
    stages._write_srt(result, out)
    assert "01:02:03,004 -->" in out.read_text(encoding="utf-8")
