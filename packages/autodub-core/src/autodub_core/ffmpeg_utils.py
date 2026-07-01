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
import tempfile
import wave
from pathlib import Path

from .config import CANON_CHANNELS, CANON_SR
from .jsonio import atomic_output


class FfmpegError(RuntimeError):
    pass


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _run(cmd: list[str], *, timeout: float | None = None, cwd: str | Path | None = None) -> str:
    # ffmpeg/ffprobe emit UTF-8; decode as UTF-8 (not the Windows locale/cp936) and
    # never crash the output-reader thread on odd bytes. AIGC metadata carries
    # non-ASCII (e.g. Chinese), which a locale decode would choke on.
    # ``timeout`` bounds a long re-encode (M2.1 burn): subprocess.run kills the child on
    # expiry, and we surface it as FfmpegError so a wedged encode never holds the lease.
    # ``cwd`` lets the burn step run from the subtitle file's directory so the libass
    # ``subtitles=`` filter can reference it by a plain name (no Windows drive-colon escaping).
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=None if cwd is None else str(cwd),
        )
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(
            f"command timed out after {timeout}s: {' '.join(cmd[:6])} ..."
        ) from exc
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

# Playlist / concat / script container extensions whose demuxers (HLS, concat, …)
# follow sub-resources. Rejected by extension BEFORE ffprobe ever opens the input,
# so an honestly-named playlist can't even reach the probe.
_PLAYLIST_EXTENSIONS = frozenset({
    ".m3u", ".m3u8", ".pls", ".xspf", ".asx", ".smil", ".wpl", ".cue",
    ".concat", ".ffconcat",
})

# Demuxer whitelist for the untrusted-source format probe: ffprobe refuses to even
# OPEN an input whose demuxer is not allowed (e.g. a disguised concat/hls under a
# media extension), so a playlist/concat demuxer can never dereference sub-resources
# during the probe — constraining the probe itself, not only the post-probe validate.
_FORMAT_WHITELIST = ("-format_whitelist", ",".join(sorted(ALLOWED_INPUT_FORMATS)))


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
    """ffprobe the input's container ``format_name`` (protocol- AND demuxer-restricted)."""
    out = _run([
        *_FFPROBE_BASE, *_FORMAT_WHITELIST, "-show_entries", "format=format_name",
        "-of", "default=nokey=1:noprint_wrappers=1", *_PROTOCOL_WHITELIST, str(path),
    ])
    return out.strip()


def assert_allowed_input_format(path: str | Path) -> None:
    """Refuse ``path`` unless its container is an allowed media format (SSRF guard).

    Layered defense so a playlist/concat demuxer never reads sub-resources:
    1. reject known playlist/script *extensions* before ffprobe opens the input;
    2. probe with ``-protocol_whitelist file,crypto`` (no network) — and ffmpeg's
       own ``allowed_segment_extensions`` blocks ``file://`` segment reads — so even
       a disguised playlist (media extension, hls content) cannot reach a resource
       during the probe; then
    3. reject any non-allowlisted detected demuxer (hls/concat/rtsp/…).
    """
    ext = Path(path).suffix.lower()
    if ext in _PLAYLIST_EXTENSIONS:
        raise FfmpegError(
            f"input extension {ext!r} is a playlist/concat/script container, not media; "
            f"refusing before probe (SSRF guard, T1.3c)."
        )
    validate_format_name(probe_format_name(path))


def probe_duration_ms(path: str | Path) -> int:
    # Demuxer-restricted like probe_format_name: safe-by-default so a direct caller (e.g. the
    # worker's ffprobe re-admission, T2.4) cannot probe a disguised playlist/concat/network
    # demuxer's duration even without a prior assert_allowed_input_format (ffprobe won't open it).
    out = _run([
        *_FFPROBE_BASE, *_FORMAT_WHITELIST, "-show_entries", "format=duration",
        "-of", "json", *_PROTOCOL_WHITELIST, str(path),
    ])
    # Malformed ffprobe output (empty/garbled JSON, a missing format/duration key,
    # or a non-numeric duration like "N/A"/null) must surface as FfmpegError, not a
    # bare KeyError/JSONDecodeError, so callers see one error type. JSONDecodeError
    # is a ValueError subclass; TypeError covers a non-dict payload (e.g. "[]") and a
    # null duration. Carry the raw stdout snippet for diagnosis.
    try:
        dur = float(json.loads(out)["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FfmpegError(
            f"could not read duration from ffprobe output ({exc}); stdout was: {out[:500]!r}"
        ) from exc
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
                params = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                if params != (channels, sampwidth, rate):
                    # A segment that bypassed to_canonical_wav (e.g. 16kHz or stereo)
                    # would be spliced in raw and silently corrupt this span's
                    # pitch/tempo with no error. Refuse it; atomic_output discards the
                    # partial composed track. The pipeline always normalizes first, so
                    # this is a defensive assertion, not an expected path.
                    raise FfmpegError(
                        f"stitch input {seg_path} is not canonical: (channels, sampwidth, "
                        f"rate)={params}, expected {(channels, sampwidth, rate)}; all segments "
                        "must be normalized through to_canonical_wav before stitching."
                    )
                data = w.readframes(w.getnframes())
                out.writeframes(data)
            head_ms += wav_duration_ms(seg_path)
        if total_ms > head_ms:
            out.writeframes(_silence_frames(total_ms - head_ms, sampwidth, channels, rate))


def mux(
    video: str | Path, audio: str | Path, out: str | Path,
    ambient: str | Path | None = None, metadata: list[str] | None = None,
) -> None:
    """Mux a composed dubbed audio track onto the original video (copy video
    stream). Optionally mixes a low background ambient track underneath.

    ``metadata`` is a list of extra ffmpeg output options (e.g. the AIGC
    ``-metadata`` tags from T1.3b); they go on the output container and are
    stream-copy compatible (no re-encode).

    Atomic (temp + replace): a failed ffmpeg must not leave a partial mp4 that,
    alongside an already-written subtitles.srt, the mux cache would treat as done.
    """
    extra = list(metadata or [])
    with atomic_output(out) as tmp:
        if ambient and Path(ambient).exists():
            cmd = [
                "ffmpeg", "-y", *_input(video), *_input(audio), *_input(ambient),
                "-filter_complex",
                "[2:a]volume=0.35[amb];[1:a][amb]amix=inputs=2:normalize=0[aout]",
                "-map", "0:v:0", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", *extra, "-shortest", str(tmp),
            ]
        else:
            cmd = [
                "ffmpeg", "-y", *_input(video), *_input(audio),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", *extra, "-shortest", str(tmp),
            ]
        _run(cmd)


# --------------------------------------------------------------------------- #
# Burned-in subtitles (M2.1): a libass re-encode that paints the subtitle into the picture.
# --------------------------------------------------------------------------- #
def probe_dimensions(path: str | Path) -> tuple[int, int]:
    """ffprobe the first video stream's ``(width, height)``.

    Demuxer- AND protocol-restricted like the other probes, so a disguised
    playlist/concat/network demuxer can never be opened here (SSRF guard, T1.3c).
    """
    out = _run([
        *_FFPROBE_BASE, *_FORMAT_WHITELIST, "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json",
        *_PROTOCOL_WHITELIST, str(path),
    ])
    try:
        streams = json.loads(out).get("streams") or []
        width = int(streams[0]["width"])
        height = int(streams[0]["height"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise FfmpegError(
            f"could not read video dimensions from ffprobe ({exc}); stdout was: {out[:300]!r}"
        ) from exc
    if width <= 0 or height <= 0:
        raise FfmpegError(f"ffprobe reported non-positive video dimensions {width}x{height}")
    return width, height


# A plain, fixed subtitle filename used inside the libass ``subtitles=`` filter:
# burn_subtitles() stages the srt under this name in a temp dir and runs ffmpeg from
# there, so the filter never carries a Windows drive colon / path separator that the
# filtergraph parser would otherwise mangle.
_BURN_SUBS_NAME = "subs.srt"


def _burn_vf(width: int, height: int, max_width: int, max_height: int, *,
             subs_name: str = _BURN_SUBS_NAME, force_style: str | None = None) -> str:
    """Build the ``-vf`` value for a subtitle burn-in (pure; no I/O).

    Downscales the picture to fit within ``max_width`` x ``max_height`` BEFORE the libass
    overlay so a large-area source — including an ultra-wide / anamorphic frame whose height
    alone is within the cap — can't blow up the re-encode. The aspect ratio is preserved (a
    single scale factor), dimensions are forced even (x264), and a source already within BOTH
    caps is NOT scaled (never upscale). ``force_style`` (when given) selects the libass style —
    e.g. a CJK font name so the burn renders non-Latin scripts.
    """
    parts: list[str] = []
    # Downscale-only: the factor is capped at 1.0, so a source within both caps is never enlarged.
    factor = min(max_width / width, max_height / height, 1.0)
    # Even target dims: x264 + yuv420p (4:2:0) reject odd width/height. Round down to even (min 2,
    # so a tiny factor can't round to 0). Emit a scale whenever the target differs from the source —
    # i.e. to downscale AND to fix an in-cap but ODD-dimension source (e.g. 853x481 -> 852x480).
    target_w = max(2, int(width * factor) // 2 * 2)
    target_h = max(2, int(height * factor) // 2 * 2)
    if (target_w, target_h) != (width, height):
        parts.append(f"scale={target_w}:{target_h}")
    subs = f"subtitles={subs_name}"
    if force_style:
        subs += f":force_style='{force_style}'"
    parts.append(subs)
    return ",".join(parts)


def burn_subtitles(
    video: str | Path, srt: str | Path, out: str | Path, *,
    max_width: int, max_height: int, timeout_sec: float,
    crf: int = 23, preset: str = "veryfast",
    force_style: str | None = None, metadata: list[str] | None = None,
) -> None:
    """Re-encode ``video`` with ``srt`` burned into the picture (libass), writing ``out``.

    Unlike mux() (which stream-copies), burning paints the subtitle into pixels, so the
    video stream MUST be re-encoded (x264). The source is validated at this ffmpeg
    boundary (SSRF guard, T1.3c), downscaled to ``max_height`` if taller, and the
    re-encode is bounded by ``timeout_sec`` (a wedged encode is killed and surfaced as
    FfmpegError, never an unbounded lease hold). Audio is stream-copied. ``metadata``
    (the AIGC ``-metadata`` tags) rides on the output container. Atomic temp+replace.
    """
    assert_ffmpeg()
    # mux/burn may run on a pre-staged/resumed job that bypassed ingest's allowlist;
    # validate the source container HERE too so a crafted input can't reach ffmpeg.
    assert_allowed_input_format(video)
    width, height = probe_dimensions(video)
    vf = _burn_vf(width, height, max_width, max_height, force_style=force_style)
    extra = list(metadata or [])
    src = Path(video).resolve()  # absolute: ffmpeg runs with cwd = the srt's temp dir
    # Stage the srt under a plain name in a temp dir and run ffmpeg from there, so the
    # libass filter references "subs.srt" (no path escaping / Windows drive-colon issues).
    with tempfile.TemporaryDirectory(prefix="ovt_burn_") as td:
        shutil.copy2(srt, Path(td) / _BURN_SUBS_NAME)
        with atomic_output(out) as tmp:
            cmd = [
                "ffmpeg", "-y", *_input(src),
                "-vf", vf,
                "-map", "0:v:0", "-map", "0:a:0?",
                # Force a browser-compatible MP4: yuv420p (4:2:0) video + AAC audio, so a valid but
                # exotic source (RGB / yuv444p MKV/WebM, non-AAC audio) still yields a playable
                # video/mp4 instead of inheriting an unplayable pixel format / copying a non-AAC
                # track. Mirrors the dub mux's AAC normalization (not -c:a copy).
                "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", *extra, str(Path(tmp).resolve()),
            ]
            _run(cmd, timeout=timeout_sec, cwd=td)
