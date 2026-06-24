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
from .base import ProviderInfo, ProviderUnavailable, TTSProvider, has_binary, has_module, register

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

    def voices_for(self, lang: str) -> list[str]:
        self._ensure_available()
        ids = _preset_ids("cloudflare", lang)
        if not ids:
            raise ProviderUnavailable(
                f"Cloudflare MeloTTS does not cover language {lang!r} (only en/es/fr/zh/ja/ko). "
                f"Use edge_tts for broader coverage."
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
        if resp.status_code >= 400:
            raise ProviderUnavailable(f"cloudflare TTS HTTP {resp.status_code}: {resp.text[:300]}")
        audio_b64 = resp.json().get("result", {}).get("audio", "")
        if not audio_b64:
            raise ProviderUnavailable("cloudflare TTS returned no audio")
        out = str(Path(out_path).with_suffix(".mp3"))
        Path(out).write_bytes(base64.b64decode(audio_b64))
        return out


# --------------------------------------------------------------------------- #
class PiperTTS(TTSProvider):
    info = ProviderInfo(
        "piper", "tts", paid=False,
        requires="piper binary + a .onnx voice model (FVD_PIPER_MODEL)",
        languages="35+ (per downloaded model)", notes="fully local/offline",
    )
    ext = "wav"

    def available(self) -> bool:
        return has_binary("piper") and env("FVD_PIPER_MODEL") is not None

    def voices_for(self, lang: str) -> list[str]:
        self._ensure_available()
        return [require_env("FVD_PIPER_MODEL")]

    def synthesize(self, text: str, voice_id: str, lang: str, out_path: str) -> str:
        out = str(Path(out_path).with_suffix(".wav"))
        proc = subprocess.run(
            ["piper", "--model", voice_id, "--output_file", out],
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
