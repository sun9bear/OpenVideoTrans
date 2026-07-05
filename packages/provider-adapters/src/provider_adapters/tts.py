"""TTS (dubbing voice) providers. Auto-ladder: edge_tts -> cloudflare(melotts) ->
piper (all $0). Paid TTS (elevenlabs) is opt-in only.

None of the free providers clone the original speaker: each assigns a fixed preset
neural voice per speaker (free tier = generic synthesized voice, by design). Voice
cloning is reached only via a local-GPU engine or the paid opt-in handoff.

The preset voice catalog (``assets/voices.json``) is optional here — it is populated
by the voice/language units (T1.3b piper-default / T1.3f language_capabilities); when
absent, ``_voice_catalog`` returns ``{}`` and edge_tts falls back to a default voice.
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

from ._env import env, require_env
from .base import (
    ProviderInfo,
    ProviderUnavailable,
    TTSProvider,
    _build,
    has_binary,
    has_module,
    probe,
    raise_quota_if_429,
    register,
)
from .languages import COMMERCIAL_SAFE_TTS

_VOICES_PATH = Path(__file__).resolve().parent / "assets" / "voices.json"


def _voice_catalog() -> dict:
    try:
        return json.loads(_VOICES_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - catalog is optional (lands in T1.3b/f); absent -> {}
        return {}


def _preset_ids(provider: str, lang: str) -> list[str]:
    cat = _voice_catalog().get(provider, {})
    entries = cat.get(lang) or cat.get(lang.split("-")[0]) or cat.get("_default") or []
    return [e["id"] for e in entries]


def tts_preset_voices(provider: str, lang: str) -> list[str]:
    """The CLOSED free-preset voice ids a TTS ``provider`` offers for ``lang`` — the SAME
    set the picker shows AND the set an explicit pin (``JobPlan.tts_voice``) is validated against.

    Open-core guardrail (plan §4): Tier 1 offers only preset voices; an ARBITRARY voice string or
    model path is a Tier 2/3 capability and must be rejected. This delegates to the provider's own
    ``voices_for`` (catalog + built-in presets), so it matches what synthesis accepts.
    Returns ``[]`` when the provider is unknown / unavailable / does not cover the locale, so a pin
    validated against it fails closed. Never raises (a membership source, not a synth path)."""
    try:
        engine = _build("tts", provider)
        return list(engine.voices_for(lang)) if isinstance(engine, TTSProvider) else []
    except Exception:  # noqa: BLE001 - unknown/unavailable/uncovered -> empty (pin fails closed)
        return []


def _voice_meta(catalog: dict, provider: str, lang: str, voice_id: str) -> dict | None:
    per_provider = catalog.get(provider, {})
    entries = per_provider.get(lang) or per_provider.get(lang.split("-")[0])
    for entry in entries or []:
        if entry.get("id") == voice_id:
            return entry
    return None


def list_tts_voices(target_lang: str) -> list[dict]:
    """Rich, picker-facing dub-voice options AVAILABLE for ``target_lang`` on THIS box. For each
    installed, non-paid TTS provider, its closed preset voices (``tts_preset_voices``) are annotated
    with catalog metadata (gender + human label from voices.json) plus flags:
      * ``commercial_safe`` — a default-dub-safe engine (piper / cloudflare);
      * ``experimental``    — edge_tts (non-commercial lane; user-explicit pick only, never auto).
    The capability manifest publishes this and the picker renders it — never a static list
    (each deployment's voices differ). A voice with no catalog entry still appears with
    gender 'unknown' + its id as the label, so an installed voice is never silently dropped."""
    catalog = _voice_catalog()
    options: list[dict] = []
    for name, avail, info in probe("tts"):
        if not avail or info.paid:
            continue
        for voice_id in tts_preset_voices(name, target_lang):
            meta = _voice_meta(catalog, name, target_lang, voice_id) or {}
            options.append({
                "provider": name,
                "voice_id": voice_id,
                "gender": meta.get("gender", "unknown"),
                "label": meta.get("label", voice_id),
                "commercial_safe": name in COMMERCIAL_SAFE_TTS,
                "experimental": name == "edge_tts",
            })
    return options


# --------------------------------------------------------------------------- #
class EdgeTTS(TTSProvider):
    info = ProviderInfo(
        "edge_tts", "tts", paid=False,
        requires="pip install edge-tts (keyless)",
        languages="40+ neural voices", notes="best free+keyless default",
    )
    ext = "mp3"

    def available(self) -> bool:
        return has_binary("edge-tts")

    def voices_for(self, lang: str) -> list[str]:
        self._ensure_available()
        return _preset_ids("edge_tts", lang) or ["en-US-AriaNeural"]

    def synthesize(self, text: str, voice_id: str, lang: str, out_path: str) -> str:
        out = Path(out_path).with_suffix(".mp3")
        out.parent.mkdir(parents=True, exist_ok=True)  # edge-tts errors opaquely if dir missing
        proc = subprocess.run(
            ["edge-tts", "--voice", voice_id, "--text", text, "--write-media", str(out)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not out.exists():
            raise ProviderUnavailable(f"edge-tts failed: {proc.stderr[-400:]}")
        return str(out)


# --------------------------------------------------------------------------- #
class CloudflareTTS(TTSProvider):
    info = ProviderInfo(
        "cloudflare", "tts", paid=False,
        requires="CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN",
        languages="en/es/fr/zh/ja/ko only", notes="MeloTTS; single voice per language",
    )
    ext = "mp3"

    def available(self) -> bool:
        return bool(
            has_module("requests")
            and env("CLOUDFLARE_ACCOUNT_ID")
            and env("CLOUDFLARE_API_TOKEN")
        )

    # MeloTTS uses the language code itself as its single "voice"; the CF binding accepts
    # en/es/fr/zh + jp/kr. Built-in so the ladder entry is usable WITHOUT the optional
    # assets/voices.json catalog (a catalog, when present, still overrides — T1.3b/f).
    _MELOTTS_LANGS = {"en": "en", "es": "es", "fr": "fr", "zh": "zh", "ja": "jp", "ko": "kr"}

    def voices_for(self, lang: str) -> list[str]:
        self._ensure_available()
        code = self._MELOTTS_LANGS.get(lang.split("-")[0].lower())
        ids = _preset_ids("cloudflare", lang) or ([code] if code else [])
        if not ids:
            raise ProviderUnavailable(
                f"Cloudflare MeloTTS does not cover language {lang!r} (only en/es/fr/zh/ja/ko). "
                f"Use piper/edge_tts for broader coverage."
            )
        return ids

    def synthesize(self, text: str, voice_id: str, lang: str, out_path: str) -> str:
        import requests

        acct, token = require_env("CLOUDFLARE_ACCOUNT_ID"), require_env("CLOUDFLARE_API_TOKEN")
        model = env("FVD_CF_TTS_MODEL", "@cf/myshell-ai/melotts")
        url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{model}"
        resp = requests.post(
            url, headers={"Authorization": f"Bearer {token}"},
            json={"prompt": text, "lang": voice_id}, timeout=120,
        )
        raise_quota_if_429(resp, "cloudflare", kind="tts")  # 429 -> circuit-break (FREE-POOL)
        if resp.status_code >= 400:
            raise ProviderUnavailable(f"cloudflare TTS HTTP {resp.status_code}: {resp.text[:300]}")
        audio_b64 = resp.json().get("result", {}).get("audio", "")
        if not audio_b64:
            raise ProviderUnavailable("cloudflare TTS returned no audio")
        out = str(Path(out_path).with_suffix(".mp3"))
        Path(out).write_bytes(base64.b64decode(audio_b64))
        return out


# --------------------------------------------------------------------------- #
def piper_model_language() -> str | None:
    """Base language subtag of the installed Piper voice, or None if it can't be determined.

    Piper's official voices follow ``<lang>_<REGION>-<voice>-<quality>.onnx`` (e.g.
    ``de_DE-thorsten-medium.onnx`` -> ``de``), so the language is the filename's leading subtag
    before ``_``. ``FVD_PIPER_LANG`` overrides for a non-standard model name. A name we can't parse
    a plausible language out of returns None — the caller then can't *prove* a mismatch and trusts
    the operator; the standard-named cross-language case (the actual reported bug) IS parseable."""
    override = env("FVD_PIPER_LANG")
    if override:
        return override.strip().replace("_", "-").split("-")[0].lower() or None
    model = env("FVD_PIPER_MODEL")
    if not model:
        return None
    head = Path(model).name.split("-")[0]  # "en_US-amy-medium.onnx" -> "en_US"
    if "_" not in head:  # Piper's convention is "<lang>_<REGION>"; no underscore -> non-standard
        return None
    lang = head.split("_")[0].strip().lower()
    return lang if lang.isalpha() and 2 <= len(lang) <= 3 else None


def _piper_voices_dir() -> str | None:
    """The multi-voice Piper models directory (``FVD_PIPER_VOICES_DIR``), or None for the legacy
    single-model deployment. When set to a real dir, PiperTTS serves EVERY installed catalog voice
    (P1: multiple M/F models per locale) instead of the one ``FVD_PIPER_MODEL``.

    ⚠️ P1b-bake BLOCKER (supply-chain, T1.3g): admission.py / doctor.py enforce the sha256 pin
    (verify_piper_model) by reading ONLY ``FVD_PIPER_MODEL`` — which is unset in multi-voice mode, so
    the integrity gate NO-OPS for the dir's models. Nothing sets this env yet (inert), but P1b-bake
    MUST, together, (a) add sha256 pins for the baked M/F models and (b) make admission/doctor verify
    EACH installed ``<VOICES_DIR>/*.onnx`` (fail-closed) before setting ``FVD_PIPER_VOICES_DIR``."""
    voices_dir = env("FVD_PIPER_VOICES_DIR")
    return voices_dir if voices_dir and Path(voices_dir).is_dir() else None


def _piper_installed(lang: str) -> list[str]:
    """Catalog Piper voice basenames covering ``lang`` whose ``<VOICES_DIR>/<basename>.onnx`` is
    actually installed on this box. Empty when there is no voices dir or none is installed — a
    availability is a FILE fact, so routing / the picker only ever offer an installed one."""
    voices_dir = _piper_voices_dir()
    if voices_dir is None:
        return []
    return [vid for vid in _preset_ids("piper", lang)
            if Path(voices_dir, f"{vid}.onnx").is_file()]


def _resolve_piper_model(voice_id: str) -> str:
    """Map a pinned voice id to a model path: in multi-voice mode a bare basename resolves to
    ``<VOICES_DIR>/<basename>.onnx``; otherwise the id is already a full model path (legacy
    ``FVD_PIPER_MODEL``). A basename with a path separator is never joined (defense-in-depth)."""
    voices_dir = _piper_voices_dir()
    if voices_dir is not None and "/" not in voice_id and "\\" not in voice_id:
        candidate = Path(voices_dir, f"{voice_id}.onnx")
        if candidate.is_file():
            return str(candidate)
    return voice_id


def piper_model_covers(lang: str) -> bool:
    """Whether Piper can serve ``lang`` on this box.

    Multi-voice (``FVD_PIPER_VOICES_DIR`` set): covered iff ≥1 installed catalog voice covers the
    locale. Legacy single-model: base-subtag match against the one model's language; True when the
    language is undeterminable (operator-trusted, non-standard name) — we never *block* a model we
    can't classify, only one we can prove is the wrong language."""
    if _piper_voices_dir() is not None:
        return bool(_piper_installed(lang))
    model_lang = piper_model_language()
    if model_lang is None:
        return True
    return model_lang == lang.strip().replace("_", "-").split("-")[0].lower()


class PiperTTS(TTSProvider):
    info = ProviderInfo(
        "piper", "tts", paid=False,
        requires="piper binary + a .onnx voice (FVD_PIPER_MODEL or FVD_PIPER_VOICES_DIR)",
        languages="35+ (per downloaded model)", notes="fully local/offline",
    )
    ext = "wav"

    def available(self) -> bool:
        # Require the binary AND ≥1 installed model — a stale/missing config must report unavailable
        # so the ladder falls through instead of selecting Piper and failing later in synthesize()
        # (CodeX). Multi: any *.onnx in FVD_PIPER_VOICES_DIR; legacy: the FVD_PIPER_MODEL file.
        if not has_binary("piper"):
            return False
        voices_dir = _piper_voices_dir()
        if voices_dir is not None:
            return any(Path(voices_dir).glob("*.onnx"))
        model = env("FVD_PIPER_MODEL")
        return model is not None and Path(model).is_file()

    def voices_for(self, lang: str) -> list[str]:
        self._ensure_available()
        # Multi-voice: return EVERY installed catalog voice covering the locale (the P1 picker's
        # M/F choices). Availability is a file fact, so an uninstalled entry is never offered.
        voices_dir = _piper_voices_dir()
        if voices_dir is not None:
            installed = _piper_installed(lang)
            if not installed:
                raise ProviderUnavailable(
                    f"no installed Piper voice covers {lang!r} in FVD_PIPER_VOICES_DIR "
                    f"({voices_dir!r}); use another provider or bake a {lang!r} voice."
                )
            return installed
        # Legacy single-model: piper has ONE configured model; fail closed on a language it can't
        # serve — a `de` job on an `en_US` model would silently synthesize German with an English
        # voice. Mirrors CloudflareTTS's per-language gate + routing defense-in-depth (@CodeX bot).
        if not piper_model_covers(lang):
            raise ProviderUnavailable(
                f"the installed Piper model (language {piper_model_language()!r}) does not cover "
                f"{lang!r}: piper synthesizes only its single configured model's language. Point "
                f"FVD_PIPER_MODEL at a {lang!r} voice (or FVD_PIPER_LANG for a non-standard name)."
            )
        return [require_env("FVD_PIPER_MODEL")]

    def synthesize(self, text: str, voice_id: str, lang: str, out_path: str) -> str:
        out = str(Path(out_path).with_suffix(".wav"))
        model = _resolve_piper_model(voice_id)  # basename -> <VOICES_DIR>/<name>.onnx, or a path
        proc = subprocess.run(
            ["piper", "--model", model, "--output_file", out],
            input=text, capture_output=True, text=True,
        )
        if proc.returncode != 0 or not Path(out).exists():
            raise ProviderUnavailable(f"piper failed: {proc.stderr[-400:]}")
        return out


# --------------------------------------------------------------------------- #
class ElevenLabsTTS(TTSProvider):
    info = ProviderInfo(
        "elevenlabs", "tts", paid=True,
        requires="ELEVENLABS_API_KEY (paid for real use)",
        languages="~29", notes="PAID — opt-in only; BYO key",
    )
    ext = "mp3"

    def available(self) -> bool:
        return has_module("requests") and env("ELEVENLABS_API_KEY") is not None

    def voices_for(self, lang: str) -> list[str]:
        self._ensure_available()
        custom = env("FVD_ELEVENLABS_VOICES")
        if custom:
            return [v.strip() for v in custom.split(",") if v.strip()]
        return _preset_ids("elevenlabs", lang) or _preset_ids("elevenlabs", "_default")

    def synthesize(self, text: str, voice_id: str, lang: str, out_path: str) -> str:
        import requests

        model = env("FVD_ELEVENLABS_MODEL", "eleven_multilingual_v2")
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": require_env("ELEVENLABS_API_KEY"),
                "Content-Type": "application/json",
            },
            json={"text": text, "model_id": model}, timeout=180,
        )
        if resp.status_code >= 400:
            raise ProviderUnavailable(f"elevenlabs HTTP {resp.status_code}: {resp.text[:300]}")
        out = str(Path(out_path).with_suffix(".mp3"))
        Path(out).write_bytes(resp.content)
        return out


def register_all() -> None:
    register("tts", "edge_tts", EdgeTTS)
    register("tts", "cloudflare", CloudflareTTS)
    register("tts", "piper", PiperTTS)
    register("tts", "elevenlabs", ElevenLabsTTS)
