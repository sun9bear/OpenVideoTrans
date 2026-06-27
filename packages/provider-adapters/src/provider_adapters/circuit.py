"""FREE-POOL (#23) — circuit-breaker-aware free-provider routing (the PURE routing half).

The seam (backlog FREE-POOL, CodeX P1.3 + AD-13): the per-provider quota STATE lives in the
control-plane (D1/KV, shared across worker boxes); THIS module only consumes an immutable
``ProviderAvailability`` snapshot and returns a routing RESULT. It has NO import edge to the
control-plane, D1, KV, the worker, or the network (asserted structurally by
tests/test_freepool_seam.py) so the free-pool mechanism can never entangle the kernel or a
future BYOK/Tier-3 path.

Red line (§1/§14, 不可改): routing walks the $0 ``AUTO_LADDER`` only and skips any paid name
defensively, so it NEVER returns a paid provider — even under total free-pool exhaustion it
returns a FAILURE (``free_pool_exhausted``) rather than escalating to a paid API.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .ladder import AUTO_LADDER, is_paid_provider

# Routing-failure reason codes (stable strings the worker logs / branches on).
FREE_POOL_EXHAUSTED = "free_pool_exhausted"
NO_FREE_PROVIDER = "no_free_provider"


def _free_ladder_providers() -> frozenset[str]:
    """Union of every $0 provider reachable via AUTO_LADDER, paid names excluded defensively
    (the ladders are already free — this only hardens against a future bad edit)."""
    names: set[str] = set()
    for ladder in AUTO_LADDER.values():
        for name in ladder:
            if not is_paid_provider(name):
                names.add(name)
    return frozenset(names)


# The universe of free providers routing may ever return. Disjoint from PAID_PROVIDERS (red
# line; asserted in tests/test_circuit.py and test_redline_invariants.py).
FREE_LADDER_PROVIDERS: frozenset[str] = _free_ladder_providers()


@dataclass(frozen=True)
class ProviderAvailability:
    """Immutable snapshot of per-provider circuit-breaker state at instant ``now_ms``.

    ``exhausted_until`` maps a provider name to the UTC epoch-ms at which its free quota resets
    (typically set on a 429 / quota-exhausted response). A provider is circuit-broken while
    ``now_ms < reset``; a provider absent from the map is available. The control-plane builds
    this and already drops entries whose reset has passed, but ``is_exhausted`` re-checks
    ``now_ms`` so a slightly stale snapshot still auto-recovers AT the reset instant (inclusive)
    rather than staying broken."""

    now_ms: int
    exhausted_until: Mapping[str, int] = field(default_factory=dict)

    def is_exhausted(self, name: str) -> bool:
        reset = self.exhausted_until.get(name)
        return reset is not None and self.now_ms < reset


@dataclass(frozen=True)
class ProviderResult:
    """A free provider was routed for ``kind``."""

    provider: str
    kind: str


@dataclass(frozen=True)
class ProviderFailure:
    """No free provider could be routed for ``kind``.

    ``reason`` is FREE_POOL_EXHAUSTED (every candidate is circuit-broken right now) or
    NO_FREE_PROVIDER (the kind has no configured free provider at all). ``retry_at_ms`` is the
    earliest reset among the exhausted candidates (soonest recovery) when known."""

    kind: str
    reason: str
    retry_at_ms: int | None = None


def configured_free_providers(names: Iterable[str]) -> frozenset[str]:
    """Reduce a set of provider NAMES (e.g. ``WorkerCredentials.providers`` keys pulled by
    SECRETS) to the ones that are known FREE providers, dropping paid and unknown names.

    Secrets-safe by construction: it takes names only, never key values. This is the pure
    consumer the SECRETS->FREE-POOL deferral routed here — the worker feeds the result (unioned
    with locally-probed keyless providers) into ``route_free``'s ``configured`` at M2-CLOSE."""
    return frozenset(n for n in names if n in FREE_LADDER_PROVIDERS)


def route_free(
    kind: str,
    snapshot: ProviderAvailability,
    *,
    configured: Iterable[str] | None = None,
) -> ProviderResult | ProviderFailure:
    """Pick the first free provider for ``kind`` ('asr'|'mt'|'tts') that is on the $0 ladder,
    (optionally) configured, and NOT circuit-broken in ``snapshot``.

    Returns a ``ProviderResult``, or a ``ProviderFailure``:
      - FREE_POOL_EXHAUSTED when every otherwise-usable free provider is currently exhausted
        (429 不复撞: a broken provider is skipped; total exhaustion fails rather than escalates);
      - NO_FREE_PROVIDER when the kind has no free provider to consider (unknown kind, or none
        of ``configured`` serves this kind).

    ``configured`` (when given) restricts consideration to those provider names — so an
    unconfigured provider (no key pulled) is never routed to, and "all configured exhausted"
    is free_pool_exhausted even if some unconfigured free provider exists. When omitted, every
    ladder free provider is considered. A paid name is skipped defensively regardless (red line)."""
    ladder = AUTO_LADDER.get(kind)
    if not ladder:
        return ProviderFailure(kind=kind, reason=NO_FREE_PROVIDER)
    allowed = frozenset(configured) if configured is not None else None
    saw_candidate = False
    exhausted_resets: list[int] = []
    for name in ladder:
        if is_paid_provider(name):  # defensive: routing never returns paid (red line §1/§14)
            continue
        if allowed is not None and name not in allowed:
            continue
        saw_candidate = True
        if snapshot.is_exhausted(name):
            reset = snapshot.exhausted_until.get(name)
            if reset is not None:
                exhausted_resets.append(reset)
            continue
        return ProviderResult(provider=name, kind=kind)
    if not saw_candidate:
        return ProviderFailure(kind=kind, reason=NO_FREE_PROVIDER)
    return ProviderFailure(
        kind=kind,
        reason=FREE_POOL_EXHAUSTED,
        retry_at_ms=min(exhausted_resets) if exhausted_resets else None,
    )
