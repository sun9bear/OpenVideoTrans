"""T2.0 spike — the portable optimistic-lock job claim (SQLite <-> Cloudflare D1).

The whole point of the hard gate: prove this claim is **exactly-once under concurrency**. The
guarantee rests on write-serialization (SQLite, and D1 which is SQLite with a single primary):

    UPDATE the single highest-priority, oldest CLAIMABLE job to 'running', with the claimable
    guard REPEATED in the outer WHERE. Two consumers racing the same job both run their UPDATE
    one-after-another (writes serialize); the first flips status->running, so the second's outer
    guard (queued OR lease-expired) no longer matches -> 0 rows affected -> it claims nothing and
    moves on. No row is ever handed to two live leases.

A 'running' job whose `lease_expires_at <= now` is claimable again (lost-worker recovery), and
`attempt < max_attempt` caps reclaims so a poison job can't loop forever.

Params are positional ``?`` (the common denominator of Python ``sqlite3`` and the D1 REST API);
``claim_params`` returns them in the order the ``?`` appear in ``CLAIM_SQL``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_MIGRATION = Path(__file__).parent / "migrations" / "0001_jobs_claim.sql"

CLAIM_SQL = """
UPDATE jobs
SET status = 'running',
    claim_version = claim_version + 1,
    attempt = attempt + 1,
    lease_expires_at = ? + ?,
    started_at = COALESCE(started_at, ?),
    current_stage = 'claimed',
    claimed_by = ?
WHERE job_id = (
    SELECT job_id FROM jobs
    WHERE (status = 'queued' OR (status = 'running' AND lease_expires_at <= ?))
      AND attempt < ?
    ORDER BY priority DESC, enqueue_at ASC
    LIMIT 1
)
  AND (status = 'queued' OR (status = 'running' AND lease_expires_at <= ?))
  AND attempt < ?
RETURNING job_id, claim_version, attempt
""".strip()


@dataclass(frozen=True)
class Claim:
    job_id: str
    claim_version: int
    attempt: int


def claim_params(now_ms: int, lease_ms: int, consumer: str, max_attempt: int) -> list[object]:
    """Positional params for ``CLAIM_SQL``, in ``?`` appearance order:
    now, lease_ms, now, consumer, now, max_attempt, now, max_attempt."""
    return [now_ms, lease_ms, now_ms, consumer, now_ms, max_attempt, now_ms, max_attempt]


def migration_sql() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(migration_sql())
    conn.commit()


def seed_jobs(conn: sqlite3.Connection, n: int, base_ms: int = 0) -> None:
    """Insert ``n`` queued jobs (FIFO by enqueue_at)."""
    conn.executemany(
        "INSERT INTO jobs (job_id, status, priority, enqueue_at) VALUES (?, 'queued', 0, ?)",
        [(f"job_{i:04d}", base_ms + i) for i in range(n)],
    )
    conn.commit()


def claim_one(
    conn: sqlite3.Connection, *, consumer: str, now_ms: int, lease_ms: int, max_attempt: int
) -> Claim | None:
    """Atomically claim one job for ``consumer`` (its own committed transaction). Returns the
    ``Claim`` or ``None`` when nothing is claimable right now."""
    row = conn.execute(
        CLAIM_SQL, claim_params(now_ms, lease_ms, consumer, max_attempt)
    ).fetchone()
    conn.commit()
    return Claim(row[0], row[1], row[2]) if row else None
