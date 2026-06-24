"""T1.3f — language capabilities registry + layered fail-closed gating.

The registry (``CAPABILITIES``, BCP-47 keyed) records, per target locale, which output
layers Tier-1 can actually deliver:

  * ``mt_supported`` / ``subtitle_supported`` — a free MT path exists (so we can produce
    a translated subtitle), and
  * ``tts_supported`` + ``tts_models`` — a free TTS voice exists (so we can produce a dub).

A locale can support subtitles but NOT dubbing (no free TTS voice). ``assert_language_pair``
admits a job **by output_mode layer**: a ``subtitle_only`` job only needs the subtitle layer;
a ``dub_only`` / ``both`` job additionally needs a TTS model. Anything unsupported
**fails closed** with the schema's ``ErrorCode`` — ``unsupported_language_pair`` or
``no_tts_model_for_language`` — never a silent best-effort that ships a broken artifact.

This also resolves the T1.2 DeepL deferral: ``deepl_target_code`` maps a BCP-47 target to
DeepL's exact target code (PT-BR / EN-US kept, scripts folded, region stripped where DeepL
wants it), and fails closed for targets DeepL doesn't offer rather than 400-ing at call time.

No autodub-core import edge (T1.1 ∥ T1.2): the kernel/worker calls these functions; this
package owns the provider-derived capability facts.
"""

from __future__ import annotations

from ovt_schemas.contracts import ErrorCode, LanguageCapabilities, LanguageCapability

_DUB_MODES = ("dub_only", "both")
_SUBTITLE_MODES = ("subtitle_only", "both")
_OUTPUT_MODES = ("subtitle_only", "dub_only", "both")


class LanguageError(RuntimeError):
    """A language pair / capability gate failed closed. ``code`` is the schema ``ErrorCode``
    the worker surfaces to the product boundary (``unsupported_language_pair`` /
    ``no_tts_model_for_language``)."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code


def _cap(
    *,
    mt: bool = True,
    sub: bool = True,
    tts: bool = True,
    models: tuple[str, ...] = (),
    font: str = "Noto Sans",
    license_status: str = "free",
    tier: str = "standard",
    voice: str | None = None,
) -> LanguageCapability:
    return LanguageCapability(
        mt_supported=mt, subtitle_supported=sub, tts_supported=tts, tts_models=list(models),
        burn_font=font, license_status=license_status, quality_tier=tier, default_voice=voice,
    )


# Curated Tier-1 target locales. ``tts_models`` lists the FREE providers that cover the
# locale (piper local / edge_tts keyless / cloudflare MeloTTS — the latter only en/es/fr/zh/ja/ko).
# Esperanto is the deliberate subtitle-only case: LLM MT translates it, but no free TTS voice
# ships → a dub job for it fails closed with ``no_tts_model_for_language``.
CAPABILITIES: LanguageCapabilities = {
    "en": _cap(models=("piper", "edge_tts", "cloudflare"), tier="high",
               voice="en-US-AriaNeural"),
    "zh-Hans": _cap(models=("edge_tts", "cloudflare", "piper"), font="Noto Sans CJK SC",
                    tier="high", voice="zh-CN-XiaoxiaoNeural"),
    "es": _cap(models=("piper", "edge_tts", "cloudflare"), tier="high",
               voice="es-ES-ElviraNeural"),
    "fr": _cap(models=("piper", "edge_tts", "cloudflare"), tier="high",
               voice="fr-FR-DeniseNeural"),
    "de": _cap(models=("piper", "edge_tts"), voice="de-DE-KatjaNeural"),
    "ja": _cap(models=("edge_tts", "cloudflare", "piper"), font="Noto Sans CJK JP",
               voice="ja-JP-NanamiNeural"),
    "ko": _cap(models=("edge_tts", "cloudflare", "piper"), font="Noto Sans CJK KR",
               voice="ko-KR-SunHiNeural"),
    "pt-BR": _cap(models=("edge_tts", "piper"), voice="pt-BR-FranciscaNeural"),
    "ru": _cap(models=("edge_tts", "piper"), voice="ru-RU-SvetlanaNeural"),
    "it": _cap(models=("edge_tts", "piper"), voice="it-IT-ElsaNeural"),
    "hi": _cap(models=("edge_tts",), font="Noto Sans Devanagari", voice="hi-IN-SwaraNeural"),
    "ar": _cap(models=("edge_tts",), font="Noto Sans Arabic", tier="basic",
               voice="ar-EG-SalmaNeural"),
    "eo": _cap(tts=False, models=(), tier="basic"),  # subtitle-only: no free TTS voice
}


def normalize_locale(locale: str) -> str:
    """Best-effort BCP-47 canonicalisation: lowercase the language subtag, Title-case a
    4-letter script, UPPER-case a 2-letter region. ``zh-hans`` -> ``zh-Hans``,
    ``PT-br`` -> ``pt-BR``. Unparseable input is returned stripped."""
    parts = locale.strip().replace("_", "-").split("-")
    if not parts or not parts[0]:
        return locale.strip()
    out = [parts[0].lower()]
    for sub in parts[1:]:
        if len(sub) == 4 and sub.isalpha():
            out.append(sub.title())
        elif len(sub) == 2 and sub.isalpha():
            out.append(sub.upper())
        else:
            out.append(sub)
    return "-".join(out)


def get_capability(locale: str) -> LanguageCapability | None:
    """Capability for ``locale``: exact BCP-47 match first, then a base-language fallback
    (``pt`` -> ``pt-BR``, ``zh`` -> ``zh-Hans``) so a bare code still resolves. ``None`` when
    the locale is not in the Tier-1 registry at all."""
    norm = normalize_locale(locale)
    if norm in CAPABILITIES:
        return CAPABILITIES[norm]
    base = norm.split("-")[0]
    if base in CAPABILITIES:
        return CAPABILITIES[base]
    return next((cap for key, cap in CAPABILITIES.items() if key.split("-")[0] == base), None)


def assert_language_pair(
    source_lang: str | None, target_lang: str, output_mode: str
) -> LanguageCapability:
    """Layered, fail-closed admission for a job's language pair. Returns the target's
    capability on success; raises ``LanguageError`` (with the schema ErrorCode) otherwise.

    * no registry entry / no MT path        -> ``unsupported_language_pair``
    * subtitle layer requested, unsupported -> ``unsupported_language_pair``
    * dub layer requested, no TTS model      -> ``no_tts_model_for_language``
    """
    if output_mode not in _OUTPUT_MODES:
        raise ValueError(f"unknown output_mode {output_mode!r}")
    cap = get_capability(target_lang)
    if cap is None or not cap.mt_supported:
        raise LanguageError(
            "unsupported_language_pair",
            f"target language {target_lang!r} is not supported (no free MT path)",
        )
    if output_mode in _SUBTITLE_MODES and not cap.subtitle_supported:
        raise LanguageError(
            "unsupported_language_pair",
            f"subtitle output for {target_lang!r} is not supported",
        )
    if output_mode in _DUB_MODES and not (cap.tts_supported and cap.tts_models):
        raise LanguageError(
            "no_tts_model_for_language",
            f"no free TTS voice for {target_lang!r}; dubbing is unavailable "
            f"(subtitle output is still supported)",
        )
    return cap


def resolve_source_language(hint: str | None, detected: str | None = None) -> str:
    """Source-language backfill: the caller's hint wins, else the ASR-detected code, else
    ``"auto"`` (let a downstream auto-detect, or fail closed where an explicit source is
    required — e.g. Cloudflare m2m100)."""
    for cand in (hint, detected):
        if cand and cand.strip() and cand.strip().lower() != "auto":
            return cand.strip()
    return "auto"


# DeepL v2 target codes for the registry locales DeepL actually offers (per-provider "逐语 vet",
# resolving the T1.2 deferral). EN/PT keep their region; zh-Hans folds to ZH; region subtags are
# stripped where DeepL wants the bare code. Locales absent here (hi/ar/eo) fail closed.
_DEEPL_TARGETS: dict[str, str] = {
    "en": "EN-US", "zh-Hans": "ZH", "es": "ES", "fr": "FR", "de": "DE",
    "ja": "JA", "ko": "KO", "pt-BR": "PT-BR", "ru": "RU", "it": "IT",
}


def deepl_target_code(target_lang: str) -> str:
    """Map a BCP-47 target to DeepL's exact target code, failing closed for targets DeepL
    does not offer (rather than sending a code DeepL 400s on)."""
    norm = normalize_locale(target_lang)
    base = norm.split("-")[0]
    code = (
        _DEEPL_TARGETS.get(norm)
        or _DEEPL_TARGETS.get(base)
        or next((v for k, v in _DEEPL_TARGETS.items() if k.split("-")[0] == base), None)
    )
    if code is None:
        raise LanguageError(
            "unsupported_language_pair",
            f"DeepL does not offer target {target_lang!r}; use another MT provider",
        )
    return code
