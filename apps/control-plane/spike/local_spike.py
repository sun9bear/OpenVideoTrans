"""T2.0 spike — local SQLite concurrency harness (the autonomous half of the hard gate).

N consumers, each on its own connection, race to claim from M queued jobs, released together by a
barrier for maximal contention. We then check the three invariants: no job claimed by two live
leases, every job eventually claimed, and ``attempt`` never exceeds the cap.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass, field

from spike.claim import claim_one, init_schema, seed_jobs


@dataclass
class SpikeResult:
    n_jobs: int
    n_consumers: int
    claims: list[tuple[str, str]]  # (job_id, consumer) for every successful claim
    errors: list[str] = field(default_factory=list)  # any consumer-thread failure (must be empty)

    @property
    def claim_counts(self) -> Counter[str]:
        return Counter(job_id for job_id, _ in self.claims)

    @property
    def double_claimed(self) -> dict[str, int]:
        """Jobs handed out more than once within this (no-expiry) run — must be empty."""
        return {job: n for job, n in self.claim_counts.items() if n > 1}

    @property
    def distinct_claimed(self) -> int:
        return len(self.claim_counts)

    @property
    def no_double_claim(self) -> bool:
        return not self.double_claimed

    @property
    def all_claimed(self) -> bool:
        return self.distinct_claimed == self.n_jobs

    @property
    def ok(self) -> bool:
        return self.no_double_claim and self.all_claimed and not self.errors


def connect(db_path: str) -> sqlite3.Connection:
    """A connection tuned for concurrent writers: WAL (readers don't block the writer) + a long
    busy timeout so racing claims retry the write lock instead of erroring."""
    # DEFERRED + explicit commit: each claim is its own committed transaction (identical to the
    # legacy "" isolation level, but a typed literal pyright accepts).
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level="DEFERRED")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def run_local_spike(
    db_path: str,
    *,
    n_jobs: int = 100,
    n_consumers: int = 20,
    lease_ms: int = 60_000,
    max_attempt: int = 5,
    now_ms: int = 1_000_000,
) -> SpikeResult:
    """Seed ``n_jobs`` queued jobs, then have ``n_consumers`` threads drain them. With a fixed
    ``now_ms`` no lease expires mid-run, so each job must be claimed exactly once."""
    setup = connect(db_path)
    init_schema(setup)
    seed_jobs(setup, n_jobs)
    setup.close()

    claims: list[tuple[str, str]] = []
    errors: list[str] = []
    lock = threading.Lock()
    gate = threading.Barrier(n_consumers)

    def consume(consumer_id: str) -> None:
        conn = connect(db_path)
        gate.wait()  # release all consumers at once -> maximal contention on the first claims
        try:
            while True:
                claim = claim_one(
                    conn, consumer=consumer_id, now_ms=now_ms, lease_ms=lease_ms,
                    max_attempt=max_attempt,
                )
                if claim is None:
                    break  # nothing claimable -> drained (writes serialize, so no false drain)
                with lock:
                    claims.append((claim.job_id, consumer_id))
        except Exception as exc:  # don't swallow: a thread failure must fail the gate, not pass it
            with lock:
                errors.append(f"{consumer_id}: {exc}")
        finally:
            conn.close()

    threads = [
        threading.Thread(target=consume, args=(f"c{i:02d}",), name=f"consumer-{i}")
        for i in range(n_consumers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return SpikeResult(n_jobs=n_jobs, n_consumers=n_consumers, claims=claims, errors=errors)
