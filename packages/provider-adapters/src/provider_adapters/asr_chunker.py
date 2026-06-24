"""T1.3e — cloud ASR compress-first / chunk / offset-merge, with provider audio
format negotiation.

Cloud ASR endpoints cap request size and/or audio duration. This module:

  1. **compress-first** — re-encode the kernel's prepared 16 kHz mono wav to the most
     compact codec the *target provider accepts* (Opus → FLAC → MP3 → wav). The point
     of `negotiate_codec` is that we do NOT assume every API ingests Opus (OpenAI
     whisper-1 does not, e.g.): each provider declares its ``accepted_formats`` and we
     pick the smallest mutually-acceptable one (backlog T1.3e "不收 Opus 自动换 FLAC/MP3").
  2. **chunk only when still over-limit** — if the compressed whole file is within the
     provider's byte/duration caps it ships in a single request; otherwise it is split
     by time into the fewest chunks that each fit, and
  3. **offset-merge** — each chunk is transcribed independently (chunk-relative word
     timings) and the words are shifted by the chunk's start offset and concatenated,
     so the merged transcript carries a single global timeline.

This package has **no import edge to autodub-core** (T1.1 ∥ T1.2, backlog DAG): the
compress/cut steps shell out to ffmpeg directly here rather than reusing the kernel's
ffmpeg helpers. ffmpeg is invoked with ``-protocol_whitelist file`` (no network
demuxers — defence in depth, mirroring the kernel's SSRF hardening in T1.3c); the
input is the worker's own already-validated local audio.
"""

from __future__ import annotations

import contextlib
import subprocess
import wave
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil
from pathlib import Path

from ovt_schemas.contracts import Word

from .base import ProviderUnavailable, has_binary

# Codec preference, most-compact first. Each maps to (ffmpeg encoder args, file ext).
# Opus at 16 kbps mono is tiny; FLAC is lossless-but-smaller-than-wav; MP3 is the
# widest-accepted lossy fallback; wav is the universal last resort.
_ENCODERS: dict[str, tuple[tuple[str, ...], str]] = {
    "opus": (("-c:a", "libopus", "-b:a", "16k"), "ogg"),
    "flac": (("-c:a", "flac"), "flac"),
    "mp3": (("-c:a", "libmp3lame", "-b:a", "64k"), "mp3"),
    "wav": (("-c:a", "pcm_s16le"), "wav"),
}
_PREFERENCE: tuple[str, ...] = ("opus", "flac", "mp3", "wav")

# When sizing chunks against a byte cap, leave headroom so codec/container overhead
# (and VBR jitter) doesn't push a chunk back over the limit.
_BYTE_SAFETY = 0.9


@dataclass(frozen=True)
class AudioConstraints:
    """One ASR provider's request limits + the audio codecs its API ingests.

    ``accepted_formats`` are codec keys from ``_ENCODERS`` (e.g. ``("flac", "mp3", "wav")``
    for OpenAI whisper-1, which rejects Opus). ``max_bytes`` / ``max_duration_ms`` are the
    request caps; ``None`` means "no cap on this axis" (e.g. local backends)."""

    accepted_formats: tuple[str, ...]
    max_bytes: int | None = None
    max_duration_ms: int | None = None


@dataclass(frozen=True)
class _Chunk:
    path: str
    offset_ms: int
    duration_ms: int


def negotiate_codec(constraints: AudioConstraints) -> str:
    """The most-compact codec the provider accepts. Opus first; a provider that does not
    list Opus downgrades to FLAC, then MP3, then wav. Raises if there is no overlap with
    the codecs we can actually encode."""
    for codec in _PREFERENCE:
        if codec in constraints.accepted_formats:
            return codec
    raise ProviderUnavailable(
        f"no mutually-acceptable audio codec: provider accepts {constraints.accepted_formats}, "
        f"this chunker encodes {_PREFERENCE}"
    )


def _sec(ms: int) -> str:
    return f"{ms / 1000.0:.3f}"


def _src_duration_ms(path: str) -> int:
    """Duration of the source PCM wav via stdlib ``wave`` (no ffprobe). 0 when the source
    is not a readable wav — the kernel's prepare stage always hands us a wav, so 0 only
    happens on a genuinely broken input (caller then ships a single request)."""
    try:
        with contextlib.closing(wave.open(path, "rb")) as w:
            frames, rate = w.getnframes(), w.getframerate()
        return int(round(frames * 1000 / rate)) if rate else 0
    except Exception:  # noqa: BLE001 - best-effort; caller tolerates 0
        return 0


def _encode(src: str, dst: str, codec: str, start_ms: int, end_ms: int) -> None:
    """Re-encode ``src`` [start_ms, end_ms) to ``dst`` as 16 kHz mono ``codec``.

    ffmpeg only (no network protocols). Monkeypatched out in unit tests so the chunk
    planning / merge logic is exercised without a real ffmpeg or real audio."""
    if not has_binary("ffmpeg"):
        raise ProviderUnavailable(
            "asr_chunker needs ffmpeg to compress/split audio for cloud ASR; install ffmpeg"
        )
    enc_args, _ext = _ENCODERS[codec]
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-protocol_whitelist", "file",
        "-ss", _sec(start_ms), "-i", src, "-t", _sec(max(0, end_ms - start_ms)),
        "-ac", "1", "-ar", "16000", *enc_args, dst,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not Path(dst).exists():
        raise ProviderUnavailable(f"asr_chunker encode failed: {proc.stderr[-400:]}")


def within_limits(path: str, total_ms: int, constraints: AudioConstraints) -> bool:
    """True when ``path`` (``total_ms`` long) is inside every set cap. An unset cap
    (``None``) never blocks — so a constraint with no caps is always 'within'."""
    if constraints.max_bytes is not None and Path(path).stat().st_size > constraints.max_bytes:
        return False
    return not (constraints.max_duration_ms is not None and total_ms > constraints.max_duration_ms)


def compress(src_wav: str, constraints: AudioConstraints, work: str) -> tuple[str, int]:
    """Compress-first: encode the whole wav to the negotiated codec. Returns
    ``(compressed_path, total_ms)``. The caller checks ``within_limits`` to decide
    between a single request and chunking."""
    total_ms = _src_duration_ms(src_wav)
    codec = negotiate_codec(constraints)
    ext = _ENCODERS[codec][1]
    workdir = Path(work)
    workdir.mkdir(parents=True, exist_ok=True)
    dst = workdir / f"compressed.{ext}"
    _encode(src_wav, str(dst), codec, 0, total_ms if total_ms > 0 else 0)
    return str(dst), total_ms


def _chunk_duration_ms(total_ms: int, whole_bytes: int, constraints: AudioConstraints) -> int:
    """Largest per-chunk duration that satisfies every set cap. A byte cap is converted to
    a duration via the measured bytes-per-ms of the compressed whole (with safety
    headroom)."""
    limits: list[int] = []
    if constraints.max_duration_ms is not None:
        limits.append(constraints.max_duration_ms)
    if constraints.max_bytes is not None and total_ms > 0 and whole_bytes > 0:
        bytes_per_ms = whole_bytes / total_ms
        limits.append(int(constraints.max_bytes * _BYTE_SAFETY / bytes_per_ms))
    return max(1, min(limits)) if limits else total_ms


def _split(
    src_wav: str, constraints: AudioConstraints, total_ms: int, whole_bytes: int, work: str
) -> list[_Chunk]:
    """Cut the source wav into time chunks that each fit the caps, re-encoding every chunk
    to the negotiated codec. Returns chunks with their start offsets (for merge)."""
    codec = negotiate_codec(constraints)
    ext = _ENCODERS[codec][1]
    chunk_ms = _chunk_duration_ms(total_ms, whole_bytes, constraints)
    workdir = Path(work)
    workdir.mkdir(parents=True, exist_ok=True)
    n = max(1, ceil(total_ms / chunk_ms))
    chunks: list[_Chunk] = []
    for i in range(n):
        start = i * chunk_ms
        end = min(start + chunk_ms, total_ms)
        dst = workdir / f"chunk_{i:03d}.{ext}"
        _encode(src_wav, str(dst), codec, start, end)
        chunks.append(_Chunk(str(dst), start, end - start))
    return chunks


def merge_words(parts: list[tuple[list[Word], int]]) -> list[Word]:
    """Shift each chunk's chunk-relative word timings by the chunk's start offset and
    concatenate into one global timeline."""
    merged: list[Word] = []
    for words, offset in parts:
        for w in words:
            merged.append(Word(text=w.text, start_ms=w.start_ms + offset, end_ms=w.end_ms + offset))
    return merged


def chunked_words(
    run_one: Callable[[str, int], list[Word]],
    src_wav: str,
    constraints: AudioConstraints,
    total_ms: int,
    whole_bytes: int,
    work: str,
) -> list[Word]:
    """Split an over-limit source into fitting chunks, transcribe each via
    ``run_one(chunk_path, chunk_duration_ms)`` (chunk-relative words), and offset-merge
    into one timeline. The duration is a hint for a backend's degenerate "text but no
    word timings" fallback; backends that always return real timings ignore it.

    A 0-duration source can't be time-split — there is nothing to offset — so it falls
    back to a single ``run_one`` over the source (the provider then surfaces any real
    size error loudly)."""
    if total_ms <= 0:
        return run_one(src_wav, 0)
    chunks = _split(src_wav, constraints, total_ms, whole_bytes, work)
    if len(chunks) == 1:
        return run_one(chunks[0].path, chunks[0].duration_ms)
    return merge_words([(run_one(c.path, c.duration_ms), c.offset_ms) for c in chunks])
