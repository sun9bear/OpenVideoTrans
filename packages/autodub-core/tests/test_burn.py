"""M2.1 burn-in ffmpeg helpers: the dimension probe, the pure ``-vf`` builder, and the
re-encode timeout. Pure command-construction / parsing — no real ffmpeg is invoked.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from autodub_core import ffmpeg_utils as ff


# --- _burn_vf (pure filter builder) ------------------------------------------ #
def test_burn_vf_scales_4k_down_to_cap() -> None:
    # a 4K source is downscaled to fit within the WxH cap BEFORE the libass overlay
    assert ff._burn_vf(3840, 2160, 1920, 1080) == "scale=1920:1080,subtitles=subs.srt"


def test_burn_vf_caps_ultrawide_by_width() -> None:
    # an ultra-wide source whose HEIGHT is within the cap must still be bounded by WIDTH (the
    # height-only-cap gap): 7680x1080 -> factor 0.25 -> 1920x270, not fed through at full ~8 MP.
    assert ff._burn_vf(7680, 1080, 1920, 1080) == "scale=1920:270,subtitles=subs.srt"


def test_burn_vf_no_scale_within_cap() -> None:
    # never upscale: a source within BOTH caps gets only the subtitles filter
    assert ff._burn_vf(1280, 720, 1920, 1080) == "subtitles=subs.srt"
    assert ff._burn_vf(1920, 1080, 1920, 1080) == "subtitles=subs.srt"  # exactly at cap


def test_burn_vf_force_style_is_quoted() -> None:
    vf = ff._burn_vf(1280, 720, 1920, 1080, force_style="FontName=Noto Sans CJK SC")
    assert vf == "subtitles=subs.srt:force_style='FontName=Noto Sans CJK SC'"


# --- probe_dimensions parsing ------------------------------------------------- #
def test_probe_dimensions_parses_first_video_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ff, "_run", lambda *a, **k: '{"streams":[{"width":1920,"height":1080}]}')
    assert ff.probe_dimensions("v.mp4") == (1920, 1080)


def test_probe_dimensions_raises_on_no_video_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ff, "_run", lambda *a, **k: '{"streams":[]}')
    with pytest.raises(ff.FfmpegError):
        ff.probe_dimensions("v.mp4")


def test_probe_dimensions_raises_on_nonpositive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ff, "_run", lambda *a, **k: '{"streams":[{"width":0,"height":0}]}')
    with pytest.raises(ff.FfmpegError):
        ff.probe_dimensions("v.mp4")


# --- re-encode timeout -------------------------------------------------------- #
def test_run_timeout_maps_to_ffmpeg_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a: object, **k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

    monkeypatch.setattr(ff.subprocess, "run", _raise)
    with pytest.raises(ff.FfmpegError, match="timed out"):
        ff._run(["ffmpeg", "-version"], timeout=1)


def test_burn_subtitles_passes_timeout_and_writes_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # burn_subtitles must run ffmpeg with the encode timeout + cwd (staged-srt dir) and
    # atomically write `out`; the filtergraph carries the x264 re-encode + libass overlay.
    video = tmp_path / "in.mp4"
    video.write_bytes(b"vid")
    srt = tmp_path / "in.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    out = tmp_path / "out.mp4"
    monkeypatch.setattr(ff, "assert_ffmpeg", lambda: None)
    monkeypatch.setattr(ff, "assert_allowed_input_format", lambda v: None)  # noqa: ARG005
    monkeypatch.setattr(ff, "probe_dimensions", lambda v: (1280, 720))  # noqa: ARG005
    captured: dict[str, object] = {}

    def _fake_run(cmd: list[str], *, timeout: float | None = None,
                  cwd: str | None = None) -> str:
        captured["timeout"] = timeout
        captured["cwd"] = cwd
        captured["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"burned")  # emulate ffmpeg writing the atomic temp output
        return ""

    monkeypatch.setattr(ff, "_run", _fake_run)
    ff.burn_subtitles(
        video, srt, out, max_width=1920, max_height=1080, timeout_sec=123, crf=20, preset="fast"
    )
    assert out.exists() and out.read_bytes() == b"burned"
    assert captured["timeout"] == 123
    assert captured["cwd"] is not None  # ran from the staged-srt temp dir
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "libx264" in cmd and "subtitles=subs.srt" in cmd and "20" in cmd
