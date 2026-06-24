"""Red-line paid-provider safety invariants (plan §5 — the project's #1 red line).

Encodes the 5 invariants that lock CLAUDE.md §1/§14 (不可改): a paid API is never
auto-invoked and ``allow_paid`` is constant ``False``. STEP0-C landed these as
test-first ``xfail`` because the provider-adapters paid-safety API did not exist yet
(keeping main CI green, CodeX P1.1). T1.2 implements that API, so the xfail / TODO /
type-ignore scaffolding is removed here and these are now hard, must-pass red-line
guards (acceptance: xfail TODO count → 0 — the guardrail must not park a permanent
待办).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.redline


def test_inv1_paid_flag_matches_paid_provider_set() -> None:
    """① Every provider's ``paid`` flag agrees with PAID_PROVIDERS membership."""
    from provider_adapters import PAID_PROVIDERS, REGISTRY, is_paid_provider

    for name, info in REGISTRY.items():
        assert info.paid == (name in PAID_PROVIDERS) == is_paid_provider(name)


def test_inv2_auto_ladder_all_free() -> None:
    """② Every provider reachable via AUTO_LADDER is free (never paid)."""
    from provider_adapters import AUTO_LADDER, is_paid_provider

    for kind, ladder in AUTO_LADDER.items():
        for name in ladder:
            assert not is_paid_provider(name), f"{kind} ladder contains paid provider {name}"


def test_inv3_select_auto_never_returns_paid() -> None:
    """③ ``select(kind, None, allow_paid=False)`` never resolves to a paid provider."""
    from provider_adapters import AUTO_LADDER, is_paid_provider, select

    for kind in AUTO_LADDER:
        chosen = select(kind, None, allow_paid=False)
        assert not is_paid_provider(chosen.name)


def test_inv4_explicit_paid_without_allow_raises() -> None:
    """④ Requesting ANY paid provider without ``allow_paid`` raises PaidProviderBlocked.

    Iterates all paid names (deterministic order) — the §5 invariant is about the paid
    gate firing string-level *before* kind/factory resolution, so the kind passed is
    irrelevant to the block.
    """
    from provider_adapters import PAID_PROVIDERS, PaidProviderBlocked, select

    for paid_name in sorted(PAID_PROVIDERS):
        with pytest.raises(PaidProviderBlocked):
            select("tts", paid_name, allow_paid=False)


def test_inv5_string_only_paid_name_blocked() -> None:
    """⑤ A string-only paid name (no factory) still raises under ``allow_paid=False``."""
    from provider_adapters import PaidProviderBlocked, select

    # 'backend' is a string-only paid name with no registered adapter (design-correct, plan §5).
    with pytest.raises(PaidProviderBlocked):
        select("tts", "backend", allow_paid=False)
