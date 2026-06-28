"""M2-CLOSE (PR-A): a free provider that returns 429 (or DeepL's 456) must raise the typed
``QuotaExhausted`` — a ``ProviderUnavailable`` subclass — so the worker's FREE-POOL routing can
circuit-break that provider (report exhausted + re-route, 429 不复撞) instead of treating it as a
hard failure. The adapter stays clock-free: it parses only the Retry-After *delta-seconds* hint and
the worker turns it into an absolute resetAt against its own clock.
"""
from __future__ import annotations

import sys

import pytest
from provider_adapters.base import (
    ProviderUnavailable,
    QuotaExhausted,
    parse_retry_after,
    raise_quota_if_429,
)


class _Resp:
    """Minimal stand-in for a requests.Response (status + headers + body)."""

    def __init__(
        self, status_code: int, *, headers: dict[str, str] | None = None, body: dict | None = None
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body or {}

    def json(self) -> dict:
        return self._body

    @property
    def text(self) -> str:
        return "body-excerpt"


# ── QuotaExhausted type ───────────────────────────────────────────────────────
def test_quota_exhausted_is_a_provider_unavailable() -> None:
    # Subclassing keeps every existing `except ProviderUnavailable` path treating it as unusable;
    # only the worker that wants to circuit-break catches QuotaExhausted specifically.
    exc = QuotaExhausted("groq", retry_after_sec=12.5)
    assert isinstance(exc, ProviderUnavailable)
    assert exc.provider == "groq"
    assert exc.retry_after_sec == 12.5


def test_quota_exhausted_message_carries_no_secret() -> None:
    # The message names the provider + status only — never a key, URL, or response body.
    exc = QuotaExhausted("cloudflare")
    assert "cloudflare" in str(exc)
    assert exc.retry_after_sec is None


# ── raise_quota_if_429 helper ─────────────────────────────────────────────────
def test_raise_quota_if_429_raises_on_429_with_retry_after() -> None:
    resp = _Resp(429, headers={"Retry-After": "120"})
    with pytest.raises(QuotaExhausted) as ei:
        raise_quota_if_429(resp, "groq", kind="asr")
    assert ei.value.provider == "groq"
    assert ei.value.kind == "asr"  # the worker re-routes only the failed stage
    assert ei.value.retry_after_sec == 120.0


def test_raise_quota_if_429_honours_provider_specific_extra_status() -> None:
    # DeepL Free signals quota exhaustion with HTTP 456, not 429.
    resp = _Resp(456)
    with pytest.raises(QuotaExhausted) as ei:
        raise_quota_if_429(resp, "deepl", extra=(456,))
    assert ei.value.provider == "deepl"
    # 456 without the extra is NOT a quota signal — left for the caller's >=400 branch.
    assert raise_quota_if_429(_Resp(456), "deepl") is None


def test_raise_quota_if_429_is_noop_on_other_statuses() -> None:
    # A 500/503/200 is not a quota signal: the helper returns None and the caller's existing
    # >=400 branch raises a plain ProviderUnavailable (a hard error, not a circuit-break).
    assert raise_quota_if_429(_Resp(500), "cloudflare") is None
    assert raise_quota_if_429(_Resp(200), "cloudflare") is None


# ── parse_retry_after ─────────────────────────────────────────────────────────
def test_parse_retry_after_delta_seconds() -> None:
    assert parse_retry_after(_Resp(429, headers={"Retry-After": "30"})) == 30.0
    assert parse_retry_after(_Resp(429, headers={"retry-after": "5"})) == 5.0  # case-insensitive


def test_parse_retry_after_absent_or_unparseable_is_none() -> None:
    assert parse_retry_after(_Resp(429)) is None  # no header
    # An HTTP-date form is not parsed here (the worker falls back to a default reset).
    assert (
        parse_retry_after(_Resp(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
        is None
    )
    assert parse_retry_after(_Resp(429, headers={"Retry-After": "-3"})) is None  # negative ignored


# ── integration: a real adapter HTTP path surfaces QuotaExhausted ─────────────
def test_cloudflare_asr_429_raises_quota_exhausted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    from pathlib import Path

    from provider_adapters.asr import CloudflareASR

    class _FakeRequests:
        @staticmethod
        def post(*_a: object, **_k: object) -> _Resp:
            return _Resp(429, headers={"Retry-After": "60"})

    monkeypatch.setitem(sys.modules, "requests", _FakeRequests)  # type: ignore[arg-type]
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    audio = Path(str(tmp_path)) / "a.wav"
    audio.write_bytes(b"\x00\x10")
    with pytest.raises(QuotaExhausted) as ei:
        CloudflareASR()._run_one(str(audio))
    assert ei.value.provider == "cloudflare"
    assert ei.value.kind == "asr"
    assert ei.value.retry_after_sec == 60.0
