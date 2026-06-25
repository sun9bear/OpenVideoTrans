"""T2.0 spike — real Cloudflare D1 concurrency harness (the *remote* half of the hard gate).

Local SQLite (``local_spike``) can't vouch for D1's distributed transaction semantics, so the gate
also drives the **same claim SQL** against a real D1 database over its REST API. N consumer threads
race to claim M queued jobs; we assert the same invariants — no job claimed by two live leases,
every job eventually claimed.

Design choices that keep this safe to run against a real account:
  * **Isolated table** ``_ovt_spike_jobs`` (NOT ``jobs``) — never touches the real control-plane
    table that T2.1 will own; dropped before and after the run.
  * **Secrets only from the environment** — the API token comes from ``CLOUDFLARE_API_TOKEN`` and is
    sent solely in the ``Authorization`` header. It is never logged, printed, or put in an error
    message. Account/database ids (non-secret) come from ``CLOUDFLARE_ACCOUNT_ID`` /
    ``OVT_D1_DATABASE_ID`` (GitHub repo *variables*, injected by CI).
  * **Stdlib only** (``urllib``) — no third-party dependency to install or pin for the gate.

The claim LOGIC is imported verbatim from ``spike.claim`` (``CLAIM_SQL`` + ``claim_params``); only
the table *name* is retargeted, so what runs on D1 is the exact optimistic-lock guard proven on
local SQLite.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

from spike.claim import CLAIM_SQL, claim_params

# ── Spike-isolated schema (mirrors migrations/0001_jobs_claim.sql with non-colliding names) ─────
_TABLE = "_ovt_spike_jobs"
_INDEX = "_ovt_spike_jobs_claimable"
_CREATE_TABLE = (
    f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
    "job_id TEXT PRIMARY KEY, status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, "
    "enqueue_at INTEGER NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, "
    "claim_version INTEGER NOT NULL DEFAULT 0, lease_expires_at INTEGER, started_at INTEGER, "
    "current_stage TEXT, claimed_by TEXT)"
)
_CREATE_INDEX = f"CREATE INDEX IF NOT EXISTS {_INDEX} ON {_TABLE} (status, priority, enqueue_at)"
_DROP_TABLE = f"DROP TABLE IF EXISTS {_TABLE}"


def retarget(sql: str, table: str = _TABLE) -> str:
    """Rename the ``jobs`` table token in ``sql`` to ``table`` (word-boundary match leaves the
    ``job_id`` column untouched). Used so the canonical ``CLAIM_SQL`` runs against the isolated
    spike table while keeping its optimistic-lock guard byte-for-byte identical."""
    return re.sub(r"\bjobs\b", table, sql)


_CLAIM = retarget(CLAIM_SQL)


class RemoteD1Error(RuntimeError):
    """A D1 REST call failed. Messages here never contain the API token (header-only)."""


class D1Client(Protocol):
    """Minimal D1 surface the harness needs: run one SQL statement, get back its rows (dicts)."""

    def query(self, sql: str, params: list[object] | None = None) -> list[dict[str, object]]: ...


def _extract_rows(payload: dict[str, object]) -> list[dict[str, object]]:
    """Pull the first statement's result rows out of a D1 ``/query`` response, or raise on failure.
    Pure (no I/O) so the response contract is unit-testable without a network."""
    if not payload.get("success"):
        raise RemoteD1Error(f"D1 query unsuccessful: {payload.get('errors')}")
    result = payload.get("result") or []
    if not isinstance(result, list) or not result:
        return []
    first = result[0]
    rows = first.get("results") if isinstance(first, dict) else None
    return list(rows) if isinstance(rows, list) else []


class HttpD1Client:
    """Talks to the D1 REST API. Token stays in the Authorization header — never logged."""

    def __init__(self, account_id: str, database_id: str, token: str, *, timeout: float = 30.0):
        self._url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/d1/database/{database_id}/query"
        )
        self._token = token
        self._timeout = timeout

    def query(self, sql: str, params: list[object] | None = None) -> list[dict[str, object]]:
        body = json.dumps({"sql": sql, "params": list(params or [])}).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 (https)
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # The response body carries D1's error detail and never the token; surface it, but drop
            # the original exception chain so the request URL/headers can't leak into a traceback.
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RemoteD1Error(f"D1 HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise RemoteD1Error(f"D1 request failed: {exc.reason}") from None
        return _extract_rows(payload)


@dataclass
class RemoteSpikeResult:
    n_jobs: int
    n_consumers: int
    claims: list[tuple[str, str, int, int]]  # (job_id, consumer, claim_version, attempt)
    errors: list[str] = field(default_factory=list)

    @property
    def claim_counts(self) -> Counter[str]:
        return Counter(job_id for job_id, *_ in self.claims)

    @property
    def double_claimed(self) -> dict[str, int]:
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


def _seed(client: D1Client, n_jobs: int, table: str, *, chunk: int = 25) -> None:
    """Insert ``n_jobs`` queued jobs in small batches (2 bound params/row) to stay well under D1's
    per-query bound-parameter limit."""
    for start in range(0, n_jobs, chunk):
        ids = range(start, min(start + chunk, n_jobs))
        values = ", ".join("(?, 'queued', 0, ?)" for _ in ids)
        params: list[object] = []
        for i in ids:
            params += [f"job_{i:04d}", i]
        client.query(
            f"INSERT INTO {table} (job_id, status, priority, enqueue_at) VALUES {values}", params
        )


def run_remote_spike(
    client: D1Client,
    *,
    n_jobs: int = 100,
    n_consumers: int = 20,
    lease_ms: int = 600_000,
    max_attempt: int = 5,
    now_ms: int = 1_000_000,
) -> RemoteSpikeResult:
    """Reset the isolated table, seed ``n_jobs`` queued jobs, then race ``n_consumers`` threads to
    drain them. A fixed ``now_ms`` + long lease means no lease expires mid-run, so each job must be
    claimed exactly once. Always drops the spike table afterward (best-effort cleanup)."""
    client.query(_DROP_TABLE)
    client.query(_CREATE_TABLE)
    client.query(_CREATE_INDEX)
    _seed(client, n_jobs, _TABLE)

    claims: list[tuple[str, str, int, int]] = []
    errors: list[str] = []
    lock = threading.Lock()
    gate = threading.Barrier(n_consumers)

    def consume(consumer_id: str) -> None:
        gate.wait()  # release all consumers together -> maximal contention on the first claims
        try:
            while True:
                rows = client.query(
                    _CLAIM, claim_params(now_ms, lease_ms, consumer_id, max_attempt)
                )
                if not rows:
                    break  # nothing claimable -> drained (D1 serializes writes, so no false drain)
                row = rows[0]
                with lock:
                    claims.append(
                        (
                            str(row["job_id"]),
                            consumer_id,
                            int(row["claim_version"]),  # type: ignore[arg-type]
                            int(row["attempt"]),  # type: ignore[arg-type]
                        )
                    )
        except Exception as exc:  # capture per-thread so one failure doesn't mask the others
            with lock:
                errors.append(f"{consumer_id}: {exc}")

    threads = [
        threading.Thread(target=consume, args=(f"c{i:02d}",), name=f"remote-consumer-{i}")
        for i in range(n_consumers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        client.query(_DROP_TABLE)  # cleanup — leave the real account's DB clean for T2.1
    except RemoteD1Error as exc:
        errors.append(f"cleanup: {exc}")

    return RemoteSpikeResult(n_jobs=n_jobs, n_consumers=n_consumers, claims=claims, errors=errors)


def main() -> int:
    """CI entrypoint. Reads config from the environment (token never echoed), runs the spike against
    real D1, prints a secret-free summary, exits 0 on pass / 1 on invariant breach / 2 on
    misconfig."""
    try:
        token = os.environ["CLOUDFLARE_API_TOKEN"]
        account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
        database = os.environ["OVT_D1_DATABASE_ID"]
    except KeyError as exc:
        print(
            f"missing env var {exc.args[0]} — need CLOUDFLARE_API_TOKEN (secret), "
            "CLOUDFLARE_ACCOUNT_ID, OVT_D1_DATABASE_ID (repo vars)"
        )
        return 2

    n_jobs = int(os.environ.get("SPIKE_N_JOBS", "100"))
    n_consumers = int(os.environ.get("SPIKE_N_CONSUMERS", "20"))
    client = HttpD1Client(account, database, token)

    # db id is non-secret; print only a short prefix anyway. token is never printed.
    print(f"remote D1 spike: {n_consumers} consumers x {n_jobs} jobs (db {database[:8]}...)")
    res = run_remote_spike(client, n_jobs=n_jobs, n_consumers=n_consumers)
    print(f"  claimed distinct {res.distinct_claimed}/{res.n_jobs}, total claims {len(res.claims)}")
    if res.double_claimed:
        print(f"  DOUBLE-CLAIMED: {res.double_claimed}")
    if res.errors:
        print(f"  consumer/cleanup errors: {res.errors}")
    print("RESULT:", "PASS" if res.ok else "FAIL")
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
