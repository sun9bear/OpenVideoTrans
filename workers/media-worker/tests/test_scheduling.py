"""Pure free_min_share slot-reservation logic (M2-CLOSE PR-D, #26).

The heart of the light-slot reservation (plan §6 line 178 / §8 line 212): a worker runs at most
`worker_concurrency` jobs at once and reserves `light_slot_reserve` slots for LIGHT (subtitle_only)
jobs so a stream of HEAVY (dub) jobs can never starve a short subtitle job — WITHOUT preemption
(运行中不抢占, real preemption is Tier 2/3). These are pure functions + a thread-safe counter so the
reservation arithmetic is verified deterministically, independent of the claim-loop threading.
"""
from __future__ import annotations

import threading

from media_worker.scheduling import (
    HEAVY,
    LIGHT,
    ClaimPlan,
    SlotTracker,
    plan_claim,
    weight_class,
)


# --------------------------------------------------------------------------- #
# weight_class — output_mode -> light/heavy (ungameable: derived from the mode, NOT the browser
# advisory_duration hint). subtitle_only is light (no TTS); dub_only/both carry TTS = heavy.
# --------------------------------------------------------------------------- #
def test_subtitle_only_is_light() -> None:
    assert weight_class("subtitle_only") == LIGHT


def test_dub_and_both_are_heavy() -> None:
    assert weight_class("dub_only") == HEAVY
    assert weight_class("both") == HEAVY


def test_unknown_mode_is_heavy_fail_safe() -> None:
    # An unrecognized output_mode must NOT be treated as light — that would let it consume a
    # reserved light slot. Fail safe to heavy (the capped class), so only a known subtitle job
    # ever gets the reservation.
    assert weight_class("") == HEAVY
    assert weight_class("something_new") == HEAVY


# --------------------------------------------------------------------------- #
# plan_claim — given the running counts + knobs, decide what may be claimed next.
#   heavy_budget = max(worker_concurrency - light_slot_reserve, 1)   <- never zero (see below)
# --------------------------------------------------------------------------- #
def test_default_2_1_empty_allows_any() -> None:
    # concurrency=2, reserve=1 -> heavy_budget=1. Nothing running: a heavy OR light job is
    # admissible.
    assert plan_claim(0, 0, 2, 1) == ClaimPlan.ANY


def test_default_2_1_one_heavy_running_restricts_to_light() -> None:
    # One heavy job occupies the only heavy slot; the remaining (reserved) slot is LIGHT-only.
    assert plan_claim(1, 1, 2, 1) == ClaimPlan.LIGHT_ONLY


def test_default_2_1_one_light_running_still_allows_any() -> None:
    # A light job did NOT consume the heavy budget, so a heavy job may still take the heavy slot.
    assert plan_claim(1, 0, 2, 1) == ClaimPlan.ANY


def test_default_2_1_full_is_none() -> None:
    assert plan_claim(2, 1, 2, 1) == ClaimPlan.NONE
    assert plan_claim(2, 0, 2, 1) == ClaimPlan.NONE  # 2 light jobs also fill the box


def test_single_slot_box_still_admits_heavy() -> None:
    # The documented 2GB box: concurrency=1, reserve=1. heavy_budget MUST clamp to 1 (not 0) or a
    # dub job could never run and would wedge until the 24h TTL. One slot, shared via the
    # comparator.
    assert plan_claim(0, 0, 1, 1) == ClaimPlan.ANY
    assert plan_claim(1, 1, 1, 1) == ClaimPlan.NONE
    assert plan_claim(1, 0, 1, 1) == ClaimPlan.NONE


def test_reserve_clamped_so_one_heavy_always_runs() -> None:
    # Even reserve >= concurrency (reserve everything for light) must still let ONE heavy job
    # run, or all dub jobs wedge forever. heavy_budget = max(2-3, 1) = 1.
    assert plan_claim(0, 0, 2, 3) == ClaimPlan.ANY
    assert plan_claim(1, 1, 2, 3) == ClaimPlan.LIGHT_ONLY


def test_wider_box_3_1() -> None:
    # concurrency=3, reserve=1 -> heavy_budget=2: two heavy slots + one reserved light slot.
    assert plan_claim(0, 0, 3, 1) == ClaimPlan.ANY
    assert plan_claim(1, 1, 3, 1) == ClaimPlan.ANY  # 1 heavy < budget 2
    assert plan_claim(2, 2, 3, 1) == ClaimPlan.LIGHT_ONLY  # heavy budget full, reserved slot left
    assert plan_claim(2, 1, 3, 1) == ClaimPlan.ANY  # 1 heavy + 1 light: a heavy slot is still free
    assert plan_claim(3, 2, 3, 1) == ClaimPlan.NONE


# --------------------------------------------------------------------------- #
# SlotTracker — thread-safe accounting around plan_claim, with a wait primitive the dispatcher uses
# to sleep until a slot frees. add() is only ever called by the single dispatcher; remove() by the
# pool worker threads.
# --------------------------------------------------------------------------- #
def test_tracker_counts_and_plans() -> None:
    t = SlotTracker(worker_concurrency=2, light_slot_reserve=1)
    assert t.plan() == ClaimPlan.ANY
    assert t.snapshot() == (0, 0)
    t.add(HEAVY)
    assert t.snapshot() == (1, 1)
    assert t.plan() == ClaimPlan.LIGHT_ONLY  # heavy budget (1) full -> reserved slot is light-only
    t.add(LIGHT)
    assert t.snapshot() == (2, 1)
    assert t.plan() == ClaimPlan.NONE  # full
    t.remove(HEAVY)
    assert t.snapshot() == (1, 0)
    assert t.plan() == ClaimPlan.ANY  # a heavy slot freed


def test_tracker_remove_wakes_a_waiter() -> None:
    # The dispatcher blocks in wait_for_slot when full; a pool thread's remove() must wake it.
    t = SlotTracker(worker_concurrency=1, light_slot_reserve=1)
    t.add(HEAVY)
    assert t.plan() == ClaimPlan.NONE
    woke = threading.Event()

    def _waiter() -> None:
        t.wait_for_slot(timeout=2.0)
        woke.set()

    th = threading.Thread(target=_waiter)
    th.start()
    # The waiter is parked (box full). Freeing the slot must release it well within the timeout.
    t.remove(HEAVY)
    assert woke.wait(2.0)
    th.join(2.0)
    assert not th.is_alive()
    assert t.plan() == ClaimPlan.ANY


def test_tracker_wait_for_slot_returns_on_timeout_when_still_full() -> None:
    # If nothing frees, wait_for_slot returns (does not hang) so the dispatcher re-checks
    # stop_event.
    t = SlotTracker(worker_concurrency=1, light_slot_reserve=1)
    t.add(HEAVY)
    t.wait_for_slot(timeout=0.05)  # returns promptly despite the box staying full
    assert t.plan() == ClaimPlan.NONE
