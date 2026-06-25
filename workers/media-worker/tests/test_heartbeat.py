from __future__ import annotations

import threading
import time

from media_worker.worker import Heartbeat
from mw_fakes import FAST_CONFIG, FakeControlPlane


def test_heartbeat_renews_on_independent_timer() -> None:
    # The lease is renewed by a standalone timer thread, with no stage edges driving it.
    cp = FakeControlPlane(config=FAST_CONFIG)
    hb = Heartbeat(cp, "job_x", 7, interval_sec=0.02)
    hb.start()
    deadline = time.monotonic() + 3.0
    while cp.heartbeat_count < 3 and time.monotonic() < deadline:
        time.sleep(0.005)
    hb.stop()
    assert cp.heartbeat_count >= 3
    # Every renewal carries the winning claim_version (the lease is claim_version-gated).
    assert all(cv == 7 for (_jid, cv, _stage) in cp.heartbeats)


def test_heartbeat_stops_after_stop() -> None:
    cp = FakeControlPlane(config=FAST_CONFIG)
    hb = Heartbeat(cp, "job_x", 1, interval_sec=0.02)
    hb.start()
    deadline = time.monotonic() + 2.0
    while cp.heartbeat_count < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    hb.stop()
    settled = cp.heartbeat_count
    time.sleep(0.1)
    assert cp.heartbeat_count == settled  # no further ticks after stop()


def test_heartbeat_stops_at_hard_timeout() -> None:
    # Past job_hard_timeout_sec the heartbeat must STOP renewing (plan §6) so the lease lapses and
    # the sweeper can recover the job — a wedged stage can't extend the lease forever.
    cp = FakeControlPlane(config=FAST_CONFIG)
    stopped = threading.Event()
    ticks = iter([0.0, 0.0, 100.0])  # started, 1st check (< cap), 2nd check (>= cap -> stop)
    hb = Heartbeat(
        cp,
        "job_x",
        1,
        interval_sec=0.01,
        max_total_sec=50.0,
        clock=lambda: next(ticks, 100.0),
        on_lost=stopped.set,
    )
    hb.start()
    assert stopped.wait(2.0)  # the hard-cap deadline tripped
    hb.stop()
    assert cp.heartbeat_count == 1  # renewed once before the cap, then stopped


def test_heartbeat_stops_and_signals_on_stale_claim() -> None:
    cp = FakeControlPlane(config=FAST_CONFIG, stale=True)
    lost = threading.Event()
    hb = Heartbeat(cp, "job_x", 1, interval_sec=0.02, on_lost=lost.set)
    hb.start()
    assert lost.wait(2.0)  # a 409 stale_claim drives on_lost
    hb.stop()
    settled = cp.heartbeat_count
    time.sleep(0.1)
    assert cp.heartbeat_count == settled  # it stopped renewing instead of hammering
