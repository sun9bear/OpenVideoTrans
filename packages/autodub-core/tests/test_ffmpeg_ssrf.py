"""T1.3c — ffmpeg/ffprobe SSRF hardening. Test-first (red-line bucket):

* every ffmpeg/ffprobe invocation restricts demuxer protocols to ``file,crypto``
  (no http/tcp/…), so a media file that is secretly a playlist can't make ffmpeg
  touch the network; and
* the input container format is allowlisted, so a disguised playlist / concat /
  network demuxer is refused before processing.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from autodub_core import JobPaths, stages
from autodub_core import ffmpeg_utils as ff


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _assert_no_network_protocol(cmd: list[str]) -> None:
    """Every -protocol_whitelist in the command whitelists ONLY file/crypto."""
    found = False
    for k, arg in enumerate(cmd):
        if arg == "-protocol_whitelist":
            found = True
            assert set(cmd[k + 1].split(",")) <= {"file", "crypto"}, cmd
    assert found, f"command does not restrict protocols: {cmd}"


def _assert_inputs_protocol_restricted(cmd: list[str]) -> None:
    """Each ``-i PATH`` is immediately preceded by ``-protocol_whitelist file,crypto``."""
    i_positions = [k for k, a in enumerate(cmd) if a == "-i"]
    assert i_positions, f"no -i input in {cmd}"
    for k in i_positions:
        assert cmd[k - 2] == "-protocol_whitelist", cmd
        assert cmd[k - 1] == "file,crypto", cmd
    _assert_no_network_protocol(cmd)


def _ffmpeg_capture(captured: list[list[str]]):  # noqa: ANN202
    """A fake _run that records the command and creates the output temp (last arg)
    so the surrounding atomic_output can replace its target."""
    def run(cmd: list[str]) -> str:
        captured.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"\x00")
        return ""
    return run


# --------------------------------------------------------------------------- #
# format allowlist (pure)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "ok",
    ["mov,mp4,m4a,3gp,3g2,mj2", "matroska,webm", "wav", "mp3", "avi", "MOV,MP4"],
)
def test_validate_format_name_accepts_real_containers(ok: str) -> None:
    ff.validate_format_name(ok)  # must not raise


@pytest.mark.parametrize(
    "bad",
    ["hls,applehttp", "concat", "ffconcat", "image2", "image2pipe", "rtsp",
     "rtp", "sdp", "data", "http", "dash", "", "mp4,concat"],
)
def test_validate_format_name_rejects_playlist_and_network(bad: str) -> None:
    with pytest.raises(ff.FfmpegError):
        ff.validate_format_name(bad)


def test_assert_allowed_input_format_uses_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ff, "probe_format_name", lambda p: "mov,mp4,m4a")  # noqa: ARG005
    ff.assert_allowed_input_format("x.mp4")  # no raise
    monkeypatch.setattr(ff, "probe_format_name", lambda p: "hls,applehttp")  # noqa: ARG005
    with pytest.raises(ff.FfmpegError, match="SSRF guard"):
        ff.assert_allowed_input_format("x.mp4")


@pytest.mark.parametrize("ext", [".m3u8", ".m3u", ".concat", ".ffconcat", ".pls", ".xspf"])
def test_assert_allowed_input_format_rejects_playlist_extension_before_probe(
    ext: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an honestly-named playlist/script is refused by extension BEFORE ffprobe ever
    # opens it, so the playlist demuxer never runs (CodeX R2 P1).
    def _no_probe(p: object) -> str:
        raise AssertionError("probe must not run for a playlist extension")

    monkeypatch.setattr(ff, "probe_format_name", _no_probe)
    with pytest.raises(ff.FfmpegError, match="before probe"):
        ff.assert_allowed_input_format(f"evil{ext}")


# --------------------------------------------------------------------------- #
# protocol whitelist on every ffmpeg/ffprobe command
# --------------------------------------------------------------------------- #
def test_extract_audio_restricts_protocols(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(ff, "_run", _ffmpeg_capture(captured))
    ff.extract_audio(tmp_path / "in.mp4", tmp_path / "out.wav")
    _assert_inputs_protocol_restricted(captured[0])


def test_to_canonical_wav_restricts_protocols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(ff, "_run", _ffmpeg_capture(captured))
    ff.to_canonical_wav(tmp_path / "in.wav", tmp_path / "out.wav", [1.5])
    _assert_inputs_protocol_restricted(captured[0])


def test_mux_restricts_protocols_on_all_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(ff, "_run", _ffmpeg_capture(captured))
    ff.mux(tmp_path / "v.mp4", tmp_path / "a.wav", tmp_path / "out.mp4")
    cmd = captured[0]
    _assert_inputs_protocol_restricted(cmd)
    assert cmd.count("-i") == 2  # video + audio, each protocol-restricted


def test_mux_with_ambient_restricts_all_three_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient = tmp_path / "ambient.wav"
    ambient.write_bytes(b"\x00")  # mux only mixes ambient when it exists
    captured: list[list[str]] = []
    monkeypatch.setattr(ff, "_run", _ffmpeg_capture(captured))
    ff.mux(tmp_path / "v.mp4", tmp_path / "a.wav", tmp_path / "out.mp4", ambient=ambient)
    cmd = captured[0]
    _assert_inputs_protocol_restricted(cmd)
    assert cmd.count("-i") == 3  # video + audio + ambient


def test_probe_duration_restricts_protocols(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(
        ff, "_run",
        lambda cmd: captured.append(list(cmd)) or '{"format": {"duration": "1.5"}}',
    )
    assert ff.probe_duration_ms("in.mp4") == 1500
    _assert_no_network_protocol(captured[0])


def test_probe_format_name_restricts_protocols(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(ff, "_run", lambda cmd: captured.append(list(cmd)) or "mov,mp4\n")
    assert ff.probe_format_name("in.mp4") == "mov,mp4"
    _assert_no_network_protocol(captured[0])


# --------------------------------------------------------------------------- #
# ingest wiring (the untrusted-source boundary)
# --------------------------------------------------------------------------- #
def test_ingest_refuses_disguised_playlist_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a source whose real container is a playlist (hls/applehttp) is refused at
    # ingest, before any audio is extracted (SSRF guard wiring).
    paths = JobPaths(tmp_path).ensure()
    staged = paths.video / "original.mp4"
    staged.write_bytes(b"not actually an mp4")
    monkeypatch.setattr(stages.ff, "assert_ffmpeg", lambda: None)
    monkeypatch.setattr(stages.ff, "probe_format_name", lambda p: "hls,applehttp")  # noqa: ARG005

    def _no_extract(*a: object, **k: object) -> None:
        raise AssertionError("extract_audio must not run for a refused source")

    monkeypatch.setattr(stages.ff, "extract_audio", _no_extract)
    with pytest.raises(ff.FfmpegError, match="SSRF guard"):
        stages.ingest(paths, str(staged))
    assert not paths.original_audio.exists()
