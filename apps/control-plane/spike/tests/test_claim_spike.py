"""T2.0 — D1-claim concurrency invariants on local SQLite (the hard gate's autonomous half).
Test-first: no double-claim under contention, every job claimed, expired leases reclaimable,
``attempt`` capped. The real-D1 half runs in CI via the D1 REST runner (token injected by GH).
"""
from __future__ import annotations

from pathlib import Path

from spike.claim import claim_one, init_schema, seed_jobs
from spike.local_spike import connect, run_local_spike


def test_concurrent_claim_no_double_and_all_claimed(tmp_path: Path) -> None:
    # 20 consumers race 100 jobs (spec): each job claimed exactly once, all claimed.
    res = run_local_spike(str(tmp_path / "spike.db"), n_jobs=100, n_consumers=20)
    assert res.no_double_claim, f"double-claimed: {res.double_claimed}"
    assert res.all_claimed, f"only {res.distinct_claimed}/{res.n_jobs} claimed"
    assert len(res.claims) == 100  # exactly 100 total claims (no extras)


def test_repeated_runs_stay_exactly_once(tmp_path: Path) -> None:
    # Concurrency is nondeterministic — repeat to catch a flaky double-claim / lost job.
    for i in range(8):
        res = run_local_spike(str(tmp_path / f"r{i}.db"), n_jobs=60, n_consumers=16)
        assert res.no_double_claim and res.all_claimed, f"run {i}: {res.double_claimed}"


def test_expired_lease_is_reclaimable_and_attempt_increments(tmp_path: Path) -> None:
    conn = connect(str(tmp_path / "lease.db"))
    init_schema(conn)
    seed_jobs(conn, 1)
    a = claim_one(conn, consumer="A", now_ms=1000, lease_ms=500, max_attempt=5)  # lease -> 1500
    assert a is not None and a.attempt == 1
    # before expiry: NOT reclaimable (A still holds the live lease)
    assert claim_one(conn, consumer="B", now_ms=1200, lease_ms=500, max_attempt=5) is None
    # after expiry (now 1600 > 1500): reclaimable -> same job, attempt bumped (lost-worker recovery)
    b = claim_one(conn, consumer="B", now_ms=1600, lease_ms=500, max_attempt=5)
    assert b is not None and b.job_id == a.job_id and b.attempt == 2
    conn.close()


def test_attempt_cap_stops_reclaim(tmp_path: Path) -> None:
    conn = connect(str(tmp_path / "cap.db"))
    init_schema(conn)
    seed_jobs(conn, 1)
    max_attempt, now, lease = 3, 1000, 100
    attempts: list[int] = []
    for _ in range(10):
        c = claim_one(conn, consumer="c", now_ms=now, lease_ms=lease, max_attempt=max_attempt)
        if c is None:
            break
        attempts.append(c.attempt)
        now += lease + 1  # advance past lease expiry so the next reclaim is allowed
    assert attempts == [1, 2, 3]  # claimable only while attempt < max -> never exceeds the cap
    conn.close()
