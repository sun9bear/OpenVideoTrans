"""Golden tests for the pure alignment decision (assign_timing).

This is the deterministic timing-assignment logic the align stage relies on:
how a synthesized clip is fitted into its target slot, capped at MAX_SPEEDUP.
No ffmpeg, no I/O — it locks the kernel's behavior independent of the box.
"""
from __future__ import annotations

import pytest
from autodub_core import assign_timing


def test_fit_when_clip_shorter_than_slot() -> None:
    p = assign_timing(500, 1000)  # ratio 0.5
    assert p.method == "fit"
    assert p.atempo_chain is None
    assert p.align_ratio == 0.5
    assert p.needs_review is False


def test_exact_fit_ratio_one() -> None:
    p = assign_timing(1000, 1000)  # ratio 1.0 (boundary, still "fit")
    assert p.method == "fit"
    assert p.atempo_chain is None
    assert p.align_ratio == 1.0
    assert p.needs_review is False


def test_dsp_within_max_speedup() -> None:
    p = assign_timing(1500, 1000)  # ratio 1.5
    assert p.method == "dsp"
    assert p.atempo_chain == [1.5]
    assert p.align_ratio == 1.5
    assert p.needs_review is False


def test_dsp_at_max_speedup_boundary() -> None:
    p = assign_timing(2000, 1000)  # ratio 2.0 == MAX_SPEEDUP -> still dsp, no review
    assert p.method == "dsp"
    assert p.atempo_chain == [2.0]
    assert p.align_ratio == 2.0
    assert p.needs_review is False


def test_force_dsp_beyond_max_flags_review() -> None:
    p = assign_timing(3000, 1000)  # ratio 3.0 > MAX_SPEEDUP 2.0
    assert p.method == "force_dsp"
    assert p.atempo_chain == [2.0]   # speed-up capped to MAX_SPEEDUP
    assert p.align_ratio == 3.0      # but the true (uncapped) ratio is reported
    assert p.needs_review is True


def test_zero_target_uses_floor_of_one_ms() -> None:
    p = assign_timing(1000, 0)  # target clamped to >=1ms -> huge ratio
    assert p.method == "force_dsp"
    assert p.align_ratio == 1000.0
    assert p.needs_review is True


def test_force_dsp_just_over_max_boundary() -> None:
    p = assign_timing(2001, 1000)  # ratio 2.001, barely past MAX_SPEEDUP 2.0
    assert p.method == "force_dsp"   # strictly > max -> force, not dsp
    assert p.atempo_chain == [2.0]   # speed-up capped exactly at MAX_SPEEDUP
    assert p.align_ratio == 2.001    # true (uncapped) ratio still reported
    assert p.needs_review is True


@pytest.mark.parametrize(
    ("actual_ms", "target_ms", "max_speedup", "method", "chain", "review"),
    [
        (3000, 1000, 4.0, "dsp", [2.0, 1.5], False),   # ratio 3 <= custom max 4
        (5000, 1000, 4.0, "force_dsp", [2.0, 2.0], True),  # ratio 5 > 4 -> capped to 4
    ],
)
def test_custom_max_speedup(
    actual_ms: int, target_ms: int, max_speedup: float,
    method: str, chain: list[float], review: bool,
) -> None:
    p = assign_timing(actual_ms, target_ms, max_speedup=max_speedup)
    assert p.method == method
    assert p.atempo_chain == chain
    assert p.needs_review is review
