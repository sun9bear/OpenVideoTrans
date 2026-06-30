"""free_min_share light-slot reservation (M2-CLOSE PR-D, #26).

The worker runs up to ``worker_concurrency`` jobs at once and reserves ``light_slot_reserve`` slots
for LIGHT (subtitle_only) jobs, so a stream of HEAVY (dub) jobs can never starve a short subtitle
job — WITHOUT preemption (运行中不抢占; real preemption is Tier 2/3, plan §6/§8). A HEAVY job may
occupy at most ``heavy_budget`` slots; the remaining slots are LIGHT-only and idle when no subtitle
job is queued (a running job is never evicted to honor the reservation).

This module is PURE policy + a thread-safe counter — no I/O — so the reservation arithmetic is
verified deterministically, independent of the claim-loop threading (worker.py).
"""
from __future__ import annotations

import threading
from enum import Enum

# Weight classes, derived from output_mode ONLY (ungameable: the browser advisory_duration is a
# sort hint, never an admission decision). subtitle_only has no TTS = light; dub_only/both = heavy.
LIGHT = "light"
HEAVY = "heavy"


class ClaimPlan(Enum):
    """What the dispatcher may claim next given the current slot occupancy."""

    NONE = "none"  # the box is full — claim nothing, wait for a slot to free
    ANY = "any"  # a heavy slot is free — a heavy OR light job may be claimed
    LIGHT_ONLY = "light_only"  # only the reserved slots remain — claim a LIGHT job or nothing


def weight_class(output_mode: str) -> str:
    """LIGHT iff subtitle_only (no TTS); every other / unknown mode is HEAVY (fail-safe: an unknown
    mode must never consume a reserved light slot)."""
    return LIGHT if output_mode == "subtitle_only" else HEAVY


def heavy_budget(worker_concurrency: int, light_slot_reserve: int) -> int:
    """How many slots HEAVY (dub) jobs may occupy. The rest are reserved for LIGHT jobs.

    Clamped to >= 1: reserving EVERY slot (light_slot_reserve >= worker_concurrency — e.g. the
    documented 1-slot 2GB box with reserve 1) would otherwise wedge dub jobs forever. At least one
    slot must always admit a heavy job; on such a box the single slot is shared, fairness handled by
    the §8 comparator (subtitle outranks dub).
    """
    return max(worker_concurrency - light_slot_reserve, 1)


def plan_claim(
    running_total: int, running_heavy: int, worker_concurrency: int, light_slot_reserve: int
) -> ClaimPlan:
    """Decide what may be claimed next from the running counts + knobs (pure)."""
    if running_total >= worker_concurrency:
        return ClaimPlan.NONE
    if running_heavy < heavy_budget(worker_concurrency, light_slot_reserve):
        return ClaimPlan.ANY
    return ClaimPlan.LIGHT_ONLY


class SlotTracker:
    """Thread-safe occupancy counter around plan_claim, with a wait primitive the dispatcher uses
    to sleep until a slot frees. ``add`` is only ever called by the single dispatcher thread;
    ``remove`` by the pool worker threads on job completion (which wakes a waiting dispatcher)."""

    def __init__(self, worker_concurrency: int, light_slot_reserve: int) -> None:
        if worker_concurrency < 1:
            raise ValueError("worker_concurrency must be >= 1")
        if light_slot_reserve < 0:
            raise ValueError("light_slot_reserve must be >= 0")
        self._concurrency = worker_concurrency
        self._reserve = light_slot_reserve
        self._total = 0
        self._heavy = 0
        self._cond = threading.Condition()

    def add(self, weight: str) -> None:
        with self._cond:
            self._total += 1
            if weight == HEAVY:
                self._heavy += 1

    def remove(self, weight: str) -> None:
        with self._cond:
            self._total -= 1
            if weight == HEAVY:
                self._heavy -= 1
            self._cond.notify_all()

    def plan(self) -> ClaimPlan:
        with self._cond:
            return plan_claim(self._total, self._heavy, self._concurrency, self._reserve)

    def snapshot(self) -> tuple[int, int]:
        with self._cond:
            return (self._total, self._heavy)

    def wait_for_slot(self, timeout: float) -> None:
        """Block until a slot is free or `timeout` elapses (re-checks under the lock first, so a
        remove() that races the caller is never missed). The dispatcher re-evaluates plan()
        after."""
        with self._cond:
            if self._total < self._concurrency:
                return
            self._cond.wait(timeout)
