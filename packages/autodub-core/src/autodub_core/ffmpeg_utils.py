"""Thin ffmpeg/ffprobe helpers + a stdlib wave stitcher.

ffmpeg cannot run inside the control-plane (Cloudflare Workers), so all video
demux/remux happens here, in the worker/local process where a shell is
available. We keep ffmpeg use to a few well-understood calls and do the timeline
assembly with the stdlib ``wave`` module (robust, no giant filter_complex, no
arg-length limits).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

from .config import CANON_CHANNELS, CANON_SR
from .jsonio import atomic_output


class FfmpegError(RuntimeError):
    pass


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FfmpegError(
            f"command failed ({proc.returncode}): {' '.join(cmd[:6])} ...\n{proc.stderr[-2000:]}"
        )
    return proc.stdout


def assert_ffmpeg() -> None:
    missing = [b for b in ("ffmpeg", "ffprobe") if not have(b)]
    if missing:
        raise FfmpegError(
            f"missing required binary/binaries: {', '.join(missing)}. "
            "Install ffmpeg (https://ffmpeg.org/download.html) and ensure it is on PATH."
        )


# --------------------------------------------------------------------------- #
# SSRF / local-file-read hardening (T1.3c)
# --------------------------------------------------------------------------- #
# Every ffmpeg/ffprobe input is a local file, so demuxers may follow ONLY the
# file protocol (+ crypto, for AES-encrypted local segments). This blocks a media
# file that is secretly a playlist / concat script from making ffmpeg fetch
# http(s):// URLs or read arbitrary file:// paths through a sub-protocol — the
# classic ffmpeg SSRF / local-file-read vector. The kernel is network-free.
_PROTOCOL_WHITELIST = ("-protocol_whitelist", "file,crypto")
_FFPROBE_BASE = ("ffprobe", "-v", "error")

# Container demuxers accepted as input. A strict allowlist: the source's probed
# format_name (a comma list of demuxer aliases) must contain ONLY these tokens,
# so a disguised playlist (hls/applehttp), concat script (concat/ffconcat), image
# list (image2) or network demuxer (rtsp/rtp/sdp/data) is refused up front.
ALLOWED_INPUT_FORMATS = frozenset({
    # video containers
    "mov", "mp4", "m4a", "m4v", "m4b", "3gp", "3g2", "mj2",
    "matroska", "webm",
    "avi", "mpegts", "mpeg", "mpegvideo", "flv", "asf", "mxf",
    # audio containers / streams
    "mp3", "mp2", "wav", "w64", "flac", "ogg", "oga", "opus", "aac", "ac3", "aiff",
})


def _input(path: str | Path) -> list[str]:
    """A protocol-restricted input: ``-protocol_whitelist file,crypto -i PATH``."""
    return [*_PROTOCOL_WHITELIST, "-i", str(path)]


def validate_format_name(format_name: str) -> None:
    """Raise unless every demuxer token in ``format_name`` is an allowed media format.

    ``format_name`` is ffprobe's comma-joined demuxer alias list (e.g.
    ``"mov,mp4,m4a,3gp,3g2,mj2"``). A single disallowed token (``hls``, ``concat``,
    ``image2``, ``rtsp`` …) means the input is a playlist/network/script demuxer
    masquerading as media — refuse it (SSRF guard, T1.3c).
    """
    tokens = [t.strip().lower() for t in format_name.split(",") if t.strip()]
    if not tokens:
        raise FfmpegError("ffprobe reported no container format for the input")
    disallowed = [t for t in tokens if t not in ALLOWED_INPUT_FORMATS]
    if disallowed:
        raise FfmpegError(
            f"input container format {format_name!r} is not an allowed media format "
            f"(disallowed demuxer(s): {', '.join(disallowed)}); refusing a possible "
            f"playlist/concat/network demuxer (SSRF guard, T1.3c)."
        )


def probe_format_name(path: str | Path) -> str:
    """ffprobe the input's container ``format_name`` (protocol-restricted)."""
    out = _run([
        *_FFPROBE_BASE, "-show_entries", "format=format_name",
        "-of", "default=nokey=1:noprint_wrappers=1", *_PROTOCOL_WHITELIST, str(path),
    ])
    return out.strip()


def assert_allowed_input_format(path: str | Path) -> None:
    """Probe ``path`` and refuse it unless its container is an allowed media format."""
    validate_format_name(probe_format_name(path))


def probe_duration_ms(path: str | Path) -> int:
    out = _run([
        *_FFPROBE_BASE, "-show_entries", "format=duration",
        "-of", "json", *_PROTOCOL_WHITELIST, str(path),
    ])
    dur = float(json.loads(out)["format"]["duration"])
    return int(round(dur * 1000))


def extract_audio(src: str | Path, out_wav: str | Path, sr: int = 16000, mono: bool = True) -> None:
    """Extract a PCM wav suitable for ASR (16 kHz mono by default).

    Atomic (temp + replace) so a killed ffmpeg never leaves a partial wav at
    ``audio/original.wav`` / ``audio/speech.wav`` that resume logic (an .exists()
    check) would treat as a valid cached audio artifact.
    """
    with atomic_output(out_wav) as tmp:
        cmd = ["ffmpeg", "-y", *_input(src), "-vn", "-ar", str(sr)]
        if mono:
            cmd += ["-ac", "1"]
        cmd += ["-c:a", "pcm_s16le", str(tmp)]
        _run(cmd)


def to_canonical_wav(
    src: str | Path, out_wav: str | Path, atempo_chain: list[float] | None = None
) -> None:
    """Transcode any audio into the canonical composed-track format, optionally
    applying an atempo chain to time-stretch it.

    Writes a temp file and atomically replaces ``out_wav`` only on success, so a
    killed/failed ffmpeg never leaves a partial file at the cache path — align()
    would otherwise treat a truncated ``_aligned.wav`` as done on resume.
    """
    with atomic_output(out_wav) as tmp:
        cmd = ["ffmpeg", "-y", *_input(src)]
        if atempo_chain:
            flt = ",".join(f"atempo={t:.6f}" for t in atempo_chain)
            cmd += ["-filter:a", flt]
        cmd += ["-ar", str(CANON_SR), "-ac", str(CANON_CHANNELS), "-c:a", "pcm_s16le", str(tmp)]
        _run(cmd)


def atempo_chain_for(ratio: float, max_total: float) -> list[float]:
    """Decompose a speed-up ``ratio`` (>1 = faster) into atempo steps each in
    [0.5, 2.0], capped so the product never exceeds ``max_total``."""
    ratio = min(max(ratio, 1.0), max_total)
    chain: list[float] = []
    remaining = ratio
    while remaining > 2.0 + 1e-6:
        chain.append(2.0)
        remaining /= 2.0
    if remaining > 1.0 + 1e-6:
        chain.append(remaining)
    return chain or [1.0]


def wav_params(path: str | Path) -> tuple[int, int, int]:
    with wave.open(str(path), "rb") as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate()


def wav_duration_ms(path: str | Path) -> int:
    with wave.open(str(path), "rb") as w:
        frames, rate = w.getnframes(), w.getframerate()
    return int(round(frames * 1000 / rate)) if rate else 0


def _silence_frames(ms: int, sampwidth: int, channels: int, rate: int) -> bytes:
    n = max(0, int(round(ms * rate / 1000)))
    return b"\x00" * (n * sampwidth * channels)


def stitch_timeline(
    placements: list[tuple[int, Path]],
    out_wav: str | Path,
    total_ms: int,
) -> None:
    """Build one composed PCM track by placing each (start_ms, aligned_wav) on a
    silent timeline. All inputs must already be canonical (same sr/channels).

    Segments are anchored at their start_ms when the write head allows; if a
    previous segment overran, the next is appended back-to-back (drift is
    accepted rather than overlapped). Pure stdlib ``wave``.
    """
    placements = sorted(placements, key=lambda x: x[0])
    sampwidth = 2  # s16
    channels = CANON_CHANNELS
    rate = CANON_SR

    with atomic_output(out_wav) as tmp, wave.open(str(tmp), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(sampwidth)
        out.setframerate(rate)
        head_ms = 0
        for start_ms, seg_path in placements:
            if start_ms > head_ms:
                out.writeframes(_silence_frames(start_ms - head_ms, sampwidth, channels, rate))
                head_ms = start_ms
            with wave.open(str(seg_path), "rb") as w:
                data = w.readframes(w.getnframes())
                out.writeframes(data)
            head_ms += wav_duration_ms(seg_path)
        if total_ms > head_ms:
            out.writeframes(_silence_frames(total_ms - head_ms, sampwidth, channels, rate))


def mux(
    video: str | Path, audio: str | Path, out: str | Path, ambient: str | Path | None = None
) -> None:
    """Mux a composed dubbed audio track onto the original video (copy video
    stream). Optionally mixes a low background ambient track underneath.

    Atomic (temp + replace): a failed ffmpeg must not leave a partial mp4 that,
    alongside an already-written subtitles.srt, the mux cache would treat as done.
    """
    with atomic_output(out) as tmp:
        if ambient and Path(ambient).exists():
            cmd = [
                "ffmpeg", "-y", *_input(video), *_input(audio), *_input(ambient),
                "-filter_complex",
                "[2:a]volume=0.35[amb];[1:a][amb]amix=inputs=2:normalize=0[aout]",
                "-map", "0:v:0", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(tmp),
            ]
        else:
            cmd = [
                "ffmpeg", "-y", *_input(video), *_input(audio),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(tmp),
            ]
        _run(cmd)
