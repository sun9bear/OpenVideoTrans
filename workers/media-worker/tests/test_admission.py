"""T2.4 — worker ffprobe re-admission (the authoritative source gate). Test-first.

The control plane's POST /api/jobs HEAD-checks the uploaded object's size/type, but the presigned
PUT stays valid for its short TTL, so a client can swap a larger / wrong / over-long object after
that HEAD and before the worker claims (a TOCTOU swap; routed from T2.1). admit_source re-reads the
ACTUAL downloaded bytes at claim and re-checks, authoritatively:

* container format / SSRF allowlist (reuses autodub-core's T1.3c guard) -> unsupported_format;
* actual byte size vs cap (closes the post-HEAD size swap)             -> upload_too_large;
* duration vs the per-output_mode cap (browser advisory is sort-only)  -> over_duration.

A rejection raises SourceRejected(error_code); the worker deletes the source + fails the job.
"""
from __future__ import annotations

from pathlib import Path

import media_worker.admission as adm
import pytest
from autodub_core import ffmpeg_utils as ff
from media_worker.admission import SourceRejected, admit_source
from mw_fakes import TEST_CONFIG, make_job


def _write(path: Path, size: int) -> Path:
    path.write_bytes(b"\x00" * size)
    return path


@pytest.fixture
def valid_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: an allowed container + a short duration, so each test overrides ONE thing."""
    monkeypatch.setattr(adm.ff, "assert_allowed_input_format", lambda p: None)
    monkeypatch.setattr(adm.ff, "probe_duration_ms", lambda p: 10_000)


def test_admits_a_valid_small_short_source(tmp_path: Path, valid_probe: None) -> None:
    src = _write(tmp_path / "in", 1024)
    admit_source(src, make_job(output_mode="dub_only"), TEST_CONFIG)  # must not raise


def test_rejects_disguised_playlist_unsupported_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # assert_allowed_input_format raises (a hls/concat/network demuxer) -> unsupported_format.
    def _raise(p: object) -> None:
        raise ff.FfmpegError("input container format 'hls,applehttp' ... (SSRF guard, T1.3c).")

    monkeypatch.setattr(adm.ff, "assert_allowed_input_format", _raise)
    monkeypatch.setattr(adm.ff, "probe_duration_ms", lambda p: 1_000)
    src = _write(tmp_path / "in", 64)
    with pytest.raises(SourceRejected) as ei:
        admit_source(src, make_job(output_mode="dub_only"), TEST_CONFIG)
    assert ei.value.error_code == "unsupported_format"


def test_format_check_runs_before_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the SSRF/format allowlist must gate BEFORE duration probing, so a disguised playlist's
    # demuxer is never asked to compute a duration (it could dereference sub-resources).
    def _raise(p: object) -> None:
        raise ff.FfmpegError("SSRF guard")

    def _no_probe(p: object) -> int:
        raise AssertionError("probe_duration_ms must not run for a refused format")

    monkeypatch.setattr(adm.ff, "assert_allowed_input_format", _raise)
    monkeypatch.setattr(adm.ff, "probe_duration_ms", _no_probe)
    with pytest.raises(SourceRejected):
        admit_source(_write(tmp_path / "in", 32), make_job(), TEST_CONFIG)


def test_rejects_oversized_actual_bytes_upload_too_large(
    tmp_path: Path, valid_probe: None
) -> None:
    # the real downloaded bytes exceed the cap even though the control-plane HEAD passed (a swap).
    src = _write(tmp_path / "in", TEST_CONFIG.max_upload_bytes + 1)
    with pytest.raises(SourceRejected) as ei:
        admit_source(src, make_job(output_mode="dub_only"), TEST_CONFIG)
    assert ei.value.error_code == "upload_too_large"


def test_rejects_over_duration_for_dub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adm.ff, "assert_allowed_input_format", lambda p: None)
    # 301s > the 300s dub cap.
    monkeypatch.setattr(adm.ff, "probe_duration_ms", lambda p: 301_000)
    with pytest.raises(SourceRejected) as ei:
        admit_source(_write(tmp_path / "in", 64), make_job(output_mode="dub_only"), TEST_CONFIG)
    assert ei.value.error_code == "over_duration"


def test_subtitle_only_allows_longer_than_dub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 600s passes the 1800s subtitle-only cap but would fail the 300s dub cap: the cap is per-mode.
    monkeypatch.setattr(adm.ff, "assert_allowed_input_format", lambda p: None)
    monkeypatch.setattr(adm.ff, "probe_duration_ms", lambda p: 600_000)
    src = _write(tmp_path / "in", 64)
    admit_source(src, make_job(output_mode="subtitle_only"), TEST_CONFIG)  # ok at 1800s cap
    with pytest.raises(SourceRejected) as ei:
        admit_source(src, make_job(output_mode="dub_only"), TEST_CONFIG)
    assert ei.value.error_code == "over_duration"


def test_both_mode_uses_the_tighter_dub_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `both` produces dubbed audio, so it is bound by the tighter TTS wall-time (dub) cap.
    monkeypatch.setattr(adm.ff, "assert_allowed_input_format", lambda p: None)
    monkeypatch.setattr(adm.ff, "probe_duration_ms", lambda p: 301_000)
    with pytest.raises(SourceRejected) as ei:
        admit_source(_write(tmp_path / "in", 64), make_job(output_mode="both"), TEST_CONFIG)
    assert ei.value.error_code == "over_duration"


def test_rejects_burned_subtitles_on_audio_only_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # subtitle_only + burned on an audio-only upload: probe_dimensions raises (no video stream) ->
    # a coded unsupported_format terminal, not a deep internal_error re-encode (M2.1, CodeX P2).
    monkeypatch.setattr(adm.ff, "assert_allowed_input_format", lambda p: None)
    monkeypatch.setattr(adm.ff, "probe_duration_ms", lambda p: 10_000)

    def _no_video(p: object) -> tuple[int, int]:
        raise adm.ff.FfmpegError("no video stream")

    monkeypatch.setattr(adm.ff, "probe_dimensions", _no_video)
    with pytest.raises(SourceRejected) as ei:
        admit_source(
            _write(tmp_path / "in", 32),
            make_job(output_mode="subtitle_only", subtitle_delivery="burned"),
            TEST_CONFIG,
        )
    assert ei.value.error_code == "unsupported_format"


def test_admits_burned_subtitles_on_a_video_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the same burn job on a real video source (probe_dimensions returns dims) is admitted.
    monkeypatch.setattr(adm.ff, "assert_allowed_input_format", lambda p: None)
    monkeypatch.setattr(adm.ff, "probe_duration_ms", lambda p: 10_000)
    monkeypatch.setattr(adm.ff, "probe_dimensions", lambda p: (1920, 1080))
    admit_source(
        _write(tmp_path / "in", 32),
        make_job(output_mode="subtitle_only", subtitle_delivery="burned"),
        TEST_CONFIG,
    )  # must not raise


def test_burned_subtitle_uses_the_tighter_dub_duration_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A burned subtitle_only job runs a video re-encode, so it is bound by the DUB cap (300s), not
    # the loose srt-only cap (1800s): 600s passes srt-only but is rejected for a burn (bot P2).
    monkeypatch.setattr(adm.ff, "assert_allowed_input_format", lambda p: None)
    monkeypatch.setattr(adm.ff, "probe_duration_ms", lambda p: 600_000)  # 10 min: > 300s dub cap
    with pytest.raises(SourceRejected) as ei:
        admit_source(
            _write(tmp_path / "in", 32),
            make_job(output_mode="subtitle_only", subtitle_delivery="burned"),
            TEST_CONFIG,
        )
    assert ei.value.error_code == "over_duration"


def test_duration_cap_sec_mapping() -> None:
    assert TEST_CONFIG.duration_cap_sec("subtitle_only") == 1800
    assert TEST_CONFIG.duration_cap_sec("dub_only") == 300
    assert TEST_CONFIG.duration_cap_sec("both") == 300
    # unknown mode -> the tightest cap (fail-safe), never an unbounded default.
    assert TEST_CONFIG.duration_cap_sec("nonsense") == 300
