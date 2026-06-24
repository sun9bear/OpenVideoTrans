"""T1.3f — language capabilities: layered (by output_mode) admission and fail-closed
gating, plus the DeepL per-target-code vet. Test-first (backlog T1.3f acceptance:
分层准入 + fail-closed).
"""
from __future__ import annotations

import pytest
from provider_adapters import (
    LanguageError,
    assert_language_pair,
    deepl_target_code,
    get_capability,
    resolve_source_language,
)
from provider_adapters.languages import normalize_locale


# ── BCP-47 normalization + base-language fallback ────────────────────────────
def test_normalize_locale_canonicalizes_script_and_region() -> None:
    assert normalize_locale("zh-hans") == "zh-Hans"
    assert normalize_locale("PT-br") == "pt-BR"
    assert normalize_locale("EN") == "en"


def test_get_capability_exact_then_base_fallback() -> None:
    assert get_capability("zh-Hans") is not None
    assert get_capability("pt") is get_capability("pt-BR")  # bare base resolves to the region entry
    assert get_capability("zh") is get_capability("zh-Hans")
    assert get_capability("tlh") is None  # Klingon: not in the Tier-1 registry


def test_get_capability_fails_closed_on_variant_mismatch() -> None:
    # CodeX-review P2: a specific sibling variant must NOT silently collapse to the wrong
    # curated entry (the worst case zh-Hant -> Simplified ships semantically wrong characters).
    assert get_capability("zh-Hant") is None  # NOT zh-Hans
    assert get_capability("zh-TW") is None
    assert get_capability("pt-PT") is None  # NOT pt-BR
    # A region variant of a GENERIC base entry ("en") may still fold to it (English -> English).
    assert get_capability("en-GB") is get_capability("en")
    assert get_capability("en-US") is get_capability("en")


# ── layered admission: subtitle layer vs dub layer ───────────────────────────
def test_fully_capable_language_passes_every_output_mode() -> None:
    for mode in ("subtitle_only", "dub_only", "both"):
        cap = assert_language_pair("en", "es", mode)
        assert cap.tts_supported and cap.tts_models


def test_subtitle_only_language_passes_subtitle_fails_dub() -> None:
    # Esperanto: MT translates it (subtitle ok) but no free TTS voice -> dub fails closed.
    assert assert_language_pair("en", "eo", "subtitle_only").subtitle_supported
    for dub_mode in ("dub_only", "both"):
        with pytest.raises(LanguageError) as ei:
            assert_language_pair("en", "eo", dub_mode)
        assert ei.value.code == "no_tts_model_for_language"


# ── fail-closed: unknown target ──────────────────────────────────────────────
def test_unknown_target_fails_unsupported_language_pair() -> None:
    for mode in ("subtitle_only", "dub_only", "both"):
        with pytest.raises(LanguageError) as ei:
            assert_language_pair("en", "tlh", mode)  # Klingon
        assert ei.value.code == "unsupported_language_pair"


def test_assert_rejects_unknown_output_mode() -> None:
    with pytest.raises(ValueError, match="output_mode"):
        assert_language_pair("en", "es", "telepathy")


# ── source-language backfill ─────────────────────────────────────────────────
def test_resolve_source_language_hint_wins_then_detected_then_auto() -> None:
    assert resolve_source_language("pt-BR", "en") == "pt-BR"  # caller hint wins
    assert resolve_source_language(None, "en") == "en"  # else the ASR-detected code
    assert resolve_source_language("auto", "auto") == "auto"  # neither -> auto
    assert resolve_source_language("", None) == "auto"


# ── DeepL per-target-code vet (resolves the T1.2 deferral) ───────────────────
def test_deepl_target_code_maps_region_and_script() -> None:
    assert deepl_target_code("en") == "EN-US"
    assert deepl_target_code("pt-BR") == "PT-BR"  # region kept
    assert deepl_target_code("zh-Hans") == "ZH"  # script folded
    assert deepl_target_code("zh") == "ZH"  # bare base resolves
    assert deepl_target_code("ja") == "JA"


def test_deepl_target_code_fails_closed_for_unsupported() -> None:
    # hi/ar are in the capability registry (MT via LLM) but DeepL doesn't offer them: the
    # per-provider vet fails closed rather than 400-ing at call time.
    for target in ("hi", "ar", "eo"):
        with pytest.raises(LanguageError) as ei:
            deepl_target_code(target)
        assert ei.value.code == "unsupported_language_pair"


def test_deepl_target_code_fails_closed_on_variant_mismatch() -> None:
    # CodeX-review P2: pt-PT must NOT mis-map to DeepL PT-BR, nor zh-Hant to ZH (Simplified).
    for target in ("pt-PT", "zh-Hant", "zh-TW"):
        with pytest.raises(LanguageError):
            deepl_target_code(target)
    assert deepl_target_code("en-GB") == "EN-US"  # region variant of generic "en" still folds
