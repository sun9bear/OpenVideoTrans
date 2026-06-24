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
from .jsonio import atomic_output, read_json, write_json
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
    src_resolved, dst_resolved = src.resolve(), dst.resolve()
    # Drop any stale original.* (e.g. a prior run's different extension) so
    # original_video() can only resolve to the source we are ingesting now —
    # otherwise a forced re-ingest with a new suffix would keep extracting/muxing
    # the old file (original.avi sorts before original.mp4).
    for old in paths.video.glob("original.*"):
        if old.resolve() not in (src_resolved, dst_resolved):
            old.unlink()
    if src_resolved != dst_resolved:
        with atomic_output(dst) as tmp:
            shutil.copy2(src, tmp)
        _log(f"ingest: copied local {src.name}")
    else:
        # already staged at the canonical location (worker pre-stage / re-extract):
        # don't copy a file onto itself; extract audio from it in place.
        _log(f"ingest: source already staged ({src.name})")

    video = paths.original_video()
    if not video:
        raise RuntimeError("ingest produced no video/original.* file")
    ff.assert_ffmpeg()
    # Drop any prior audio BEFORE extracting: extract_audio only replaces on
    # success, so a failed re-extract would otherwise leave the old audio paired
    # with the freshly-staged video on a later non-force retry.
    paths.original_audio.unlink(missing_ok=True)
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
    # atomic copy: a truncated speech.wav would be cached and fed to ASR.
    with atomic_output(paths.speech) as tmp:
        shutil.copy2(paths.original_audio, tmp)
    # no separation -> no ambient stem; drop any stale ambient.wav from a prior
    # separated run so mux(keep_ambient=True) can't mix old background.
    paths.ambient.unlink(missing_ok=True)
    _log("prepare: speech = original audio (no separation)")


def _demucs_split(paths: JobPaths) -> None:
    # TemporaryDirectory auto-removes the (potentially large) demucs stems; the
    # two stems we keep are copied out to paths.speech/ambient before cleanup.
    with tempfile.TemporaryDirectory(prefix="ovt_demucs_") as tmp:
        out_dir = Path(tmp)
        subprocess.run(
            ["demucs", "--two-stems=vocals", "-o", str(out_dir), str(paths.original_audio)],
            capture_output=True, text=True, check=True,
        )
        vocals = next(out_dir.rglob("vocals.wav"))
        no_vocals = next(out_dir.rglob("no_vocals.wav"))
        # Write ambient FIRST, then speech: speech.wav is prepare()'s cache gate, so
        # writing it last means speech.exists() implies separation fully completed
        # (an interruption can't leave a "cached" speech without its ambient stem).
        ff.to_canonical_wav(no_vocals, paths.ambient)
        ff.extract_audio(vocals, paths.speech, sr=16000, mono=True)


# --------------------------------------------------------------------------- #
def transcribe(paths: JobPaths, resolver: Resolver, provider: str | None,
               source_lang: str | None, force: bool = False) -> Transcript:
    if paths.transcript.exists() and not force:
        _log("transcribe: cached")
        return Transcript.model_validate(read_json(paths.transcript))
    # Tier 1 kernel NEVER enables paid providers (CLAUDE.md red line §1/§14):
    # allow_paid is hard-False here; provider-adapters' select enforces the gate.
    asr = resolver.select("asr", provider, allow_paid=False)
    _log(f"transcribe: provider={asr.info.name}")
    result = asr.transcribe(str(paths.speech), source_lang)
    write_json(paths.transcript, result.model_dump())
    _log(f"transcribe: {len(result.lines)} lines, lang={result.source_language}")
    return result


# --------------------------------------------------------------------------- #
def translate(paths: JobPaths, resolver: Resolver, provider: str | None, target_lang: str,
              source_lang: str | None, force: bool = False) -> TranslationResult:
    if paths.segments.exists() and not force:
        _log("translate: cached")
        return TranslationResult.model_validate(read_json(paths.segments))
    transcript = Transcript.model_validate(read_json(paths.transcript))
    src = source_lang or transcript.source_language or "auto"
    texts = [ln.source_text for ln in transcript.lines]
    if not texts:
        # no speech -> nothing to translate; don't resolve an MT provider (it may
        # be unavailable / quota-exhausted), still produce a valid empty result.
        result = TranslationResult(source_language=src, target_language=target_lang,
                                   mt_provider="", segments=[])
        write_json(paths.segments, result.model_dump())
        _log("translate: no transcript lines; nothing to translate")
        return result

    mt = resolver.select("mt", provider, allow_paid=False)  # red line: never paid (§1/§14)
    _log(f"translate: provider={mt.info.name} {src}->{target_lang}")
    budgets = [_line_duration_ms(ln) for ln in transcript.lines]
    translations = mt.translate(texts, src, target_lang, budgets)
    if len(translations) != len(texts):
        # The provider contract is one output per input line. A short/truncated
        # batch would silently leave segments untranslated (target_text="" ->
        # keep_original); surface the provider failure instead of shipping it.
        raise RuntimeError(
            f"MT provider {mt.info.name} returned {len(translations)} translations "
            f"for {len(texts)} input lines (expected 1:1)."
        )

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
        force: bool = False) -> TranslationResult:
    paths.ensure()  # standalone/resume runs must still have tts/ before writing
    result = TranslationResult.model_validate(read_json(paths.segments))

    # A segment with empty target_text carries no dub -> keep_original.
    for seg in result.segments:
        if not seg.target_text.strip():
            seg.keep_original = True

    pending = [s for s in result.segments if not s.keep_original]
    # Segments needing a synthesis call NOW (cache miss or force). A fully cached
    # resume must not resolve a provider: availability/quota may have changed and
    # no provider call is needed (file-based no-op contract).
    to_synth = [s for s in pending if force or paths.find_tts_raw(s.index) is None]
    if not to_synth:
        # Nothing to synthesize: subtitle-only / all keep_original, or every raw
        # artifact already cached. Don't resolve a TTS provider or require a voice
        # for the target locale — such a job is still valid and must complete.
        write_json(paths.segments, result.model_dump())
        _log("tts: nothing to synthesize (all keep_original or cached)")
        return result

    engine = resolver.select("tts", provider, allow_paid=False)  # red line: never paid (§1/§14)
    _log(f"tts: provider={engine.info.name}")
    voice_map = _assign_voices(engine, result.target_language,
                               [s.speaker_id for s in result.segments])

    for seg in to_synth:
        # Drop any stale raw variant for this index (e.g. a different extension
        # from a previous provider on a force re-run) so align()'s find_tts_raw()
        # cannot later pick up the old file.
        for stale in paths.tts.glob(f"segment_{seg.index:04d}.*"):
            if not stale.name.endswith("_aligned.wav"):
                stale.unlink()
        voice = voice_map[seg.speaker_id]
        # Persist provider metadata BEFORE exposing the raw file: find_tts_raw() is
        # the resume skip-gate, so a crash after the raw appears but before the
        # final write must not leave the segment cached without voice_id/provider.
        seg.voice_id, seg.tts_provider = voice, engine.info.name
        write_json(paths.segments, result.model_dump())
        # Synthesize into a temp dir (outside find_tts_raw()'s glob), then move
        # into place atomically — a crash mid-synthesis must not leave a partial
        # raw that a resume treats as cached and align() then consumes corrupt.
        with tempfile.TemporaryDirectory(dir=paths.tts) as td:
            stub = Path(td) / f"raw.{getattr(engine, 'ext', 'mp3').lstrip('.')}"
            actual = Path(
                engine.synthesize(seg.target_text, voice, result.target_language, str(stub))
            )
            final = paths.tts_raw(seg.index, actual.suffix)
            actual.replace(final)
        _log(f"tts: seg {seg.index} -> {final.name} [{voice}]")

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
    ffmpeg_checked = False  # only a real clip to transcode needs ffmpeg/ffprobe;
    for seg in result.segments:  # a fully-cached align() must be a no-op without it
        aligned = paths.tts_aligned(seg.index)
        if aligned.exists() and not force:
            continue
        # Force rewrite: drop the old aligned wav up front. The atomic writers keep
        # the previous file on failure, so without this a failed re-write would
        # leave a stale aligned wav satisfying the non-force cache check on resume.
        aligned.unlink(missing_ok=True)
        # Persist each segment's metadata to segments.json BEFORE writing its
        # aligned wav (the skip-gate artifact): then aligned.exists() implies the
        # metadata is durable, so a crash mid-align can't lose a needs_review flag
        # while mux still consumes the cached audio.
        if seg.keep_original:
            seg.align_method = "silence"
            write_json(paths.segments, result.model_dump())
            _write_silence(aligned, seg.target_duration_ms)
            continue
        raw = paths.find_tts_raw(seg.index)
        if raw is None:
            seg.align_method, seg.needs_review = "missing", True
            write_json(paths.segments, result.model_dump())
            _write_silence(aligned, seg.target_duration_ms)
            continue
        if not ffmpeg_checked:
            ff.assert_ffmpeg()
            ffmpeg_checked = True
        actual_ms = _media_duration_ms(raw)
        plan = assign_timing(actual_ms, seg.target_duration_ms)
        # unconditional write-back: the "fit" plan reports needs_review=False, so a
        # force re-align resets a previously-stale flag rather than leaving it.
        seg.align_method = plan.method
        seg.align_ratio = plan.align_ratio
        seg.needs_review = plan.needs_review
        write_json(paths.segments, result.model_dump())
        ff.to_canonical_wav(raw, aligned, plan.atempo_chain)
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
    n = int(round(max(0, ms) * config.CANON_SR / 1000))
    with atomic_output(out_wav) as tmp, wave.open(str(tmp), "wb") as w:
        w.setnchannels(config.CANON_CHANNELS)
        w.setsampwidth(2)
        w.setframerate(config.CANON_SR)
        w.writeframes(b"\x00" * (n * 2 * config.CANON_CHANNELS))


# --------------------------------------------------------------------------- #
def mux(paths: JobPaths, keep_ambient: bool = True, force: bool = False) -> Path:
    paths.ensure()
    # mux promises BOTH mp4 + srt; require both before skipping, so a crash
    # between ff.mux() and _write_srt() is repaired on a non-force re-run.
    if paths.dubbed_video.exists() and paths.subtitles.exists() and not force:
        _log("mux: cached")
        return paths.dubbed_video
    # Reaching here means a (re)build is needed (forced, or an incomplete pair).
    # Clear both deliverables first so a failure mid-rewrite leaves an incomplete
    # (repairable) set, never a stale mp4+srt pair that looks cached next time.
    paths.dubbed_video.unlink(missing_ok=True)
    paths.subtitles.unlink(missing_ok=True)
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
    with atomic_output(path) as tmp:
        tmp.write_text("\n".join(lines), encoding="utf-8")


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
    separate: bool = False,
    keep_ambient: bool = True,
    force: bool = False,
) -> Path:
    """End-to-end local dub: ingest -> prepare -> transcribe -> translate -> tts
    -> align -> mux. Returns the dubbed video path (subtitles alongside it).

    The Tier 1 kernel never enables paid providers (CLAUDE.md red line §1/§14):
    there is deliberately no allow_paid opt-in here — the stages always pass
    allow_paid=False to the resolver, which enforces the paid gate.
    """
    ingest(paths, source, force=force)
    prepare(paths, separate=separate, force=force)
    transcribe(paths, resolver, asr, source_lang, force=force)
    translate(paths, resolver, mt, target_lang, source_lang, force=force)
    tts(paths, resolver, tts_provider, force=force)
    align(paths, force=force)
    return mux(paths, keep_ambient=keep_ambient, force=force)
