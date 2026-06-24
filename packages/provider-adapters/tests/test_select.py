"""Behavior tests for the selection machinery (T1.2, test-first).

These complement the frozen red-line invariants (test_redline_invariants.py): they
pin the resolver's *resolution* behavior — explicit vs auto, the availability-aware
two-pass auto walk, the ``.name`` alias, ``Resolver`` delegation, and the registry/
ladder shape — none of which the 5 paid-safety invariants exercise directly.

Availability is monkeypatched per provider class so the walk is deterministic without
real keys/binaries (CI has neither).
"""
from __future__ import annotations

import sys
from pathlib import Path

import provider_adapters
import pytest
from provider_adapters import (
    AUTO_LADDER,
    REGISTRY,
    PaidProviderBlocked,
    ProviderUnavailable,
    Resolver,
    is_paid_provider,
    list_providers,
    probe,
    select,
)
from provider_adapters.asr import CloudflareASR, FasterWhisperASR, GroqASR
from provider_adapters.base import ASRProvider
from provider_adapters.mt import CloudflareMT, DeepLMT
from provider_adapters.tts import CloudflareTTS, PiperTTS


def _avail(monkeypatch: pytest.MonkeyPatch, dotted: str, value: bool) -> None:
    """Force ``<class>.available()`` to a constant for one test."""
    monkeypatch.setattr(dotted, lambda self: value)  # noqa: ARG005 - self required by signature


# ── .name alias ──────────────────────────────────────────────────────────────
def test_name_aliases_info_name() -> None:
    prov = GroqASR()
    assert prov.name == prov.info.name == "groq"


# ── explicit selection ───────────────────────────────────────────────────────
def test_select_explicit_free_available_returns_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _avail(monkeypatch, "provider_adapters.asr.GroqASR.available", True)
    assert select("asr", "groq", allow_paid=False).name == "groq"


def test_select_explicit_free_unavailable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _avail(monkeypatch, "provider_adapters.asr.GroqASR.available", False)
    with pytest.raises(ProviderUnavailable):
        select("asr", "groq", allow_paid=False)


def test_select_unknown_provider_raises_unavailable() -> None:
    with pytest.raises(ProviderUnavailable):
        select("asr", "does_not_exist", allow_paid=False)


def test_select_explicit_paid_with_allow_returns_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # allow_paid=True opens the gate (proves the guard is correct). The kernel NEVER
    # passes True — only a future explicit, user-initiated BYOK path would.
    _avail(monkeypatch, "provider_adapters.asr.OpenAIASR.available", True)
    assert select("asr", "openai", allow_paid=True).name == "openai"


# ── auto (ladder) selection ──────────────────────────────────────────────────
def test_select_auto_prefers_available_free_over_head(monkeypatch: pytest.MonkeyPatch) -> None:
    # ASR ladder = [groq, cloudflare, faster_whisper]; head unavailable -> next available wins.
    _avail(monkeypatch, "provider_adapters.asr.GroqASR.available", False)
    _avail(monkeypatch, "provider_adapters.asr.CloudflareASR.available", True)
    assert select("asr", None, allow_paid=False).name == "cloudflare"


def test_select_auto_falls_back_to_head_when_none_avail(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing configured -> return the ladder's DEFAULT free provider (head), never paid.
    for cls in (GroqASR, CloudflareASR, FasterWhisperASR):
        _avail(monkeypatch, f"provider_adapters.asr.{cls.__name__}.available", False)
    chosen = select("asr", None, allow_paid=False)
    assert chosen.name == "groq"  # ladder head
    assert not is_paid_provider(chosen.name)


def test_select_auto_skips_paid_even_if_ladder_polluted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defensive guard ③: a paid name leaking into a ladder is skipped even when available.
    monkeypatch.setitem(provider_adapters.base.AUTO_LADDER, "asr", ["openai", "groq"])
    _avail(monkeypatch, "provider_adapters.asr.OpenAIASR.available", True)
    _avail(monkeypatch, "provider_adapters.asr.GroqASR.available", False)
    chosen = select("asr", None, allow_paid=False)
    assert chosen.name == "groq"  # paid 'openai' skipped despite being available
    assert not is_paid_provider(chosen.name)


# ── Resolver delegation (kernel injection seam) ──────────────────────────────
def test_resolver_delegates_to_select(monkeypatch: pytest.MonkeyPatch) -> None:
    _avail(monkeypatch, "provider_adapters.asr.GroqASR.available", True)
    assert Resolver().select("asr", "groq", allow_paid=False).name == "groq"


# ── registry / ladder shape ──────────────────────────────────────────────────
def test_registry_flattens_all_adapter_names() -> None:
    assert set(REGISTRY) == {
        "groq", "cloudflare", "faster_whisper", "openai",  # asr (+ shared)
        "deepl", "ollama", "deepseek",  # mt
        "edge_tts", "piper", "elevenlabs",  # tts
    }
    assert REGISTRY["openai"].paid and REGISTRY["deepseek"].paid and REGISTRY["elevenlabs"].paid
    assert not (REGISTRY["groq"].paid or REGISTRY["cloudflare"].paid or REGISTRY["edge_tts"].paid)


def test_asr_ladder_is_cloud_first() -> None:
    # backlog T1.2 acceptance: "阶梯云优先 groq→CF→(faster_whisper cli)".
    assert AUTO_LADDER["asr"] == ["groq", "cloudflare", "faster_whisper"]


def test_probe_and_list_providers() -> None:
    assert set(list_providers("tts")) == {"edge_tts", "cloudflare", "piper", "elevenlabs"}
    rows = probe("asr")
    names = {name for name, _avail_flag, _info in rows}
    assert {"groq", "cloudflare", "faster_whisper", "openai"} <= names
    # probe never raises even when a provider's availability check can't run (no keys).
    assert all(isinstance(avail_flag, bool) for _name, avail_flag, _info in rows)


def test_select_blocks_paid_provider_message_mentions_safety() -> None:
    # The block is loud and explains the rule (not a silent skip).
    with pytest.raises(PaidProviderBlocked, match="never invoked automatically"):
        select("mt", "deepseek", allow_paid=False)


# ── CodeX review fixes (regression guards) ───────────────────────────────────
def test_tts_ladder_prefers_piper_then_broad_fallback() -> None:
    # CodeX: piper is the default TTS (T1.3b "piper 默认"); the broad-coverage edge_tts
    # precedes the narrow 6-lang Cloudflare MeloTTS so it isn't blocked for other langs.
    tts = AUTO_LADDER["tts"]
    assert tts[0] == "piper"
    assert tts.index("edge_tts") < tts.index("cloudflare")


def test_deepl_only_free_key_is_auto_available(monkeypatch: pytest.MonkeyPatch) -> None:
    # RED LINE (CodeX P1): a DeepL FREE key (':fx' -> api-free.deepl.com) is auto-usable;
    # a Pro key (paid, api.deepl.com) reports unavailable so select(auto) never bills it.
    monkeypatch.setattr("provider_adapters.mt.has_module", lambda _name: True)
    monkeypatch.setenv("DEEPL_API_KEY", "00000000-0000-0000-0000-000000000000:fx")
    assert DeepLMT().available() is True
    monkeypatch.setenv("DEEPL_API_KEY", "00000000-0000-0000-0000-000000000000")  # Pro key
    assert DeepLMT().available() is False


def test_unconfigured_default_raises_clean_setup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # CodeX P2: the auto fallback hands back the (free) ladder head even when nothing is
    # configured; calling it must raise a clean ProviderUnavailable with the setup hint,
    # not a raw ImportError / FileNotFoundError / HTTP deeper down.
    for cls in (GroqASR, CloudflareASR, FasterWhisperASR):
        _avail(monkeypatch, f"provider_adapters.asr.{cls.__name__}.available", False)
    asr = select("asr", None, allow_paid=False)
    assert isinstance(asr, ASRProvider)  # auto fallback returns a usable ASR provider type
    with pytest.raises(ProviderUnavailable, match="not configured"):
        asr.transcribe("nonexistent.wav", None)


def test_cf_tts_has_builtin_melotts_voices(monkeypatch: pytest.MonkeyPatch) -> None:
    # CodeX round-2 P2: CF MeloTTS must be usable without assets/voices.json — built-in
    # lang voices for the 6 supported languages; unsupported langs still raise.
    _avail(monkeypatch, "provider_adapters.tts.CloudflareTTS.available", True)
    assert CloudflareTTS().voices_for("zh-Hans") == ["zh"]
    assert CloudflareTTS().voices_for("ja") == ["jp"]  # ja -> MeloTTS 'jp'
    with pytest.raises(ProviderUnavailable):
        CloudflareTTS().voices_for("pt-BR")  # unsupported by MeloTTS


def test_cf_mt_auto_source_fails_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    # CodeX round-2 P1: m2m100 needs an explicit source; "auto" (no hint + CF ASR) must
    # fail clean (clear error), not hit the API and 400. Detection backfill is T1.3f.
    _avail(monkeypatch, "provider_adapters.mt.CloudflareMT.available", True)
    with pytest.raises(ProviderUnavailable, match="explicit source language"):
        CloudflareMT().translate(["hello"], "auto", "en")


def test_piper_unavailable_when_model_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # CodeX round-3 P2: piper is the TTS ladder head; a stale/missing FVD_PIPER_MODEL must
    # report unavailable so the ladder falls through to cloudflare/edge_tts (not fail in
    # synthesize() after being selected).
    monkeypatch.setattr("provider_adapters.tts.has_binary", lambda _name: True)
    monkeypatch.setenv("FVD_PIPER_MODEL", str(tmp_path / "missing.onnx"))
    assert PiperTTS().available() is False
    real = tmp_path / "voice.onnx"
    real.write_bytes(b"\x00")
    monkeypatch.setenv("FVD_PIPER_MODEL", str(real))
    assert PiperTTS().available() is True


def test_asr_normalizes_detected_language_name_to_iso() -> None:
    # CodeX round-4 P2: Whisper (OpenAI/groq) returns a language NAME ("english"); the
    # transcript must store an ISO code so the default ASR->CloudflareMT handoff doesn't
    # 400. The caller's hint wins when present; an unmapped language falls back to "auto".
    asr = GroqASR()
    assert asr._parse({"language": "english"}, None).source_language == "en"
    assert asr._parse({"language": "portuguese"}, "pt-BR").source_language == "pt"  # hint wins
    assert asr._parse({"language": "klingon"}, None).source_language == "auto"  # unmapped


def test_empty_asr_output_yields_no_lines() -> None:
    # CodeX round-5 P2: a no-speech result (no words, no text) must yield NO transcript
    # lines so the kernel's translate() skips MT, not a blank line that still triggers it.
    asr = GroqASR()
    assert asr._parse({"language": "english", "segments": [], "words": []}, None).lines == []
    one = asr._parse({"language": "english", "text": "hello"}, None)  # text but no timings
    assert len(one.lines) == 1 and one.lines[0].source_text == "hello"


def test_cf_asr_no_speech_yields_no_words(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # @CodeX bot P2: CloudflareASR must not fabricate a blank Word for no-speech (no words +
    # empty text) — _run_one returns [] so the transcript has no lines (kernel skips MT).
    class _Resp:
        status_code = 200

        def json(self) -> dict:
            return {"result": {"words": [], "text": "  "}}

    class _FakeRequests:
        @staticmethod
        def post(*_a: object, **_k: object) -> _Resp:
            return _Resp()

    monkeypatch.setitem(sys.modules, "requests", _FakeRequests)  # type: ignore[arg-type]
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    audio = tmp_path / "silence.wav"
    audio.write_bytes(b"\x00\x10")
    assert CloudflareASR()._run_one(str(audio)) == []
