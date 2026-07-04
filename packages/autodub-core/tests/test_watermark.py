"""PR-2 visible AIGC watermark: the ffmpeg drawtext helpers, the re-encode pass, and the burn-path
integration. Pure command/filter construction — no real ffmpeg is invoked.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from autodub_core import ffmpeg_utils as ff


def _wm(**over: object) -> ff.Watermark:
    base: dict[str, object] = dict(
        text="本视频由 AI 合成", position="bottom_right", size_pct=5,
        opacity_pct=85, color="#FFFFFF",
    )
    base.update(over)
    return ff.Watermark(**base)  # type: ignore[arg-type]


# --- _hex_to_ffcolor (#RRGGBB + percent -> 0xRRGGBBAA) ----------------------- #
def test_hex_to_ffcolor_appends_alpha_byte() -> None:
    assert ff._hex_to_ffcolor("#FFFFFF", 85) == "0xFFFFFFD9"  # 85% -> 217 -> 0xD9
    assert ff._hex_to_ffcolor("#00ff00", 100) == "0x00FF00FF"  # normalized upper, full alpha
    assert ff._hex_to_ffcolor("#123456", 0) == "0x12345600"    # 0% -> fully transparent
    assert ff._hex_to_ffcolor("000000", 50) == "0x00000080"    # tolerates a missing '#'


# --- _wm_fontsize_px (percent of frame height, floored) ---------------------- #
def test_wm_fontsize_px_scales_with_height_and_floors() -> None:
    assert ff._wm_fontsize_px(1080, 5) == 54
    assert ff._wm_fontsize_px(720, 5) == 36
    assert ff._wm_fontsize_px(100, 1) == 10  # floor so a tiny frame stays legible


# --- _drawtext_vf (pure filter builder) -------------------------------------- #
def test_drawtext_vf_uses_textfile_and_literal_expansion() -> None:
    vf = ff._drawtext_vf(_wm(), 720)
    # textfile + expansion=none => arbitrary operator text (CJK / % / quotes) drawn literally.
    assert vf.startswith("drawtext=")
    assert "textfile=watermark.txt" in vf
    assert "expansion=none" in vf
    assert "fontsize=36" in vf  # 5% of 720
    assert "fontcolor=0xFFFFFFD9" in vf
    assert "bordercolor=0x000000D9" in vf  # opacity-matched black outline for legibility
    assert "x=w-tw-18:y=h-th-18" in vf     # bottom-right anchor with a proportional margin


def test_drawtext_vf_positions() -> None:
    assert "x=18:y=18" in ff._drawtext_vf(_wm(position="top_left"), 720)
    assert "x=w-tw-18:y=18" in ff._drawtext_vf(_wm(position="top_right"), 720)
    assert "x=18:y=h-th-18" in ff._drawtext_vf(_wm(position="bottom_left"), 720)
    assert "x=(w-tw)/2:y=(h-th)/2" in ff._drawtext_vf(_wm(position="center"), 720)


def test_drawtext_vf_includes_fontfile_when_given() -> None:
    vf = ff._drawtext_vf(_wm(fontfile="/usr/share/fonts/noto/NotoSansCJK.ttc"), 720)
    assert "fontfile=/usr/share/fonts/noto/NotoSansCJK.ttc" in vf
    # No fontfile => the option is absent (ffmpeg default face).
    assert "fontfile=" not in ff._drawtext_vf(_wm(), 720)


# --- _burn_vf appends the watermark on the SAME re-encode -------------------- #
def test_burn_vf_appends_watermark_after_subtitles() -> None:
    vf = ff._burn_vf(1280, 720, 1920, 1080, watermark=_wm())
    assert vf.startswith("subtitles=subs.srt,drawtext=")
    assert "fontsize=36" in vf  # sized from the post-scale height (720, no downscale here)


def test_burn_vf_watermark_sized_from_post_scale_height() -> None:
    # 4K downscaled to 1080 => the overlay is sized from 1080 (post-scale), not the 2160 source.
    vf = ff._burn_vf(3840, 2160, 1920, 1080, watermark=_wm())
    assert vf.startswith("scale=1920:1080,subtitles=subs.srt,drawtext=")
    assert "fontsize=54" in vf  # 5% of 1080


def test_burn_vf_no_watermark_is_unchanged() -> None:
    # Default (no watermark) => byte-identical to the pre-PR-2 burn filter.
    assert ff._burn_vf(1280, 720, 1920, 1080) == "subtitles=subs.srt"


# --- watermark_video (the second-pass re-encode command) --------------------- #
def test_watermark_video_reencodes_video_copies_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "dub.mp4"
    video.write_bytes(b"vid")
    out = tmp_path / "dub.mp4"  # in place
    monkeypatch.setattr(ff, "assert_ffmpeg", lambda: None)
    monkeypatch.setattr(ff, "assert_allowed_input_format", lambda v: None)  # noqa: ARG005
    monkeypatch.setattr(ff, "probe_dimensions", lambda v: (1920, 1080))  # noqa: ARG005
    captured: dict[str, object] = {}

    def _fake_run(cmd: list[str], *, timeout: float | None = None, cwd: str | None = None) -> str:
        captured["timeout"] = timeout
        captured["cwd"] = cwd
        captured["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"marked")
        return ""

    monkeypatch.setattr(ff, "_run", _fake_run)
    ff.watermark_video(video, out, _wm(), timeout_sec=99, crf=21, preset="fast")

    assert out.exists() and out.read_bytes() == b"marked"
    assert captured["timeout"] == 99
    assert captured["cwd"] is not None  # ran from the staged-text temp dir
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "libx264" in cmd                        # video re-encoded to paint the overlay
    assert cmd[cmd.index("-c:a") + 1] == "copy"     # audio stream-copied (already the dub AAC)
    assert "-map_metadata" in cmd                  # carry the AIGC container comment tag
    assert "yuv420p" in cmd                        # browser-compatible pixel format
    assert "-vf" in cmd and "drawtext=" in cmd[cmd.index("-vf") + 1]


def test_watermark_video_stages_text_for_textfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The operator text must be staged as watermark.txt in the ffmpeg cwd (textfile=) so arbitrary
    # content needs no filtergraph escaping. Assert the staged file has the text AT run time.
    video = tmp_path / "dub.mp4"
    video.write_bytes(b"vid")
    monkeypatch.setattr(ff, "assert_ffmpeg", lambda: None)
    monkeypatch.setattr(ff, "assert_allowed_input_format", lambda v: None)  # noqa: ARG005
    monkeypatch.setattr(ff, "probe_dimensions", lambda v: (1280, 720))  # noqa: ARG005
    seen: dict[str, str] = {}

    def _fake_run(cmd: list[str], *, timeout: float | None = None, cwd: str | None = None) -> str:
        seen["text"] = (Path(str(cwd)) / "watermark.txt").read_text(encoding="utf-8")
        Path(cmd[-1]).write_bytes(b"m")
        return ""

    monkeypatch.setattr(ff, "_run", _fake_run)
    ff.watermark_video(video, video, _wm(text="AI 合成 · 100%"), timeout_sec=10)
    assert seen["text"] == "AI 合成 · 100%"  # literal, incl. the % that %{} expansion would eat


def test_burn_subtitles_stages_watermark_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "in.mp4"
    video.write_bytes(b"vid")
    srt = tmp_path / "in.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    out = tmp_path / "out.mp4"
    monkeypatch.setattr(ff, "assert_ffmpeg", lambda: None)
    monkeypatch.setattr(ff, "assert_allowed_input_format", lambda v: None)  # noqa: ARG005
    monkeypatch.setattr(ff, "probe_dimensions", lambda v: (1280, 720))  # noqa: ARG005
    seen: dict[str, object] = {}

    def _fake_run(cmd: list[str], *, timeout: float | None = None, cwd: str | None = None) -> str:
        seen["wm_text"] = (Path(str(cwd)) / "watermark.txt").read_text(encoding="utf-8")
        seen["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"burned")
        return ""

    monkeypatch.setattr(ff, "_run", _fake_run)
    ff.burn_subtitles(
        video, srt, out, max_width=1920, max_height=1080, timeout_sec=123,
        watermark=_wm(text="AI 字幕+水印"),
    )
    assert seen["wm_text"] == "AI 字幕+水印"
    cmd = seen["cmd"]
    assert isinstance(cmd, list)
    # The overlay rides the burn's single re-encode: one -vf carrying both subtitles + drawtext.
    vf = cmd[cmd.index("-vf") + 1]
    assert "subtitles=subs.srt" in vf and "drawtext=" in vf
