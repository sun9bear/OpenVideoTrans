"""Pipeline stages. Each reads/writes the file-based artifacts in
``config.JobPaths`` and is a no-op on re-run when its output already exists
(unless ``force=True``).

Contracts come from ``ovt_schemas`` (the schemas package is the single source of
truth, STEP0-B); providers are injected via a ``Resolver`` (the paid-API safety
gate lives in ``provider-adapters``, T1.2 — never here).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from ovt_schemas.contracts import DubbingSegment, Transcript, TranslationResult

from . import config
from . import ffmpeg_utils as ff
from .config import JobPaths
from .jsonio import read_json, write_json
from .providers import ProviderUnavailable, Resolver, TtsProvider


def _log(msg: str) -> None:
    print(f"[autodub] {msg}", flush=True)


def _line_duration_ms(line) -> int:  # noqa: ANN001 - ovt_schemas.TranscriptLine (structural)
    """Duration of a transcript line (ovt_schemas lines carry no derived field)."""
    return max(0, line.end_ms - line.start_ms)


# --------------------------------------------------------------------------- #
def ingest(paths: JobPaths, source: str, force: bool = False) -> None:
    """Bring a LOCAL source file into the job directory and extract its audio.

    URL / yt-dlp ingest is enabled only in the local-runner CLI (T1.4, backlog
    "URL/yt-dlp 仅此开") together with the SSRF hardening of T1.3c; the kernel
    itself stays network-free.
    """
    paths.ensure()
    existing = paths.original_video()
    if existing and paths.original_audio.exists() and not force:
        _log(f"ingest: cached ({existing.name})")
        return

    if source.startswith(("http://", "https://")):
        # Input-mode error (a URL is not a valid kernel source), not a provider
        # problem: the kernel is local-file only and network-free. URL / yt-dlp
        # ingest is the local-runner CLI's job (T1.4) with the SSRF hardening of
        # T1.3c. T1.4 owns its own error mapping for this.
        raise ValueError(
            "autodub-core ingest accepts a local file path only; URL ingest is "
            "handled by the local-runner CLI (T1.4)."
        )
    src = Path(source)
    if not src.exists():
        raise FileNotFoundError(f"source not found: {source}")
    dst = paths.video / f"original{src.suffix.lower() or '.mp4'}"
    shutil.copy2(src, dst)
    _log(f"ingest: copied local {src.name}")

    video = paths.original_video()
    if not video:
        raise RuntimeError("ingest produced no video/original.* file")
    ff.assert_ffmpeg()
    ff.extract_audio(video, paths.original_audio, sr=16000, mono=True)
    _log(f"ingest: extracted audio -> {paths.original_audio.name}")


def prepare(paths: JobPaths, separate: bool = False, force: bool = False) -> None:
    """Produce speech.wav for ASR. Optionally split out an ambient (background)
    stem with demucs when available — best-effort, free, local. On any failure
    it falls back to speech = original audio (a free fallback, never a paid one)."""
    if paths.speech.exists() and not force:
        _log("prepare: cached")
        return
    if separate and ff.have("demucs"):
        try:
            _demucs_split(paths)
            _log("prepare: demucs separation done (speech + ambient)")
            return
        except Exception as exc:  # noqa: BLE001 - free fallback, no paid path involved
            _log(f"prepare: demucs failed ({exc}); falling back to full-mix speech")
    shutil.copy2(paths.original_audio, paths.speech)
    _log("prepare: speech = original audio (no separation)")


def _demucs_split(paths: JobPaths) -> None:
    out_dir = Path(tempfile.mkdtemp(prefix="ovt_demucs_"))
    subprocess.run(
        ["demucs", "--two-stems=vocals", "-o", str(out_dir), str(paths.original_audio)],
        capture_output=True, text=True, check=True,
    )
    vocals = next(out_dir.rglob("vocals.wav"))
    no_vocals = next(out_dir.rglob("no_vocals.wav"))
    ff.extract_audio(vocals, paths.speech, sr=16000, mono=True)
    ff.to_canonical_wav(no_vocals, paths.ambient)


# --------------------------------------------------------------------------- #
def transcribe(paths: JobPaths, resolver: Resolver, provider: str | None,
               source_lang: str | None, allow_paid: bool, force: bool = False) -> Transcript:
    if paths.transcript.exists() and not force:
        _log("transcribe: cached")
        return Transcript.model_validate(read_json(paths.transcript))
    asr = resolver.select("asr", provider, allow_paid)
    _log(f"transcribe: provider={asr.info.name}")
    result = asr.transcribe(str(paths.speech), source_lang)
    write_json(paths.transcript, result.model_dump())
    _log(f"transcribe: {len(result.lines)} lines, lang={result.source_language}")
    return result


# --------------------------------------------------------------------------- #
def translate(paths: JobPaths, resolver: Resolver, provider: str | None, target_lang: str,
              source_lang: str | None, allow_paid: bool, force: bool = False) -> TranslationResult:
    if paths.segments.exists() and not force:
        _log("translate: cached")
        return TranslationResult.model_validate(read_json(paths.segments))
    transcript = Transcript.model_validate(read_json(paths.transcript))
    src = source_lang or transcript.source_language or "auto"
    mt = resolver.select("mt", provider, allow_paid)
    _log(f"translate: provider={mt.info.name} {src}->{target_lang}")

    texts = [ln.source_text for ln in transcript.lines]
    budgets = [_line_duration_ms(ln) for ln in transcript.lines]
    translations = mt.translate(texts, src, target_lang, budgets) if texts else []

    segments: list[DubbingSegment] = []
    for i, ln in enumerate(transcript.lines):
        tgt = translations[i].strip() if i < len(translations) else ""
        segments.append(DubbingSegment(
            segment_id=f"seg_{i:04d}", index=i, speaker_id=ln.speaker_id,
            start_ms=ln.start_ms, end_ms=ln.end_ms, target_duration_ms=_line_duration_ms(ln),
            source_text=ln.source_text, target_text=tgt,
            keep_original=(not tgt),
        ))
    result = TranslationResult(source_language=src, target_language=target_lang,
                               mt_provider=mt.info.name, segments=segments)
    write_json(paths.segments, result.model_dump())
    _log(f"translate: {len(segments)} segments")
    return result


# --------------------------------------------------------------------------- #
def _assign_voices(provider: TtsProvider, lang: str, speaker_ids: list[str]) -> dict[str, str]:
    voices = provider.voices_for(lang)
    if not voices:
        raise ProviderUnavailable(f"{provider.info.name} has no voice for language {lang!r}")
    return {sid: voices[i % len(voices)] for i, sid in enumerate(sorted(set(speaker_ids)))}


def tts(paths: JobPaths, resolver: Resolver, provider: str | None,
        allow_paid: bool, force: bool = False) -> TranslationResult:
    paths.ensure()  # standalone/resume runs must still have tts/ before writing
    result = TranslationResult.model_validate(read_json(paths.segments))
    engine = resolver.select("tts", provider, allow_paid)
    _log(f"tts: provider={engine.info.name}")
    voice_map = _assign_voices(engine, result.target_language,
                               [s.speaker_id for s in result.segments])

    for seg in result.segments:
        if seg.keep_original or not seg.target_text.strip():
            seg.keep_original = True
            continue
        if not force and paths.find_tts_raw(seg.index) is not None:
            continue
        voice = voice_map[seg.speaker_id]
        out_stub = paths.tts_raw(seg.index, getattr(engine, "ext", "mp3"))
        actual = engine.synthesize(seg.target_text, voice, result.target_language, str(out_stub))
        seg.voice_id, seg.tts_provider = voice, engine.info.name
        _log(f"tts: seg {seg.index} -> {Path(actual).name} [{voice}]")

    write_json(paths.segments, result.model_dump())
    return result


# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TimingPlan:
    """Pure alignment decision for one segment (no I/O, no ffmpeg)."""

    method: str                       # "fit" | "dsp" | "force_dsp"
    atempo_chain: list[float] | None  # None when the clip already fits
    align_ratio: float                # actual/target, rounded (uncapped)
    needs_review: bool                # True when ratio exceeds max_speedup


def assign_timing(actual_ms: int, target_duration_ms: int,
                  max_speedup: float = config.MAX_SPEEDUP) -> TimingPlan:
    """Decide how to fit a synthesized clip of ``actual_ms`` into a
    ``target_duration_ms`` slot. ratio > 1.0 means the clip is too long and must
    be sped up; beyond ``max_speedup`` it is capped and flagged for review."""
    target = max(1, target_duration_ms)
    ratio = actual_ms / target
    if ratio <= 1.0:
        return TimingPlan("fit", None, round(ratio, 3), False)
    chain = ff.atempo_chain_for(ratio, max_speedup)
    method = "dsp" if ratio <= max_speedup else "force_dsp"
    return TimingPlan(method, chain, round(ratio, 3), ratio > max_speedup)


def align(paths: JobPaths, force: bool = False) -> TranslationResult:
    paths.ensure()
    result = TranslationResult.model_validate(read_json(paths.segments))
    ff.assert_ffmpeg()
    for seg in result.segments:
        aligned = paths.tts_aligned(seg.index)
        if aligned.exists() and not force:
            continue
        if seg.keep_original:
            _write_silence(aligned, seg.target_duration_ms)
            seg.align_method = "silence"
            continue
        raw = paths.find_tts_raw(seg.index)
        if raw is None:
            _write_silence(aligned, seg.target_duration_ms)
            seg.align_method, seg.needs_review = "missing", True
            continue
        actual_ms = _media_duration_ms(raw)
        plan = assign_timing(actual_ms, seg.target_duration_ms)
        ff.to_canonical_wav(raw, aligned, plan.atempo_chain)
        seg.align_method = plan.method
        seg.align_ratio = plan.align_ratio
        # Write-back is unconditional (the "fit" plan reports needs_review=False),
        # which on a force re-align resets a previously-stale flag rather than
        # leaving it — the more defensible behavior for a recomputed plan.
        seg.needs_review = plan.needs_review
    write_json(paths.segments, result.model_dump())
    n_review = sum(1 for s in result.segments if s.needs_review)
    _log(f"align: done; {n_review} segment(s) flagged needs_review")
    return result


def _media_duration_ms(path: Path) -> int:
    if path.suffix.lower() == ".wav":
        try:
            return ff.wav_duration_ms(path)
        except Exception:  # noqa: BLE001 - fall back to ffprobe for malformed/odd wavs
            pass
    return ff.probe_duration_ms(path)


def _write_silence(out_wav: Path, ms: int) -> None:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    n = int(round(max(0, ms) * config.CANON_SR / 1000))
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(config.CANON_CHANNELS)
        w.setsampwidth(2)
        w.setframerate(config.CANON_SR)
        w.writeframes(b"\x00" * (n * 2 * config.CANON_CHANNELS))


# --------------------------------------------------------------------------- #
def mux(paths: JobPaths, keep_ambient: bool = True, force: bool = False) -> Path:
    paths.ensure()
    if paths.dubbed_video.exists() and not force:
        _log("mux: cached")
        return paths.dubbed_video
    result = TranslationResult.model_validate(read_json(paths.segments))
    video = paths.original_video()
    if not video:
        raise RuntimeError("mux: no original video")
    ff.assert_ffmpeg()
    total_ms = ff.probe_duration_ms(video)

    placements = [(s.start_ms, paths.tts_aligned(s.index))
                  for s in result.segments if paths.tts_aligned(s.index).exists()]
    ff.stitch_timeline(placements, paths.dubbed_audio, total_ms)
    _log(f"mux: composed {len(placements)} segments into dubbed audio")

    ambient = paths.ambient if (keep_ambient and paths.ambient.exists()) else None
    ff.mux(video, paths.dubbed_audio, paths.dubbed_video, ambient=ambient)
    _write_srt(result, paths.subtitles)
    _log(f"mux: wrote {paths.dubbed_video}")
    return paths.dubbed_video


def _write_srt(result: TranslationResult, path: Path) -> None:
    def ts(ms: int) -> str:
        ms = max(0, ms)
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines: list[str] = []
    n = 0
    for seg in result.segments:
        text = seg.target_text.strip() or seg.source_text.strip()
        if not text:
            continue
        n += 1
        lines.append(str(n))
        lines.append(f"{ts(seg.start_ms)} --> {ts(seg.end_ms)}")
        lines.append(text)
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
def run_pipeline(
    paths: JobPaths,
    resolver: Resolver,
    *,
    source: str,
    target_lang: str,
    source_lang: str | None = None,
    asr: str | None = None,
    mt: str | None = None,
    tts_provider: str | None = None,
    allow_paid: bool = False,
    separate: bool = False,
    keep_ambient: bool = True,
    force: bool = False,
) -> Path:
    """End-to-end local dub: ingest -> prepare -> transcribe -> translate -> tts
    -> align -> mux. Returns the dubbed video path (subtitles alongside it).

    ``allow_paid`` defaults to False (red line): a paid provider is never invoked
    unless the caller both names it and opts in, and the resolver enforces that.
    """
    ingest(paths, source, force=force)
    prepare(paths, separate=separate, force=force)
    transcribe(paths, resolver, asr, source_lang, allow_paid, force=force)
    translate(paths, resolver, mt, target_lang, source_lang, allow_paid, force=force)
    tts(paths, resolver, tts_provider, allow_paid, force=force)
    align(paths, force=force)
    return mux(paths, keep_ambient=keep_ambient, force=force)
