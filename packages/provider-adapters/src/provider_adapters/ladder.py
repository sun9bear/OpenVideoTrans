"""The $0 auto-ladder and the paid-provider set — the project's #1 red line.

CRITICAL (CLAUDE.md §1/§14, 不可改): paid external APIs are NEVER auto-selected.
``AUTO_LADDER`` contains ONLY $0 providers (keyless / local / recurring-free-tier);
everything metered, pay-as-you-go, one-time-credit, or whole-job-handoff lives in
``PAID_PROVIDERS`` and is opt-in only. Nothing here loops/batches a paid call.
"""

from __future__ import annotations

# AUTO ladder = $0 providers only. A provider is skipped at runtime when its
# ``available()`` is False (missing key/binary), so the first usable $0 provider
# wins.
#   - ASR is CLOUD-PREFERRED per backlog T1.2 acceptance ("阶梯云优先
#     groq→CF→(faster_whisper cli)") — the hosted worker prefers fast free cloud ASR
#     over slow local CPU whisper, which sits at the tail as the keyless fallback.
#   - TTS prefers PIPER (local, permissively licensed) as the default per backlog
#     T1.3b ("piper 默认") / AD-6; it is never the hosted default's fallback unless
#     unconfigured. The broad-coverage free edge_tts (40+ langs) precedes the narrow
#     Cloudflare MeloTTS (only 6 langs), so a non-MeloTTS target (e.g. pt-BR/de) isn't
#     blocked by CF being merely "available" (CodeX). Proper per-target-language provider
#     gating (and the edge_tts license lane) is T1.3f / T1.3b·g.
AUTO_LADDER: dict[str, list[str]] = {
    "asr": ["groq", "cloudflare", "faster_whisper"],
    "mt": ["cloudflare", "groq", "deepl", "ollama"],
    "tts": ["piper", "edge_tts", "cloudflare"],
}

# Providers that cost money (metered / pay-as-you-go / one-time credit) or hand the
# whole job to a paid SaaS. NEVER auto-selected; opt-in only. Some carry no adapter
# at all (string-only paid names, e.g. 'backend'/'deepgram') — select() still blocks
# them at the string level, before any factory build (plan §5 invariant ④/⑤).
PAID_PROVIDERS: frozenset[str] = frozenset(
    {
        "openai",  # ASR + MT, pay-as-you-go
        "deepgram",  # ASR, one-time credits (string-only: no adapter)
        "assemblyai",  # ASR, one-time credits (string-only: no adapter)
        "deepseek",  # MT, one-time tokens then metered
        "gemini",  # MT, free tier shrinking; treat as opt-in (string-only: no adapter)
        "elevenlabs",  # TTS, paid for real use / cloning
        "minimax",  # TTS + cloning, metered (string-only: no adapter)
        "backend",  # whole-job handoff to a paid SaaS (string-only: no adapter)
    }
)

# MT spoken-length budget hint (chars synthesizable per second). Mirrors the kernel's
# config default; duplicated here because provider-adapters has no import edge to
# autodub-core (the seam keeps T1.1 ∥ T1.2 — backlog DAG).
DEFAULT_CHARS_PER_SEC = 15.0


def is_paid_provider(name: str) -> bool:
    return name in PAID_PROVIDERS
