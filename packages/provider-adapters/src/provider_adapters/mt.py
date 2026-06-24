"""Translation providers. Auto-ladder: cloudflare(m2m100) -> groq(llama) -> deepl
-> ollama (all $0). Paid LLM MT (openai/deepseek) is opt-in only.

LLM-backed providers translate in length-aware batches (each line gets a spoken
time budget so the dub fits its slot). Dedicated MT engines (m2m100, DeepL)
translate line-by-line / batched without budget control; the align stage then
handles fit.
"""

from __future__ import annotations

import json
import re

from ._env import env, require_env
from .base import MTProvider, ProviderInfo, ProviderUnavailable, has_module, register
from .ladder import DEFAULT_CHARS_PER_SEC

_BATCH = 40


def _budget_chars(ms: int) -> int:
    return max(8, int(DEFAULT_CHARS_PER_SEC * (ms / 1000.0)))


def _extract_json_array(text: str) -> list | None:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        val = json.loads(text)
    except Exception:  # noqa: BLE001 - tolerate non-JSON model output; fall back to regex
        m = re.search(r"\[.*\]", text, flags=re.DOTALL)
        if not m:
            return None
        try:
            val = json.loads(m.group(0))
        except Exception:  # noqa: BLE001 - still not parseable -> caller does 1-by-1 fallback
            return None
    if isinstance(val, dict):
        if "translations" in val:
            val = val["translations"]
        else:  # tolerate other wrapper keys (result/output/...): take first list value
            val = next((v for v in val.values() if isinstance(v, list)), None)
    return val if isinstance(val, list) else None


# --------------------------------------------------------------------------- #
class CloudflareMT(MTProvider):
    info = ProviderInfo(
        "cloudflare", "mt", paid=False,
        requires="CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN",
        languages="~100 (m2m100 subset)", notes="dedicated MT, free neurons",
    )

    def available(self) -> bool:
        return bool(
            has_module("requests")
            and env("CLOUDFLARE_ACCOUNT_ID")
            and env("CLOUDFLARE_API_TOKEN")
        )

    def translate(
        self, texts: list[str], source_lang: str, target_lang: str,
        budgets_ms: list[int] | None = None,
    ) -> list[str]:
        self._ensure_available()
        # m2m100 expects bare ISO-639-1 codes; strip any region/script subtag (zh-Hans -> zh).
        src, tgt = source_lang.split("-")[0].lower(), target_lang.split("-")[0].lower()
        if src in ("", "auto"):
            # m2m100 is many-to-many and needs an explicit source; "auto" (e.g. a CF ASR run
            # with no source hint) would 400. Fail clean — source-lang detection backfill /
            # fail-closed is T1.3f.
            raise ProviderUnavailable(
                f"Cloudflare m2m100 MT needs an explicit source language (got {source_lang!r}); "
                f"pass a source-lang hint. (Auto-detection backfill is T1.3f.)"
            )
        import requests

        acct, token = require_env("CLOUDFLARE_ACCOUNT_ID"), require_env("CLOUDFLARE_API_TOKEN")
        model = env("FVD_CF_MT_MODEL", "@cf/meta/m2m100-1.2b")
        url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{model}"
        out: list[str] = []
        for t in texts:
            if not t.strip():
                out.append("")
                continue
            resp = requests.post(
                url, headers={"Authorization": f"Bearer {token}"},
                json={"text": t, "source_lang": src, "target_lang": tgt}, timeout=120,
            )
            if resp.status_code >= 400:
                raise ProviderUnavailable(
                    f"cloudflare MT HTTP {resp.status_code}: {resp.text[:300]}"
                )
            out.append(str(resp.json().get("result", {}).get("translated_text", "")).strip())
        return out


# --------------------------------------------------------------------------- #
class DeepLMT(MTProvider):
    info = ProviderInfo(
        "deepl", "mt", paid=False,
        requires="DEEPL_API_KEY (FREE key — ends ':fx'; 500k chars/mo)",
        languages="~30", notes="highest free MT quality; Pro keys are paid, never auto-used",
    )

    def available(self) -> bool:
        # RED LINE (§1): only a DeepL FREE key (':fx' suffix -> api-free.deepl.com) is
        # auto-usable. A Pro key (no ':fx') bills via api.deepl.com, so report it
        # UNAVAILABLE here and select(auto) skips it — a paid endpoint is never
        # auto-invoked even though DeepL's free tier is genuinely $0 (CodeX P1).
        key = env("DEEPL_API_KEY")
        return has_module("requests") and key is not None and key.endswith(":fx")

    def translate(
        self, texts: list[str], source_lang: str, target_lang: str,
        budgets_ms: list[int] | None = None,
    ) -> list[str]:
        self._ensure_available()  # only a FREE ':fx' key passes — never the paid endpoint
        # TODO(T1.3f): map BCP-47 targets to DeepL's exact target codes (per-provider "逐语
        # vet"). DeepL accepts e.g. PT-BR/EN-US but NOT script subtags (zh-Hans) or JA-JP, so
        # those 400 here (loud, not silent). A naive base-strip would regress PT-BR/EN-US, so
        # the correct per-provider mapping is deferred to the language_capabilities unit.
        import requests

        key = require_env("DEEPL_API_KEY")
        host = "https://api-free.deepl.com"  # FREE endpoint only (§1); Pro is gated out above
        out: list[str] = []
        for i in range(0, len(texts), 50):
            batch = texts[i:i + 50]
            resp = requests.post(
                f"{host}/v2/translate",
                headers={"Authorization": f"DeepL-Auth-Key {key}"},
                data=[("text", t) for t in batch] + [("target_lang", target_lang.upper())],
                timeout=120,
            )
            if resp.status_code >= 400:
                raise ProviderUnavailable(f"deepl HTTP {resp.status_code}: {resp.text[:300]}")
            out.extend(tr.get("text", "") for tr in resp.json().get("translations", []))
        return out


# --------------------------------------------------------------------------- #
class _LLMTranslator(MTProvider):
    """Length-aware batch translation over any chat backend."""

    def _system(self, source_lang: str, target_lang: str) -> str:
        return (
            f"You are a professional dubbing translator. Translate each numbered source line "
            f"from {source_lang} into {target_lang}. Each line must stay concise enough to be "
            f"spoken aloud within its char_budget (a hint, not a hard cap). Preserve meaning and "
            f"tone. Return ONLY a JSON array of translated strings, one per input line, "
            f"same order, no notes, no keys."
        )

    def _payload(self, batch: list[str], budgets: list[int]) -> str:
        return json.dumps(
            [
                {"i": i, "text": t, "char_budget": _budget_chars(b)}
                for i, (t, b) in enumerate(zip(batch, budgets, strict=False))
            ],
            ensure_ascii=False,
        )

    def _chat(self, system: str, user: str) -> str:  # pragma: no cover - network
        raise NotImplementedError

    def translate(
        self, texts: list[str], source_lang: str, target_lang: str,
        budgets_ms: list[int] | None = None,
    ) -> list[str]:
        self._ensure_available()
        budgets = budgets_ms or [4000] * len(texts)
        out: list[str] = []
        sysmsg = self._system(source_lang, target_lang)
        for i in range(0, len(texts), _BATCH):
            batch, budg = texts[i:i + _BATCH], budgets[i:i + _BATCH]
            content = self._chat(sysmsg, self._payload(batch, budg))
            arr = _extract_json_array(content)
            if arr is None or len(arr) != len(batch):
                # robust fallback: translate this batch one line at a time
                for t, b in zip(batch, budg, strict=False):
                    one = self._chat(sysmsg, self._payload([t], [b]))
                    a = _extract_json_array(one)
                    out.append(str(a[0]) if a else t)
            else:
                out.extend(str(x) for x in arr)
        return out


class _OpenAICompatMT(_LLMTranslator):
    base_url = ""
    key_env = ""
    model_env = ""
    default_model = ""

    def available(self) -> bool:
        return has_module("requests") and env(self.key_env) is not None

    def _chat(self, system: str, user: str) -> str:
        import requests

        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {require_env(self.key_env)}",
                "Content-Type": "application/json",
            },
            json={
                "model": env(self.model_env, self.default_model), "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=180,
        )
        if resp.status_code >= 400:
            raise ProviderUnavailable(
                f"{self.info.name} MT HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()["choices"][0]["message"]["content"]


class GroqMT(_OpenAICompatMT):
    info = ProviderInfo(
        "groq", "mt", paid=False, requires="GROQ_API_KEY (free tier)",
        languages="multilingual (LLM)", notes="fast, prompt-controlled, free",
    )
    base_url = "https://api.groq.com/openai/v1"
    key_env = "GROQ_API_KEY"
    model_env = "FVD_GROQ_MT_MODEL"
    default_model = "llama-3.3-70b-versatile"


class OpenAIMT(_OpenAICompatMT):
    info = ProviderInfo(
        "openai", "mt", paid=True, requires="OPENAI_API_KEY (paid)",
        languages="multilingual", notes="PAID — opt-in only",
    )
    base_url = "https://api.openai.com/v1"
    key_env = "OPENAI_API_KEY"
    model_env = "FVD_OPENAI_MT_MODEL"
    default_model = "gpt-4o-mini"


class DeepSeekMT(_OpenAICompatMT):
    info = ProviderInfo(
        "deepseek", "mt", paid=True,
        requires="DEEPSEEK_API_KEY (metered after free tokens)",
        languages="multilingual", notes="PAID — opt-in only; cheap",
    )
    base_url = "https://api.deepseek.com/v1"
    key_env = "DEEPSEEK_API_KEY"
    model_env = "FVD_DEEPSEEK_MT_MODEL"
    default_model = "deepseek-chat"


class OllamaMT(_LLMTranslator):
    info = ProviderInfo(
        "ollama", "mt", paid=False, requires="local Ollama server (OLLAMA_HOST)",
        languages="depends on model", notes="fully local/offline LLM translation",
    )

    def available(self) -> bool:
        if not has_module("requests"):
            return False
        import requests

        try:
            requests.get(f"{self._host()}/api/tags", timeout=2)
            return True
        except Exception:  # noqa: BLE001 - server down/unreachable -> just unavailable
            return False

    @staticmethod
    def _host() -> str:
        return (env("OLLAMA_HOST", "http://localhost:11434") or "http://localhost:11434").rstrip("/")

    def _chat(self, system: str, user: str) -> str:
        import requests

        resp = requests.post(
            f"{self._host()}/api/chat",
            json={
                "model": env("FVD_OLLAMA_MODEL", "qwen2.5:7b"), "stream": False, "format": "json",
                "messages": [
                    {
                        "role": "system",
                        "content": system + ' Wrap the array as {"translations": [...]}.',
                    },
                    {"role": "user", "content": user},
                ],
            },
            timeout=300,
        )
        if resp.status_code >= 400:
            raise ProviderUnavailable(f"ollama HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json().get("message", {}).get("content", "")


def register_all() -> None:
    register("mt", "cloudflare", CloudflareMT)
    register("mt", "groq", GroqMT)
    register("mt", "deepl", DeepLMT)
    register("mt", "ollama", OllamaMT)
    register("mt", "openai", OpenAIMT)
    register("mt", "deepseek", DeepSeekMT)
