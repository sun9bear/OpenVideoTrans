"""ASR (speech-to-text) providers. Cloud-preferred ladder: groq -> cloudflare ->
faster_whisper (backlog T1.2 "阶梯云优先 groq→CF→(faster_whisper cli)").

All return an ``ovt_schemas`` Transcript with word-level timing where the backend
supports it (the align stage needs it). Cloud providers fall back to a single
speaker — fine for the free tier (generic preset voices).

NOTE: Cloudflare ASR here issues a SINGLE request. Long-audio chunking
(``asr_chunker``: compress-first, split, offset-merge) lands in T1.3e, which may
bridge to autodub-core's ffmpeg helpers — this package keeps no autodub-core import
edge (T1.1 ∥ T1.2, backlog DAG), so it uses stdlib ``wave`` for duration here.
"""

from __future__ import annotations

import contextlib
import wave
from pathlib import Path

from ovt_schemas.contracts import Transcript, TranscriptLine, Word

from ._env import env
from .base import ASRProvider, ProviderInfo, ProviderUnavailable, has_module, register

_LINE_GAP_MS = 700  # silence gap that starts a new transcript line when grouping words
_MAX_LINE_MS = 12000


def _wav_duration_ms(path: str) -> int:
    """Duration of a PCM wav via stdlib (no ffmpeg). 0 for non-wav / unreadable."""
    try:
        with contextlib.closing(wave.open(path, "rb")) as w:
            frames, rate = w.getnframes(), w.getframerate()
        return int(round(frames * 1000 / rate)) if rate else 0
    except Exception:  # noqa: BLE001 - best-effort duration; caller tolerates 0
        return 0


def _iso639(lang: str | None) -> str | None:
    """Reduce a BCP-47 source hint (project standard, e.g. 'pt-BR' / 'zh-Hans') to the
    bare ISO-639 code Whisper-compatible ASR APIs expect ('pt' / 'zh'). Prevents a
    region/script subtag from causing a provider 400; full language vetting is T1.3f.
    Returns None for an empty/None hint (let the backend auto-detect)."""
    return lang.split("-")[0].lower() if lang else None


# Whisper via OpenAI/groq verbose_json reports a detected language NAME ("english"), not a
# code; map the common Tier-1 targets to ISO-639-1 so the transcript stores a code the MT
# stage can use. Unknown names fall back to "auto" (clean-fail downstream — CloudflareMT
# rejects "auto"); the full language registry + detection backfill is T1.3f.
_WHISPER_NAME_TO_ISO = {
    "english": "en", "chinese": "zh", "spanish": "es", "french": "fr", "german": "de",
    "japanese": "ja", "korean": "ko", "portuguese": "pt", "italian": "it", "russian": "ru",
    "dutch": "nl", "arabic": "ar", "hindi": "hi", "turkish": "tr", "polish": "pl",
    "vietnamese": "vi", "thai": "th", "indonesian": "id", "ukrainian": "uk",
}


def _detected_to_iso(detected: str | None) -> str | None:
    """Normalize an ASR-reported source language to ISO-639-1. faster_whisper already
    returns a code; OpenAI/groq return a NAME — map the common ones. None when unknown."""
    if not detected:
        return None
    d = detected.strip().lower()
    if len(d) == 2 and d.isalpha():
        return d  # already an ISO-639-1 code (e.g. faster_whisper)
    return _WHISPER_NAME_TO_ISO.get(d)  # name -> code, or None for an unmapped language


def _group_words_into_lines(
    words: list[Word], full_text: str, total_ms: int
) -> list[TranscriptLine]:
    if not words:
        text = full_text.strip()
        if not text:
            # No words AND no text -> genuinely no speech (silence / ambient-only): return
            # NO lines, so the kernel's translate() skips MT for the no-speech job rather
            # than seeing a blank line and still selecting/configuring a provider (CodeX).
            return []
        return [
            TranscriptLine(
                index=0, start_ms=0, end_ms=total_ms, source_text=text,
                words=[], speaker_id="SPEAKER_00",
            )
        ]
    lines: list[TranscriptLine] = []
    buf: list[Word] = []
    for w in words:
        if buf and (
            w.start_ms - buf[-1].end_ms > _LINE_GAP_MS
            or w.end_ms - buf[0].start_ms > _MAX_LINE_MS
        ):
            lines.append(_line_from_words(len(lines), buf))
            buf = []
        buf.append(w)
    if buf:
        lines.append(_line_from_words(len(lines), buf))
    return lines


def _line_from_words(index: int, words: list[Word]) -> TranscriptLine:
    text = " ".join(w.text.strip() for w in words).strip()
    return TranscriptLine(
        index=index, start_ms=words[0].start_ms, end_ms=words[-1].end_ms,
        source_text=text, words=list(words), speaker_id="SPEAKER_00",
    )


# --------------------------------------------------------------------------- #
class FasterWhisperASR(ASRProvider):
    info = ProviderInfo(
        "faster_whisper", "asr", paid=False,
        requires="pip install faster-whisper (local CPU/GPU)",
        languages="~99 languages", notes="local fallback; word timestamps",
    )

    def available(self) -> bool:
        return has_module("faster_whisper")

    def transcribe(self, audio_path: str, source_lang: str | None) -> Transcript:
        self._ensure_available()
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        model_size = env("FVD_WHISPER_MODEL", "base")
        device = env("FVD_WHISPER_DEVICE", "cpu")
        compute = env("FVD_WHISPER_COMPUTE", "int8")
        model = WhisperModel(model_size, device=device, compute_type=compute)
        segments, info = model.transcribe(
            audio_path, word_timestamps=True, language=_iso639(source_lang), vad_filter=True,
        )
        lines: list[TranscriptLine] = []
        for seg in segments:
            words = [
                Word(text=w.word.strip(), start_ms=int(w.start * 1000), end_ms=int(w.end * 1000))
                for w in (seg.words or [])
            ]
            lines.append(
                TranscriptLine(
                    index=len(lines), start_ms=int(seg.start * 1000), end_ms=int(seg.end * 1000),
                    speaker_id="SPEAKER_00", source_text=seg.text.strip(), words=words,
                )
            )
        # Prefer the caller's hint (already a code) over the detected language, then
        # normalize the detected value; "auto" only when neither yields a usable code.
        return Transcript(
            source_language=_iso639(source_lang) or _detected_to_iso(info.language) or "auto",
            lines=lines, asr_provider="faster_whisper",
        )


# --------------------------------------------------------------------------- #
class _OpenAICompatASR(ASRProvider):
    """Shared client for the OpenAI-compatible /audio/transcriptions shape
    (Groq and OpenAI both speak it)."""

    base_url = ""
    key_env = ""
    model_env = ""
    default_model = ""

    def _key(self) -> str | None:
        return env(self.key_env)

    def available(self) -> bool:
        return has_module("requests") and self._key() is not None

    def transcribe(self, audio_path: str, source_lang: str | None) -> Transcript:
        self._ensure_available()
        import requests  # lazy

        model = env(self.model_env, self.default_model)
        data = {
            "model": model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["segment", "word"],
        }
        norm_lang = _iso639(source_lang)
        if norm_lang:
            data["language"] = norm_lang
        with open(audio_path, "rb") as fh:
            files = {"file": (Path(audio_path).name, fh, "audio/wav")}
            resp = requests.post(
                f"{self.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self._key()}"},
                data=data, files=files, timeout=600,
            )
        if resp.status_code >= 400:
            raise ProviderUnavailable(
                f"{self.info.name} ASR HTTP {resp.status_code}: {resp.text[:500]}"
            )
        return self._parse(resp.json(), source_lang)

    def _parse(self, j: dict, source_lang: str | None) -> Transcript:
        words_raw = j.get("words") or []
        words = [
            Word(text=str(w.get("word", "")).strip(),
                 start_ms=int(float(w["start"]) * 1000), end_ms=int(float(w["end"]) * 1000))
            for w in words_raw if "start" in w and "end" in w
        ]
        lines: list[TranscriptLine] = []
        for seg in j.get("segments") or []:
            s_ms, e_ms = int(float(seg["start"]) * 1000), int(float(seg["end"]) * 1000)
            seg_words = [w for w in words if s_ms <= (w.start_ms + w.end_ms) // 2 <= e_ms]
            lines.append(
                TranscriptLine(
                    index=len(lines), start_ms=s_ms, end_ms=e_ms, speaker_id="SPEAKER_00",
                    source_text=str(seg.get("text", "")).strip(), words=seg_words,
                )
            )
        if not lines:
            total = words[-1].end_ms if words else 0
            lines = _group_words_into_lines(words, j.get("text", ""), total)
        # Prefer the caller's hint over Whisper's detected NAME ("english"), normalizing
        # both to ISO-639-1 (CodeX): a raw name would break the default ASR->CloudflareMT
        # handoff. "auto" only when neither yields a usable code.
        return Transcript(
            source_language=_iso639(source_lang) or _detected_to_iso(j.get("language")) or "auto",
            lines=lines, asr_provider=self.info.name,
        )


class GroqASR(_OpenAICompatASR):
    info = ProviderInfo(
        "groq", "asr", paid=False,
        requires="GROQ_API_KEY (free tier) + pip install requests",
        languages="multilingual",
        notes="fast free cloud whisper; word-level on whisper-large-v3",
    )
    base_url = "https://api.groq.com/openai/v1"
    key_env = "GROQ_API_KEY"
    model_env = "FVD_GROQ_ASR_MODEL"
    default_model = "whisper-large-v3-turbo"


class OpenAIASR(_OpenAICompatASR):
    info = ProviderInfo(
        "openai", "asr", paid=True,
        requires="OPENAI_API_KEY (pay-as-you-go $0.006/min)",
        languages="multilingual", notes="PAID — opt-in only",
    )
    base_url = "https://api.openai.com/v1"
    key_env = "OPENAI_API_KEY"
    model_env = "FVD_OPENAI_ASR_MODEL"
    # verbose_json + word timestamp_granularities are only supported on whisper-1; do
    # NOT override with a gpt-4o-*-transcribe model (they reject verbose_json -> HTTP 400).
    default_model = "whisper-1"


# --------------------------------------------------------------------------- #
class CloudflareASR(ASRProvider):
    info = ProviderInfo(
        "cloudflare", "asr", paid=False,
        requires="CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN (free 10k neurons/day)",
        languages="multilingual",
        notes="@cf/openai/whisper; word timestamps. Long-audio chunking lands in T1.3e.",
    )

    def available(self) -> bool:
        return bool(
            has_module("requests")
            and env("CLOUDFLARE_ACCOUNT_ID")
            and env("CLOUDFLARE_API_TOKEN")
        )

    def transcribe(self, audio_path: str, source_lang: str | None) -> Transcript:
        self._ensure_available()
        # Single request (no chunking yet — asr_chunker is T1.3e). Long inputs may be
        # rejected by the model's size limit until then.
        words = self._run_one(audio_path)
        total = words[-1].end_ms if words else 0
        lines = _group_words_into_lines(words, "", total)
        return Transcript(
            source_language=source_lang or "auto", lines=lines, asr_provider="cloudflare"
        )

    def _run_one(self, chunk_path: str) -> list[Word]:
        import requests  # lazy

        acct, token = env("CLOUDFLARE_ACCOUNT_ID"), env("CLOUDFLARE_API_TOKEN")
        model = env("FVD_CF_ASR_MODEL", "@cf/openai/whisper")
        url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{model}"
        with open(chunk_path, "rb") as fh:
            audio = fh.read()
        resp = requests.post(
            url, headers={"Authorization": f"Bearer {token}"}, data=audio, timeout=300
        )
        if resp.status_code >= 400:
            raise ProviderUnavailable(f"cloudflare ASR HTTP {resp.status_code}: {resp.text[:500]}")
        result = resp.json().get("result", {})
        words = result.get("words") or []
        if words:
            return [
                Word(text=str(w.get("word", "")).strip(),
                     start_ms=int(float(w["start"]) * 1000), end_ms=int(float(w["end"]) * 1000))
                for w in words
            ]
        # no words[]: one pseudo-word spanning the chunk text (duration via stdlib wave)
        text = str(result.get("text", "")).strip()
        if not text:
            # no words AND no text -> no speech: return [] so transcribe() builds an empty
            # transcript and the kernel skips MT, instead of fabricating a blank Word that
            # would yield a non-empty line (matches the OpenAI/Groq parse path — @CodeX).
            return []
        return [Word(text=text, start_ms=0, end_ms=_wav_duration_ms(chunk_path))]


def register_all() -> None:
    register("asr", "groq", GroqASR)
    register("asr", "cloudflare", CloudflareASR)
    register("asr", "faster_whisper", FasterWhisperASR)
    register("asr", "openai", OpenAIASR)
