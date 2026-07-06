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

from ovt_schemas.contracts import (
    AigcMarking,
    DubbingSegment,
    Job,
    Transcript,
    TranslationResult,
    WorkerMeta,
)

from . import aigc, config
from . import ffmpeg_utils as ff
from .config import JobPaths
from .isolation import pin_resolver
from .jsonio import atomic_output, read_json, write_json
from .manifest import write_manifest
from .providers import DiarizerProvider, ProviderUnavailable, Resolver, TtsProvider


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
        # Validate even the cached original: a worker pre-stage, or an interrupted /
        # rejected prior run, could have left a disallowed source here, and a cache hit
        # must never feed an unvalidated source into transcribe/translate (CodeX R4).
        ff.assert_ffmpeg()
        ff.assert_allowed_input_format(existing)
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
    # Drop any prior audio BEFORE validating/extracting: a rejected or failed source
    # must not leave stale audio that a later non-force run would pair with it (the
    # cache return above keys on original_audio existing) — extract_audio also only
    # replaces on success (CodeX R4 + T1.3c).
    paths.original_audio.unlink(missing_ok=True)
    # SSRF guard (T1.3c): refuse a source whose real container is a playlist / concat /
    # network demuxer disguised as media, before ffmpeg extracts audio from it.
    ff.assert_allowed_input_format(video)
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
def _dominant_speaker(
    start_ms: int, end_ms: int, turns: list[tuple[int, int, str]]
) -> str | None:
    """The speaker_id whose turns overlap the line span ``[start_ms, end_ms)`` the most (by summed
    overlap ms). ``None`` when no turn overlaps (the caller keeps the line's existing label)."""
    by_speaker: dict[str, int] = {}
    for t_start, t_end, sid in turns:
        overlap = min(end_ms, t_end) - max(start_ms, t_start)
        if overlap > 0:
            by_speaker[sid] = by_speaker.get(sid, 0) + overlap
    if not by_speaker:
        return None
    # Tie-break deterministically by speaker_id so the same audio always labels identically.
    return max(sorted(by_speaker), key=lambda s: by_speaker[s])


# Resume skip-gate for diarize(): its existence means the diarizer has ALREADY run against the
# current (cached) transcript, so a resume must not reload the ~30MB sherpa model to recompute a
# result it already has. Written LAST (after the relabel is durable) so a crash mid-diarize re-runs
# rather than skipping a half-applied relabel. ``force`` bypasses it (re-runs the model).
_DIARIZED_MARKER = ".diarized"


def diarize(paths: JobPaths, diarizer: DiarizerProvider, force: bool = False) -> Transcript:
    """Relabel transcript ``speaker_id``s from a diarizer's speaker turns (P4, 分角色配音).

    Runs AFTER transcribe (needs the transcript + the extracted speech), BEFORE translate (which
    passes speaker_id through to segments, where ``_assign_voices`` gives each distinct speaker a
    distinct voice). Each line takes the speaker of the turn it overlaps most; a no-overlap line
    keeps its existing label (``SPEAKER_00``). A diarizer that finds one/zero speakers leaves the
    single-speaker baseline intact — enabling diarization can only ADD speakers, never break a job.

    Resume-cached (P4b): once the diarizer has run for the current transcript a ``.diarized`` marker
    is written, so a non-``force`` resume skips reloading the heavy sourcing model (its result is
    already persisted in transcript.json). ``force=True`` re-runs the diarizer and rewrites the
    marker. A ``force`` re-run of transcribe upstream also forces diarize (run_pipeline threads the
    same ``force``), so the marker can never mask a freshly re-transcribed, unlabeled transcript."""
    marker = paths.root / _DIARIZED_MARKER
    transcript = Transcript.model_validate(read_json(paths.transcript))
    if marker.exists() and not force:
        _log("diarize: cached")
        return transcript
    if not transcript.lines:
        # No speech to label: return before loading the model (cheap to re-check on resume, so no
        # marker is written — the diarizer never ran).
        _log("diarize: no transcript lines; nothing to relabel")
        return transcript
    turns = list(diarizer.diarize(str(paths.speech)))
    if not turns:
        _log("diarize: diarizer found no speaker turns; keeping single-speaker labels")
        # The model DID run and is determinate for this audio: mark so a resume doesn't reload it
        # only to find no speakers again.
        marker.write_text("no-turns", encoding="utf-8")
        return transcript
    for ln in transcript.lines:
        sid = _dominant_speaker(ln.start_ms, ln.end_ms, turns)
        if sid is not None:
            ln.speaker_id = sid
    write_json(paths.transcript, transcript.model_dump())
    n_speakers = len({ln.speaker_id for ln in transcript.lines})
    # Marker LAST: the relabeled transcript is now durable, so a crash before this line re-runs
    # diarize on resume (never skips a half-written relabel).
    marker.write_text(f"speakers={n_speakers}", encoding="utf-8")
    _log(f"diarize: relabeled {len(transcript.lines)} line(s) -> {n_speakers} speaker(s)")
    return transcript


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
def _assign_voices(
    provider: TtsProvider, lang: str, speaker_ids: list[str],
    *, pinned_voice: str | None = None,
) -> dict[str, str]:
    speakers = sorted(set(speaker_ids))
    # An explicit user pin (JobPlan.tts_voice) wins: every speaker uses the pinned voice. The pin is
    # trusted here — the worker soft-pin router already validated it: the provider is installed, not
    # paid, covers the locale, and the voice is a member of the provider's CLOSED preset set (the
    # open-core guardrail, plan §4), and it clears the pin on a fallback. So we skip voices_for and
    # use the pin directly. (The no-worker CLI path is operator-trusted, like FVD_PIPER_MODEL.)
    # Per-speaker voice_map is P4.
    if pinned_voice:
        return {sid: pinned_voice for sid in speakers}
    voices = provider.voices_for(lang)
    if not voices:
        raise ProviderUnavailable(f"{provider.info.name} has no voice for language {lang!r}")
    return {sid: voices[i % len(voices)] for i, sid in enumerate(speakers)}


def tts(paths: JobPaths, resolver: Resolver, provider: str | None,
        force: bool = False, *, voice_id: str | None = None) -> TranslationResult:
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
                               [s.speaker_id for s in result.segments],
                               pinned_voice=voice_id)

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
_OUTPUT_MODES = ("subtitle_only", "dub_only", "both")
_SUBTITLE_LANGS = ("target", "bilingual")
_SUBTITLE_DELIVERIES = ("srt", "burned", "both")


def mux(
    paths: JobPaths,
    keep_ambient: bool = True,
    force: bool = False,
    *,
    output_mode: str = "both",
    subtitle_lang: str = "target",
    subtitle_delivery: str = "srt",
    marking: AigcMarking | None = None,
    burn_font: str | None = None,
    watermark_font: str | None = None,
) -> Path:
    """Compose the job's deliverables per ``output_mode`` (T1.3d).

    - ``dub_only`` / ``both`` -> a dubbed ``dubbed_video.mp4`` (stitched dubbed
      audio over the source video).
    - ``subtitle_only`` / ``both`` -> ``subtitles.srt`` when ``subtitle_delivery``
      includes srt; ``subtitle_lang == "bilingual"`` emits target + source lines.
    - burned subtitle delivery is a guarded placeholder until M2.1 (feature-flag off).

    When ``marking`` is enabled (T1.3b) the AIGC legal mark is applied conditionally
    on output_mode: the dubbed video carries embedded AIGC container metadata and an
    AIGC-marked subtitle leads with a machine-translation disclosure cue. ``marking``
    is mutated with ``applied=True`` so the manifest/audit trail records it.

    Returns the primary deliverable: the dubbed video when dubbing, else the srt.
    The deliverable set is the resume cache gate — a re-run rebuilds until every
    expected file is present, so a crash mid-build never looks cached.
    """
    if output_mode not in _OUTPUT_MODES:
        raise ValueError(f"unknown output_mode: {output_mode!r} (expected {_OUTPUT_MODES})")
    if subtitle_lang not in _SUBTITLE_LANGS:
        raise ValueError(f"unknown subtitle_lang: {subtitle_lang!r}")
    if subtitle_delivery not in _SUBTITLE_DELIVERIES:
        raise ValueError(f"unknown subtitle_delivery: {subtitle_delivery!r}")
    paths.ensure()
    # RED LINE §3 (default-on): mux is exported and writes final deliverables directly, so it
    # must NOT emit an unmarked output by omission. When no marking is supplied, default it ON
    # by output_mode — exactly like run_pipeline — so a direct caller can't bypass the mark.
    # Turning the mark OFF requires an explicit AigcMarking(enabled=False) (the audited
    # acknowledgment §3 demands), which embed_method/_on already honour (CodeX bot P1).
    if marking is None:
        marking = aigc.default_marking(output_mode)

    want_video = output_mode in ("dub_only", "both")
    want_subs = output_mode in ("subtitle_only", "both")
    want_srt = want_subs and subtitle_delivery in ("srt", "both")
    burn_requested = want_subs and subtitle_delivery in ("burned", "both")
    want_burn = burn_requested and config.BURN_SUBTITLES_ENABLED  # M2.1 libass re-encode
    if burn_requested and not want_burn:
        # Burn requested (burned/both) but the re-encode is OFF (ops fallback). We can't produce the
        # burned video the user asked for, and neither worker nor complete() can tell an unburned
        # video from a burned one — so FAIL LOUD (coded terminal) rather than silently ship a plain
        # video (e.g. both+both would fall through to the plain dub) or diverge from the delivery
        # contract. Ops must re-enable BURN_SUBTITLES_ENABLED to serve any burn request.
        raise NotImplementedError(
            "subtitle_delivery requires the burn re-encode, but BURN_SUBTITLES_ENABLED is off")

    # PR-2 visible AIGC watermark (§14, owner-authorized): the POLICY gate + resolved text come from
    # aigc (DEFAULT-OFF; removing it strips NO legal mark — the container metadata mark stays). The
    # render params come from the marking; the deployment font from the caller (watermark_font, like
    # burn_font — NOT job-derived). It applies ONLY to a video deliverable (dub or burned), so it is
    # left None for a subtitle_only+srt job (no video ⇒ no needless re-encode); `watermark is not
    # None` is then the single "the mark applies" predicate below.
    wm_text = aigc.video_watermark_text(marking)
    watermark = (
        ff.Watermark(
            text=wm_text,
            position=marking.video_watermark_position,
            size_pct=marking.video_watermark_font_size,
            opacity_pct=marking.video_watermark_opacity,
            color=marking.video_watermark_color,
            fontfile=watermark_font,
        )
        if (wm_text is not None and (want_video or want_burn))
        else None
    )

    # When burning, the burned_video IS the delivered video and dubbed_video is only its
    # intermediate (the burn source for a dub+burn job) — so it is built but not delivered.
    deliver_video = (
        paths.burned_video if want_burn else (paths.dubbed_video if want_video else None)
    )
    expected = [p for p in (deliver_video, paths.subtitles if want_srt else None) if p is not None]
    primary = deliver_video if deliver_video is not None else paths.subtitles
    # The AIGC mark the deliverables must carry ("" = unmarked). A marker file records
    # what the cached artifacts were actually built with, so a marking change forces a
    # re-mux: the cache can neither under-claim (resume of a marked run) nor over-claim
    # (cached artifacts from an earlier unmarked run presented as marked) — red line §3.
    want_method = aigc.embed_method(marking, output_mode) or ""
    # The marker records EVERY setting that shapes the deliverables (AIGC method +
    # output_mode + subtitle settings), so changing any of them (e.g. target ->
    # bilingual subtitles, or a marking change) invalidates the cache and forces a
    # rewrite (CodeX R3/R5) — the cache never serves a deliverable built for other
    # settings, and never over-/under-claims the §3 mark.
    cache_key_parts = [want_method, output_mode, subtitle_lang, subtitle_delivery]
    # §14: the subtitle cue (custom text + on/off) shapes the SRT/burned output but is NOT
    # in want_method — for `both`, embed_method stays av_voice_mark regardless of the
    # subtitle channel, and want_method ignores custom cue text. Fold the cue into the key
    # so a subtitle_enabled / subtitle_text change forces a re-mux, not a stale subtitle.
    if want_srt or want_burn:
        cache_key_parts.append(f"subcue:{aigc.subtitle_disclosure(marking) or ''}")
    if want_burn:
        # The burn re-encode caps shape the pixels too, so fold them into the key — but ONLY
        # when burning, so a non-burn job's key stays byte-identical to before (no needless
        # cache churn / marker invalidation). A BURN_MAX_HEIGHT/crf/preset change re-burns.
        cache_key_parts.append(
            f"burn:{config.BURN_MAX_WIDTH}:{config.BURN_MAX_HEIGHT}:{config.BURN_CRF}:"
            f"{config.BURN_PRESET}:{burn_font or ''}"
        )
    # §14 PR-2: the visible watermark shapes the delivered pixels but is NOT in want_method — fold
    # every watermark param (incl. text + deployment font) into the key so toggling it on/off or
    # editing any field forces a re-mux. Appended ONLY when it applies, so a default-off job's key
    # stays byte-identical to pre-PR-2 (no cache churn / marker invalidation), like the burn key.
    if watermark is not None:
        cache_key_parts.append(
            f"wm:{watermark.position}:{watermark.size_pct}:{watermark.opacity_pct}:"
            f"{watermark.color}:{watermark.fontfile or ''}:{watermark.text}"
        )
    cache_key = "|".join(cache_key_parts)
    marker = paths.output / ".mux_cache"
    cached_key = marker.read_text(encoding="utf-8") if marker.exists() else ""
    if (expected and all(p.exists() for p in expected)
            and cached_key == cache_key and not force):
        _log("mux: cached")
        if marking is not None and (want_method or watermark is not None):
            marking.applied = True  # cached artifacts carry the mark (method and/or watermark)
        return primary
    # Rebuild: clear EVERY known deliverable, not just the requested ones. When the output
    # mode narrows on a reused job dir (both -> subtitle_only / dub_only), a stale deliverable
    # from the previous wider mode (e.g. dubbed_video.mp4 / subtitles.srt) must not linger for
    # presence-based packaging/upload to publish (CodeX bot P2). Expected ones are rebuilt below;
    # clearing first also keeps a mid-build failure repairable, never stale-looking-as-cached.
    for p in (paths.dubbed_video, paths.subtitles, paths.burned_video):
        p.unlink(missing_ok=True)

    result = TranslationResult.model_validate(read_json(paths.segments))
    if want_video:
        video = paths.original_video()
        if not video:
            raise RuntimeError("mux: no original video")
        ff.assert_ffmpeg()
        # mux is exported and may run on a pre-staged / resumed job that bypassed
        # ingest's allowlist — validate the source at THIS ffmpeg boundary too, so a
        # crafted video/original.* can never be parsed by ffprobe/ffmpeg here (CodeX R4).
        ff.assert_allowed_input_format(video)
        total_ms = ff.probe_duration_ms(video)
        placements = [(s.start_ms, paths.tts_aligned(s.index))
                      for s in result.segments if paths.tts_aligned(s.index).exists()]
        ff.stitch_timeline(placements, paths.dubbed_audio, total_ms)
        if placements:
            _log(f"mux: composed {len(placements)} segments into dubbed audio")
        else:
            # A dub-mode job with no aligned audio (all keep_original, or every
            # tts/align produced nothing) yields a silent track — surface it rather
            # than ship a silently-silent video as if it were a normal dub.
            _log(f"mux: WARNING dubbed 0 of {len(result.segments)} segments "
                 "(all keep_original or missing aligned audio); dub track is silent")
        ambient = paths.ambient if (keep_ambient and paths.ambient.exists()) else None
        ff.mux(video, paths.dubbed_audio, paths.dubbed_video, ambient=ambient,
               metadata=aigc.metadata_args(marking, output_mode))
        _log(f"mux: wrote {paths.dubbed_video.name}")
        if watermark is not None and not want_burn:
            # The delivered dub video gets the visible watermark as a SECOND re-encode pass (the
            # stream-copy mux above is untouched). When burning, the overlay instead rides the burn
            # re-encode below — so a both+burned job's throwaway dubbed_video intermediate is not
            # watermarked here (it is never delivered), avoiding a wasted pass.
            ff.watermark_video(
                paths.dubbed_video, paths.dubbed_video, watermark,
                timeout_sec=config.BURN_ENCODE_TIMEOUT_SEC,
                crf=config.BURN_CRF, preset=config.BURN_PRESET,
            )
            _log("mux: burned visible AIGC watermark into dubbed video")
    # Write the srt LAST so a failed video mux above leaves no lone deliverable
    # that the next run might otherwise treat as progress.
    # The subtitle text is delivered as srt and/or used as the burn source — build it once.
    srt_for_burn: Path | None = None
    if want_srt:
        _write_srt(result, paths.subtitles, bilingual=(subtitle_lang == "bilingual"),
                   disclosure=aigc.subtitle_disclosure(marking))
        _log(f"mux: wrote {paths.subtitles.name}")
        srt_for_burn = paths.subtitles
    if want_burn:
        # Burn onto the dubbed video when dubbing, else onto the original (subtitle_only +
        # burned). burn_subtitles re-validates the source container at its ffmpeg boundary.
        burn_src = paths.dubbed_video if want_video else paths.original_video()
        if not burn_src:
            raise RuntimeError("mux: no source video to burn subtitles onto")
        with tempfile.TemporaryDirectory(dir=paths.output) as _td:
            if srt_for_burn is None:
                # burn-only delivery: the srt is a throwaway burn source, never a deliverable.
                srt_for_burn = Path(_td) / "subs.srt"
                _write_srt(result, srt_for_burn, bilingual=(subtitle_lang == "bilingual"),
                           disclosure=aigc.subtitle_disclosure(marking))
            ff.burn_subtitles(
                burn_src, srt_for_burn, paths.burned_video,
                max_width=config.BURN_MAX_WIDTH, max_height=config.BURN_MAX_HEIGHT,
                timeout_sec=config.BURN_ENCODE_TIMEOUT_SEC,
                crf=config.BURN_CRF, preset=config.BURN_PRESET,
                # Per-locale libass font (resolved by the orchestrator from the language registry —
                # the kernel stays pure, no registry import). None -> libass default (Latin).
                force_style=(f"FontName={burn_font}" if burn_font else None),
                metadata=aigc.metadata_args(marking, output_mode),
                # PR-2: the visible watermark rides THIS re-encode (no extra pass) when burning.
                # `watermark` is already None unless it applies to a video deliverable (see above).
                watermark=watermark,
            )
        _log(f"mux: wrote {paths.burned_video.name}")
    # Record what was actually marked alongside the deliverables, so a later resume
    # can trust (or invalidate) the cache. applied reflects an actually-written mark
    # (non-empty method), never merely that marking was enabled — the §3 audit trail
    # must not claim a mark that no artifact carries.
    if expected:
        marker.write_text(cache_key, encoding="utf-8")
    if marking is not None and (want_method or watermark is not None):
        marking.applied = True
    return primary


def _write_srt(
    result: TranslationResult, path: Path, *,
    bilingual: bool = False, disclosure: str | None = None,
) -> None:
    def ts(ms: int) -> str:
        ms = max(0, ms)
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    segs = [s for s in result.segments if (s.target_text.strip() or s.source_text.strip())]
    disc = disclosure or ""
    # AIGC machine-translation disclosure (T1.3b): a short LEADING notice. Clamp its window to
    # END at the first real cue so it never overlaps actual subtitle content — overlapping cues
    # make players stack/hide text (@CodeX bot P2). With no usable gap before the first cue
    # (< 500 ms), ride the disclosure on that first cue's text instead of an overlapping cue.
    first_start = segs[0].start_ms if segs else 3000
    disclose_standalone = bool(disc) and first_start >= 500
    disclose_on_first = bool(disc) and not disclose_standalone

    lines: list[str] = []
    n = 0
    if disclose_standalone:
        n += 1
        lines += [str(n), f"{ts(0)} --> {ts(min(3000, first_start))}", disc, ""]
    for i, seg in enumerate(segs):
        target = seg.target_text.strip()
        source = seg.source_text.strip()
        n += 1
        lines.append(str(n))
        lines.append(f"{ts(seg.start_ms)} --> {ts(seg.end_ms)}")
        body: list[str] = []
        if disclose_on_first and i == 0:
            body.append(disc)  # no gap for a standalone cue -> ride on the first cue
        # Bilingual cue = target (deliverable language) on top, source below; both kept even
        # when MT == source (names/acronyms/punctuation) for a consistent bilingual layout
        # (@CodeX bot P3). Falls back to one line only when a side is empty (keep_original).
        if bilingual and target and source:
            body += [target, source]
        else:
            body.append(target or source)
        lines += body
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
    tts_voice: str | None = None,
    diarizer: DiarizerProvider | None = None,
    separate: bool = False,
    keep_ambient: bool = True,
    force: bool = False,
    output_mode: str = "both",
    subtitle_lang: str = "target",
    subtitle_delivery: str = "srt",
    aigc_marking: AigcMarking | None = None,
    burn_font: str | None = None,
    watermark_font: str | None = None,
    job: Job | None = None,
) -> Path:
    """End-to-end local pipeline: ingest -> prepare -> transcribe -> translate ->
    [tts -> align] -> mux. Returns the primary deliverable path.

    ``output_mode`` (T1.3d) decides which stages run and which deliverables are
    produced: ``subtitle_only`` skips tts + align and emits only subtitles;
    ``dub_only`` / ``both`` run the full dub. ``subtitle_lang`` / ``subtitle_delivery``
    shape the subtitle output (bilingual lines; burned is an M2.1 placeholder).

    The Tier 1 kernel never enables paid providers (CLAUDE.md red line §1/§14):
    there is deliberately no allow_paid opt-in here, and the resolver is pinned
    (T1.3a) so even a future change can't slip a paid provider past the gate.

    When ``job`` is supplied (the authoritative record from the control plane /
    local-runner) a ``manifest.json`` is written alongside the deliverables (T1.3a).
    """
    resolver = pin_resolver(resolver)
    if job is not None:
        # The Job is the authoritative record — derive every job-defined setting from
        # it so what runs can't diverge from the manifest written for it (CodeX P1).
        # Runtime knobs (source / separate / keep_ambient / force) stay caller-supplied;
        # the flat kwargs drive only the no-job ad-hoc (CLI) path.
        target_lang = job.target_lang
        source_lang = job.source_lang_hint
        output_mode = job.output_mode
        subtitle_lang = job.subtitle_lang
        subtitle_delivery = job.subtitle_delivery
        aigc_marking = job.aigc_marking
        asr, mt, tts_provider = job.plan.asr, job.plan.mt, job.plan.tts
        # Explicit user-pinned dub voice (P0); null unless the picker sent one. The worker soft-pin
        # router has already validated/rerouted it before this Job reaches the kernel.
        tts_voice = job.plan.tts_voice
    elif aigc_marking is None:
        # Red line §3: AIGC marking is default-ON. The ad-hoc / no-job path never
        # ships an unmarked deliverable by omission — disabling needs an explicit
        # (audited) AigcMarking(enabled=False) from the caller.
        aigc_marking = aigc.default_marking(output_mode)
    ingest(paths, source, force=force)
    prepare(paths, separate=separate, force=force)
    transcribe(paths, resolver, asr, source_lang, force=force)
    # P4 (opt-in, 分角色配音): relabel speakers BEFORE translate, so _assign_voices later gives each
    # speaker a distinct voice. Gated on BOTH the Job's diarization flag AND an injected diarizer
    # (the worker supplies a sherpa-onnx diarizer under its RAM mutex). The no-job CLI path, a
    # flag-off job, or P4a-without-a-diarizer all skip it → single-speaker, byte-identical to today.
    if diarizer is not None and job is not None and job.plan.diarization:
        diarize(paths, diarizer, force=force)
    translate(paths, resolver, mt, target_lang, source_lang, force=force)
    # subtitle_only needs no synthesized/aligned audio — skip tts + align entirely.
    if output_mode in ("dub_only", "both"):
        tts(paths, resolver, tts_provider, force=force, voice_id=tts_voice)
        align(paths, force=force)
    # burn_font (per-locale libass name) and watermark_font (a deployment-wide font PATH for the
    # visible AIGC watermark) are caller-supplied, NOT Job-derived — the orchestrator resolves them
    # (language registry / deployment env), keeping the kernel pure. The watermark font is
    # deployment-wide (not per-locale): the operator text (default zh) is independent of the target
    # language, so it needs one CJK-capable font, not the target's burn font.
    out = mux(paths, keep_ambient=keep_ambient, force=force, output_mode=output_mode,
              subtitle_lang=subtitle_lang, subtitle_delivery=subtitle_delivery,
              marking=aigc_marking, burn_font=burn_font, watermark_font=watermark_font)
    if job is not None:
        # Record the embed method only when a mark was actually applied (mux sets marking.applied on
        # the non-empty deliverable set), so the manifest never over-claims. §3 also forbids the
        # inverse UNDER-claim: a visible watermark can be the ONLY applied mark — for a
        # subtitle_only+burned job with the subtitle cue off, embed_method() is None yet the burned
        # video visibly carries the drawtext overlay. applied=True with a None embed_method can ONLY
        # arise that way (applied = want_method-non-empty OR watermark-applied, and want_method is
        # empty exactly when embed_method is None), so record "visible_watermark" rather than a
        # self-contradictory None. worker_meta.ffprobe blob is deferred to T1.4; models[] to T1.3g.
        method: str | None = None
        if aigc_marking is not None and aigc_marking.applied:
            method = aigc.embed_method(aigc_marking, output_mode) or "visible_watermark"
        write_manifest(paths, job, WorkerMeta(aigc_embed_method=method))
    return out
