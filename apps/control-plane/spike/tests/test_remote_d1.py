"""T2.0 — remote-D1 spike orchestration, exercised WITHOUT a network.

The real remote gate runs in CI (``.github/workflows/d1-spike.yml`` -> ``spike.remote_d1``) against
an actual D1 database. Here we prove the *orchestration* (reset -> seed -> race -> verify ->
cleanup) and the REST response/SQL contracts offline, using a sqlite-backed fake D1 client whose
single connection + lock models D1's single-primary write-serialization. So a green run here means
"the harness logic is correct"; the workflow run means "real D1 honors the same claim invariants".
"""
from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

import pytest
from spike.claim import CLAIM_SQL
from spike.remote_d1 import (
    RemoteD1Error,
    _extract_rows,
    main,
    retarget,
    run_remote_spike,
)


class FakeD1Client:
    """A stand-in for D1 over one sqlite connection. The lock serializes every statement, mirroring
    D1's single primary (writes serialize) — exactly the property the claim relies on."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    def query(self, sql: str, params: list[object] | None = None) -> list[dict[str, object]]:
        with self._lock:
            cur = self._conn.execute(sql, list(params or []))
            rows = cur.fetchall() if cur.description else []
            cols = [c[0] for c in cur.description] if cur.description else []
            self._conn.commit()
            return [dict(zip(cols, r, strict=True)) for r in rows]


def _fake(tmp_path: Path) -> FakeD1Client:
    tmp_path.mkdir(parents=True, exist_ok=True)  # repeats pass a per-run subdir that may not exist
    conn = sqlite3.connect(
        str(tmp_path / "d1_fake.db"), check_same_thread=False, isolation_level="DEFERRED"
    )
    conn.execute("PRAGMA journal_mode=WAL")
    return FakeD1Client(conn)


def test_remote_spike_no_double_claim_offline(tmp_path: Path) -> None:
    # 12 consumers race 50 jobs through the serialized fake: exactly-once, all claimed, no errors.
    res = run_remote_spike(_fake(tmp_path), n_jobs=50, n_consumers=12)
    assert not res.errors, res.errors
    assert res.no_double_claim, f"double-claimed: {res.double_claimed}"
    assert res.all_claimed, f"only {res.distinct_claimed}/{res.n_jobs} claimed"
    assert len(res.claims) == 50


def test_remote_spike_repeats_stay_exactly_once(tmp_path: Path) -> None:
    # Concurrency is nondeterministic — repeat to catch a flaky double-claim / lost job.
    for i in range(5):
        res = run_remote_spike(_fake(tmp_path / f"r{i}"), n_jobs=40, n_consumers=10)
        assert res.ok, f"run {i}: double={res.double_claimed} errors={res.errors}"


def test_retarget_renames_table_not_column() -> None:
    out = retarget(CLAIM_SQL)
    assert out.count("_ovt_spike_jobs") == 2  # UPDATE <t> ... FROM <t>
    assert re.search(r"\bjobs\b", out) is None  # no bare `jobs` table token left
    assert "job_id" in out  # the column survives the rename untouched


def test_extract_rows_success_returns_returning_rows() -> None:
    payload = {
        "success": True,
        "result": [
            {
                "success": True,
                "results": [{"job_id": "job_0001", "claim_version": 1, "attempt": 1}],
                "meta": {},
            }
        ],
    }
    rows = _extract_rows(payload)
    assert rows == [{"job_id": "job_0001", "claim_version": 1, "attempt": 1}]


def test_extract_rows_empty_result_is_empty() -> None:
    assert _extract_rows({"success": True, "result": []}) == []
    assert _extract_rows({"success": True, "result": [{"success": True, "results": []}]}) == []


def test_extract_rows_failure_raises_without_token() -> None:
    payload = {"success": False, "errors": [{"code": 7400, "message": "no such table"}]}
    with pytest.raises(RemoteD1Error) as exc:
        _extract_rows(payload)
    assert "Bearer" not in str(exc.value) and "token" not in str(exc.value).lower()


def test_main_missing_env_returns_2(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "OVT_D1_DATABASE_ID"):
        monkeypatch.delenv(var, raising=False)
    assert main() == 2  # misconfig -> exit 2, never attempts a network call
