"""FREE-POOL (#23) — circuit-breaker-aware free-provider routing (pure side).

These pin the unit's acceptance on the provider-adapters (routing) half of the seam:
  - 429 后不反复撞已耗尽 provider  -> route_free skips a circuit-broken provider
  - 重置时间到自动恢复              -> a past-reset provider is available again (now-relative)
  - 全部免费 provider 耗尽 -> free_pool_exhausted (a ProviderFailure, never a paid escalation)
  - 轮换只在免费间、绝不转 PAID     -> route_free never returns a paid name (red line §1/§14)

route_free is PURE: it eats an immutable ProviderAvailability SNAPSHOT (whose state the
control-plane owns in D1/KV) and returns a routing RESULT. No I/O, no control-plane import
(asserted structurally in test_freepool_seam.py).
"""

from __future__ import annotations

import pytest

from provider_adapters import (
    FREE_LADDER_PROVIDERS,
    FREE_POOL_EXHAUSTED,
    NO_FREE_PROVIDER,
    PAID_PROVIDERS,
    ProviderAvailability,
    ProviderFailure,
    ProviderResult,
    configured_free_providers,
    route_free,
)

_NOW = 1_000_000


def _snap(now: int = _NOW, **exhausted: int) -> ProviderAvailability:
    return ProviderAvailability(now_ms=now, exhausted_until=dict(exhausted))


# --------------------------------------------------------------------------- #
# Happy path + circuit-breaker skip
# --------------------------------------------------------------------------- #
def test_route_free_returns_ladder_head_when_nothing_exhausted() -> None:
    # AUTO_LADDER["asr"] head is groq.
    out = route_free("asr", _snap())
    assert out == ProviderResult(provider="groq", kind="asr")


def test_429_does_not_rehit_exhausted_provider() -> None:
    # groq circuit-broken for another hour -> route to the next free ladder rung (cloudflare).
    out = route_free("asr", _snap(groq=_NOW + 3_600_000))
    assert out == ProviderResult(provider="cloudflare", kind="asr")


def test_skips_multiple_exhausted_in_ladder_order() -> None:
    out = route_free("asr", _snap(groq=_NOW + 1, cloudflare=_NOW + 1))
    assert out == ProviderResult(provider="faster_whisper", kind="asr")


# --------------------------------------------------------------------------- #
# Auto-recovery at the reset instant (now-relative; defensive even on a stale snapshot)
# --------------------------------------------------------------------------- #
def test_reset_time_in_the_past_auto_recovers() -> None:
    # A reset that already passed must NOT keep the provider broken — groq is usable again.
    out = route_free("asr", _snap(groq=_NOW - 1))
    assert out == ProviderResult(provider="groq", kind="asr")


def test_is_exhausted_boundary_is_inclusive_recovery() -> None:
    snap = _snap(groq=_NOW)  # reset == now
    assert snap.is_exhausted("groq") is False  # recovered exactly at the reset instant
    assert _snap(groq=_NOW + 1).is_exhausted("groq") is True
    assert snap.is_exhausted("cloudflare") is False  # absent -> never exhausted


# --------------------------------------------------------------------------- #
# Total free-pool exhaustion -> free_pool_exhausted (NEVER a paid escalation)
# --------------------------------------------------------------------------- #
def test_all_free_providers_exhausted_returns_free_pool_exhausted() -> None:
    out = route_free(
        "asr",
        _snap(groq=_NOW + 30_000, cloudflare=_NOW + 90_000, faster_whisper=_NOW + 10_000),
    )
    assert isinstance(out, ProviderFailure)
    assert out.kind == "asr"
    assert out.reason == FREE_POOL_EXHAUSTED
    # retry_at_ms is the EARLIEST reset among the exhausted providers (soonest recovery).
    assert out.retry_at_ms == _NOW + 10_000


@pytest.mark.redline
def test_route_free_never_returns_a_paid_provider() -> None:
    # Across every kind and an empty snapshot, a routed provider is always free — and even
    # under total exhaustion the result is a FAILURE, never a paid name (red line §1/§14).
    for kind in ("asr", "mt", "tts"):
        out = route_free(kind, _snap())
        assert isinstance(out, ProviderResult)
        assert out.provider not in PAID_PROVIDERS


# --------------------------------------------------------------------------- #
# configured-aware routing (the SECRETS->FREE-POOL seam: only configured free providers)
# --------------------------------------------------------------------------- #
def test_configured_limits_routing_to_configured_providers() -> None:
    # groq is on the ladder ahead of cloudflare and is NOT exhausted, but it is not configured
    # (no key pulled) -> route_free must skip it and pick the configured cloudflare.
    out = route_free("asr", _snap(), configured={"cloudflare"})
    assert out == ProviderResult(provider="cloudflare", kind="asr")


def test_all_configured_exhausted_is_free_pool_exhausted_even_if_unconfigured_free_exists() -> None:
    # Only groq is configured and it is down; cloudflare/faster_whisper exist on the ladder but
    # are not configured -> free_pool_exhausted (we never route to an unconfigured provider).
    out = route_free("asr", _snap(groq=_NOW + 5_000), configured={"groq"})
    assert isinstance(out, ProviderFailure)
    assert out.reason == FREE_POOL_EXHAUSTED
    assert out.retry_at_ms == _NOW + 5_000


def test_no_configured_free_provider_for_kind_is_no_free_provider() -> None:
    # deepl is mt-only; asking it to do asr leaves no configured free provider for the kind.
    out = route_free("asr", _snap(), configured={"deepl"})
    assert isinstance(out, ProviderFailure)
    assert out.reason == NO_FREE_PROVIDER


def test_unknown_kind_returns_no_free_provider() -> None:
    out = route_free("bogus", _snap())
    assert isinstance(out, ProviderFailure)
    assert out.reason == NO_FREE_PROVIDER


# --------------------------------------------------------------------------- #
# configured_free_providers — the pure WorkerCredentials.providers consumer (secrets-safe:
# it takes provider NAMES only, never secret values) used to build `configured`.
# --------------------------------------------------------------------------- #
def test_configured_free_providers_keeps_free_drops_paid_and_unknown() -> None:
    got = configured_free_providers(["groq", "openai", "deepl", "bogus", "cloudflare"])
    assert got == frozenset({"groq", "deepl", "cloudflare"})


def test_configured_free_providers_empty() -> None:
    assert configured_free_providers([]) == frozenset()


@pytest.mark.redline
def test_free_ladder_disjoint_from_paid() -> None:
    # The free routing universe and the paid set must never overlap (red line §1/§14).
    assert FREE_LADDER_PROVIDERS.isdisjoint(PAID_PROVIDERS)
    # configured_free_providers can NEVER surface a paid name, by construction.
    assert configured_free_providers(sorted(PAID_PROVIDERS)) == frozenset()
