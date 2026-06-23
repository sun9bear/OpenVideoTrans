# type: ignore
# ^ test-first against the provider-adapters paid-safety API that lands in T1.2;
#   remove this file-level ignore (and the xfail below) in T1.2 once the API exists.
"""Red-line paid-provider safety invariants (STEP0-C, test-first — plan §5).

Encodes the 5 invariants that lock the project's #1 red line: a paid API is never
auto-invoked and ``allow_paid`` is constant ``False`` (CLAUDE.md / plan §14, 不可改).

The real provider-adapters implementation (PAID_PROVIDERS / AUTO_LADDER /
``select()`` triple guard / ``PaidProviderBlocked``) lands in T1.2. Until then each
test imports that API inside its body, so the missing symbols raise ImportError and
the test is reported ``xfail`` — keeping the main CI green (CodeX P1.1). T1.2 makes
these pass and removes the xfail markers (xfail TODO count must reach 0).
"""
from __future__ import annotations

import provider_adapters  # package exists; paid-safety symbols arrive in T1.2
import pytest

_REQUIRED_API = (
    "PAID_PROVIDERS",
    "AUTO_LADDER",
    "REGISTRY",
    "is_paid_provider",
    "select",
    "PaidProviderBlocked",
)
# True once T1.2 adds the paid-safety API. Driving the xfail off symbol PRESENCE (not off
# catching ImportError) means that once the API exists, a real violation — e.g. select()
# importing a paid SDK and raising ModuleNotFoundError (an ImportError) before
# PaidProviderBlocked — FAILS the suite instead of being masked as XFAIL (CodeX P1).
_API_READY = all(hasattr(provider_adapters, _n) for _n in _REQUIRED_API)

# TODO(T1.2): remove this xfail once the API exists (xfail TODO count → 0). It is strict, so a
# passing invariant after the API lands xpasses → forces removal; while the API is absent the
# invariant imports raise ImportError → xfail keeps main green.
pytestmark = [
    pytest.mark.redline,
    pytest.mark.xfail(
        not _API_READY,
        reason="provider-adapters paid-safety API (PAID_PROVIDERS/select) lands in T1.2",
        strict=True,
    ),
]


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
