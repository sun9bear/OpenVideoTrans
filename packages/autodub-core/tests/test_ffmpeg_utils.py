"""Golden tests for the pure (ffmpeg-free) helpers: the atempo decomposition
and the stdlib-wave timeline stitcher. These lock the kernel's deterministic
audio-assembly behavior; they need no ffmpeg binary.
"""
from __future__ import annotations

import wave
from pathlib import Path

import pytest
from autodub_core import ffmpeg_utils as ff
from autodub_core.config import CANON_CHANNELS, CANON_SR
from autodub_core.jsonio import atomic_output


def _make_wav(path: Path, ms: int) -> Path:
    """Write a canonical-format (48k mono s16) wav of exactly ``ms`` duration."""
    n = int(round(ms * CANON_SR / 1000))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CANON_CHANNELS)
        w.setsampwidth(2)
        w.setframerate(CANON_SR)
        w.writeframes(b"\x01\x00" * n)  # nonzero PCM; content is irrelevant to timing
    return path


def _make_wav_with(path: Path, channels: int, sampwidth: int, rate: int, ms: int = 50) -> Path:
    """Write a wav with arbitrary (channels, sampwidth, rate) — i.e. a *non-canonical*
    input, used to prove the stitcher refuses to splice un-normalized segments."""
    n = int(round(ms * rate / 1000))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(b"\x00" * (n * channels * sampwidth))
    return path


# ── atempo decomposition ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("ratio", "max_total", "expected"),
    [
        (0.5, 4.0, [1.0]),       # slowing/no-op clamps up to 1.0
        (1.0, 4.0, [1.0]),
        (1.5, 4.0, [1.5]),
        (2.0, 4.0, [2.0]),
        (3.0, 4.0, [2.0, 1.5]),  # >2 splits into <=2.0 steps
        (4.0, 4.0, [2.0, 2.0]),
        (4.0, 2.0, [2.0]),       # capped by max_total
        (8.0, 8.0, [2.0, 2.0, 2.0]),
    ],
)
def test_atempo_chain_decomposition(ratio: float, max_total: float, expected: list[float]) -> None:
    chain = ff.atempo_chain_for(ratio, max_total)
    assert chain == expected
    # every step is a legal single atempo filter value
    assert all(0.5 <= step <= 2.0 for step in chain)


# ── wave duration round-trip ─────────────────────────────────────────────────
def test_wav_duration_ms_roundtrip(tmp_path: Path) -> None:
    p = _make_wav(tmp_path / "a.wav", 250)
    assert ff.wav_duration_ms(p) == 250


# ── timeline stitcher ────────────────────────────────────────────────────────
def test_stitch_places_segments_with_gap_silence(tmp_path: Path) -> None:
    a = _make_wav(tmp_path / "a.wav", 100)
    b = _make_wav(tmp_path / "b.wav", 100)
    out = tmp_path / "out.wav"
    # a at 0 (100ms), b at 500 -> 400ms gap silence between, padded to 1000ms total
    ff.stitch_timeline([(0, a), (500, b)], out, total_ms=1000)
    assert ff.wav_duration_ms(out) == 1000
    nchan, sampwidth, rate = ff.wav_params(out)
    assert (nchan, sampwidth, rate) == (CANON_CHANNELS, 2, CANON_SR)


def test_stitch_pads_to_total_ms_when_segments_short(tmp_path: Path) -> None:
    a = _make_wav(tmp_path / "a.wav", 100)
    out = tmp_path / "out.wav"
    ff.stitch_timeline([(0, a)], out, total_ms=2000)
    assert ff.wav_duration_ms(out) == 2000


def test_stitch_appends_back_to_back_on_overrun(tmp_path: Path) -> None:
    a = _make_wav(tmp_path / "a.wav", 300)
    b = _make_wav(tmp_path / "b.wav", 100)
    out = tmp_path / "out.wav"
    # b is anchored at 100ms but a runs to 300ms: b is appended back-to-back at
    # 300ms (drift accepted, never overlapped). total_ms=0 -> no trailing pad.
    ff.stitch_timeline([(0, a), (100, b)], out, total_ms=0)
    assert ff.wav_duration_ms(out) == 400


def test_stitch_sorts_unordered_placements(tmp_path: Path) -> None:
    a = _make_wav(tmp_path / "a.wav", 100)
    b = _make_wav(tmp_path / "b.wav", 100)
    out = tmp_path / "out.wav"
    # placements given out of order are sorted by start_ms before assembly
    ff.stitch_timeline([(500, b), (0, a)], out, total_ms=1000)
    assert ff.wav_duration_ms(out) == 1000


def _frame_at_ms(path: Path, ms: int) -> bytes:
    """Read the single PCM frame at ``ms`` from a canonical wav."""
    with wave.open(str(path), "rb") as w:
        w.setpos(int(round(ms * w.getframerate() / 1000)))
        return w.readframes(1)


def test_to_canonical_wav_atomic_on_failure(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # a failed/killed ffmpeg must leave NO partial file at the cache path, so
    # align() can't treat a truncated _aligned.wav as done on resume (CodeX P2).
    out = tmp_path / "segment_0000_aligned.wav"

    def boom(cmd):  # noqa: ANN001,ANN202,ARG001
        raise ff.FfmpegError("ffmpeg killed")

    monkeypatch.setattr(ff, "_run", boom)
    with pytest.raises(ff.FfmpegError):
        ff.to_canonical_wav(tmp_path / "in.wav", out)
    assert not out.exists()                                    # no partial at cache path
    assert not out.with_name(out.name + ".part").exists()      # temp cleaned up


def test_extract_audio_atomic_on_failure(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # a failed/killed ffmpeg must leave NO partial wav at the cache path, so
    # ingest/prepare resume can't skip with corrupt cached audio (CodeX P2).
    out = tmp_path / "original.wav"

    def boom(cmd):  # noqa: ANN001,ANN202,ARG001
        raise ff.FfmpegError("ffmpeg killed")

    monkeypatch.setattr(ff, "_run", boom)
    with pytest.raises(ff.FfmpegError):
        ff.extract_audio(tmp_path / "in.mp4", out)
    assert not out.exists()
    assert not out.with_name(out.name + ".part").exists()


def test_atomic_output_replaces_on_success(tmp_path: Path) -> None:
    final = tmp_path / "out.bin"
    with atomic_output(final) as tmp:
        tmp.write_bytes(b"data")
        assert tmp != final and tmp.exists()  # writes go to the temp first
    assert final.read_bytes() == b"data"       # replaced atomically on clean exit
    assert not tmp.exists()                     # temp cleaned up


def test_atomic_output_leaves_no_partial_on_failure(tmp_path: Path) -> None:
    final = tmp_path / "out.bin"
    with pytest.raises(RuntimeError), atomic_output(final) as tmp:
        tmp.write_bytes(b"partial")
        raise RuntimeError("boom")
    assert not final.exists()  # no partial at the cache path
    assert not tmp.exists()    # temp cleaned up


def test_mux_atomic_on_failure(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # a failed mux must leave NO partial mp4 at the cache path, so it can't pair
    # with an existing subtitles.srt and look cached on the next run (CodeX P2).
    out = tmp_path / "dubbed_video.mp4"

    def boom(cmd):  # noqa: ANN001,ANN202,ARG001
        raise ff.FfmpegError("ffmpeg killed")

    monkeypatch.setattr(ff, "_run", boom)
    with pytest.raises(ff.FfmpegError):
        ff.mux(tmp_path / "v.mp4", tmp_path / "a.wav", out)
    assert not out.exists()
    assert not out.with_name(out.stem + ".part" + out.suffix).exists()


def test_stitch_frame_level_placement(tmp_path: Path) -> None:
    # _make_wav writes nonzero PCM (b"\x01\x00"); stitch fills gaps with zeros.
    # Verify segments land at their start_ms with silence in the gap and tail —
    # i.e. assert placement, not just total duration.
    a = _make_wav(tmp_path / "a.wav", 100)
    b = _make_wav(tmp_path / "b.wav", 100)
    out = tmp_path / "out.wav"
    ff.stitch_timeline([(0, a), (500, b)], out, total_ms=1000)
    nonzero, silence = b"\x01\x00", b"\x00\x00"
    assert _frame_at_ms(out, 50) == nonzero    # inside segment a (0-100ms)
    assert _frame_at_ms(out, 300) == silence   # gap silence (100-500ms)
    assert _frame_at_ms(out, 550) == nonzero   # inside segment b (500-600ms)
    assert _frame_at_ms(out, 800) == silence   # trailing pad to total_ms


# ── stitch rejects non-canonical segments (T1.1 defensive gap, T1.3 Batch A) ──
@pytest.mark.parametrize(
    ("channels", "sampwidth", "rate", "why"),
    [
        (CANON_CHANNELS, 2, 16000, "wrong sample rate (16k vs 48k)"),
        (2, 2, CANON_SR, "stereo (wrong channel count)"),
        (CANON_CHANNELS, 1, CANON_SR, "8-bit (wrong sample width)"),
    ],
)
def test_stitch_rejects_noncanonical_segment(
    tmp_path: Path, channels: int, sampwidth: int, rate: int, why: str
) -> None:
    # A segment that escaped to_canonical_wav (e.g. 16kHz or stereo) would be spliced
    # in raw and silently corrupt that span's pitch/tempo with no error. The stitcher
    # must refuse it instead, and (atomic_output) leave no partial composed track.
    bad = _make_wav_with(tmp_path / "bad.wav", channels, sampwidth, rate)
    out = tmp_path / "out.wav"
    with pytest.raises(ff.FfmpegError, match="canonical"):
        ff.stitch_timeline([(0, bad)], out, total_ms=500)
    assert not out.exists(), f"no partial composed track on {why}"
    assert not out.with_name(out.stem + ".part" + out.suffix).exists()


def test_stitch_rejects_noncanonical_segment_among_good_ones(tmp_path: Path) -> None:
    # A good segment is written first, then a non-canonical one is reached mid-loop:
    # the raise must still abort and discard the (now non-empty) temp entirely.
    good = _make_wav(tmp_path / "good.wav", 100)
    bad = _make_wav_with(tmp_path / "bad.wav", CANON_CHANNELS, 2, 16000)
    out = tmp_path / "out.wav"
    with pytest.raises(ff.FfmpegError, match="canonical"):
        ff.stitch_timeline([(0, good), (200, bad)], out, total_ms=1000)
    assert not out.exists()
    assert not out.with_name(out.stem + ".part" + out.suffix).exists()


def test_stitch_accepts_canonical_segments(tmp_path: Path) -> None:
    # The new guard must not reject genuinely canonical inputs (regression guard).
    a = _make_wav(tmp_path / "a.wav", 100)
    out = tmp_path / "out.wav"
    ff.stitch_timeline([(0, a)], out, total_ms=500)
    assert ff.wav_duration_ms(out) == 500


# ── probe_duration_ms wraps malformed ffprobe output (T1.1 gap, T1.3 Batch A) ──
@pytest.mark.parametrize(
    "bad_stdout",
    [
        "not json at all",                      # JSONDecodeError (⊂ ValueError)
        "",                                     # empty stdout -> JSONDecodeError
        "{}",                                   # missing "format" key -> KeyError
        '{"format": {}}',                       # missing "duration" key -> KeyError
        '{"format": {"duration": "N/A"}}',      # non-numeric duration -> ValueError
        '{"format": {"duration": null}}',       # null duration -> TypeError on float()
        "[]",                                   # JSON array, not object -> TypeError
    ],
)
def test_probe_duration_ms_wraps_malformed_output(monkeypatch, bad_stdout: str) -> None:  # noqa: ANN001
    # Malformed ffprobe stdout must surface as FfmpegError (one error type for callers),
    # never a bare KeyError/JSONDecodeError leaking out of the helper.
    def fake_run(cmd):  # noqa: ANN001,ANN202,ARG001
        return bad_stdout

    monkeypatch.setattr(ff, "_run", fake_run)
    with pytest.raises(ff.FfmpegError):
        ff.probe_duration_ms("anything.mp4")


def test_probe_duration_ms_error_includes_raw_output(monkeypatch) -> None:  # noqa: ANN001
    # The wrapped error must carry the raw ffprobe output for diagnosis.
    def fake_run(cmd):  # noqa: ANN001,ANN202,ARG001
        return '{"format": {"duration": "N/A"}}'

    monkeypatch.setattr(ff, "_run", fake_run)
    with pytest.raises(ff.FfmpegError, match="N/A"):
        ff.probe_duration_ms("anything.mp4")


def test_probe_duration_ms_parses_well_formed_output(monkeypatch) -> None:  # noqa: ANN001
    # The wrapping must not change the happy path (regression guard).
    def fake_run(cmd):  # noqa: ANN001,ANN202,ARG001
        return '{"format": {"duration": "12.5"}}'

    monkeypatch.setattr(ff, "_run", fake_run)
    assert ff.probe_duration_ms("anything.mp4") == 12500
