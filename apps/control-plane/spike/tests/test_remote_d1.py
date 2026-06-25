"""T2.0 — remote-D1 spike orchestration, exercised WITHOUT a network.

The real remote gate runs in CI (``.github/workflows/d1-spike.yml`` -> ``spike.remote_d1``) against
an actual D1 database. Here we prove the *orchestration* (reset -> seed -> race -> verify ->
cleanup) and the REST response/SQL contracts offline, using a sqlite-backed fake D1 client whose
single connection + lock models D1's single-primary write-serialization. So a green run here means
"the harness logic is correct"; the workflow run means "real D1 honors the same claim invariants".
"""
from __future__ import annotations

import email.message
import io
import json
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from spike.claim import CLAIM_SQL
from spike.remote_d1 import (
    HttpD1Client,
    RemoteD1Error,
    _extract_rows,
    main,
    retarget,
    run_remote_reclaim_check,
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


# ── reclaim + attempt-cap: the two invariants the race alone cannot prove (run on real D1 too) ──


def test_remote_reclaim_check_offline(tmp_path: Path) -> None:
    res = run_remote_reclaim_check(_fake(tmp_path), max_attempt=3, lease_ms=1000)
    assert res.attempts == [1, 2, 3]  # expired leases reclaim w/ bumped attempt; cap halts at 3
    assert res.ok


# ── HttpD1Client: real request construction + token-safe error branches + retry, no network ─────

_TOKEN = "SEKRET-TOKEN-do-not-leak"
_OK_BODY = json.dumps(
    {"success": True, "result": [{"success": True, "results": [{"job_id": "job_0001"}]}]}
).encode()


class _Resp:
    """Duck-typed stand-in for the HTTPResponse context manager that urlopen returns."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


def _http_error(req: urllib.request.Request, code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        req.full_url, code, "err", email.message.Message(), io.BytesIO(body)
    )


def test_http_client_builds_post_with_bearer_and_json_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(req: urllib.request.Request, timeout: float | None = None) -> _Resp:
        assert isinstance(req.data, bytes)
        captured.update(
            method=req.get_method(),
            auth=req.get_header("Authorization"),
            body=json.loads(req.data.decode()),
            url=req.full_url,
        )
        return _Resp(_OK_BODY)

    monkeypatch.setattr(urllib.request, "urlopen", handler)
    client = HttpD1Client("acct123", "db456", _TOKEN, sleep=lambda _: None)
    assert client.query("SELECT 1", ["a", 2]) == [{"job_id": "job_0001"}]
    assert captured["method"] == "POST"
    assert captured["auth"] == f"Bearer {_TOKEN}"
    assert captured["body"] == {"sql": "SELECT 1", "params": ["a", 2]}
    assert _TOKEN not in str(captured["url"])  # token is header-only, never in the URL


def test_http_client_http_error_omits_token_and_drops_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: urllib.request.Request, timeout: float | None = None) -> _Resp:
        raise _http_error(req, 400, b'{"errors":[{"message":"bad sql"}]}')

    monkeypatch.setattr(urllib.request, "urlopen", handler)
    client = HttpD1Client("acct", "db", _TOKEN, sleep=lambda _: None)
    with pytest.raises(RemoteD1Error) as exc:
        client.query("SELECT 1")
    assert _TOKEN not in str(exc.value)  # non-retryable 4xx -> surfaced, token absent
    assert exc.value.__cause__ is None  # `from None` keeps URL/headers/token out of the traceback


def test_http_client_url_error_retries_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(req: urllib.request.Request, timeout: float | None = None) -> _Resp:
        calls["n"] += 1
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", handler)
    client = HttpD1Client("acct", "db", _TOKEN, max_retries=3, sleep=sleeps.append)
    with pytest.raises(RemoteD1Error) as exc:
        client.query("SELECT 1")
    assert calls["n"] == 4  # initial try + 3 retries
    assert len(sleeps) == 3
    assert _TOKEN not in str(exc.value)


def test_http_client_retries_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    seq = [429, 429]
    sleeps: list[float] = []

    def handler(req: urllib.request.Request, timeout: float | None = None) -> _Resp:
        if seq:
            raise _http_error(req, seq.pop(0), b"{}")
        return _Resp(_OK_BODY)

    monkeypatch.setattr(urllib.request, "urlopen", handler)
    client = HttpD1Client("acct", "db", _TOKEN, max_retries=4, sleep=sleeps.append)
    assert client.query("SELECT 1") == [{"job_id": "job_0001"}]
    assert len(sleeps) == 2  # two 429s retried, third attempt succeeded


def test_http_client_success_false_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    body = json.dumps({"success": False, "errors": [{"message": "no table"}]}).encode()

    def handler(req: urllib.request.Request, timeout: float | None = None) -> _Resp:
        return _Resp(body)

    monkeypatch.setattr(urllib.request, "urlopen", handler)
    client = HttpD1Client("acct", "db", _TOKEN, sleep=sleeps.append)
    with pytest.raises(RemoteD1Error):
        client.query("SELECT 1")
    assert sleeps == []  # genuine SQL error -> surfaced immediately, never retried


def test_retry_delay_honors_retry_after_else_backoff() -> None:
    client = HttpD1Client("acct", "db", _TOKEN, backoff_base=0.5, sleep=lambda _: None)
    assert client._retry_delay(0, "5") == 5.0  # honor Retry-After seconds
    assert client._retry_delay(99, "100") == 30.0  # capped at 30s
    assert client._retry_delay(2, None) == 2.0  # 0.5 * 2**2 exponential backoff
    assert client._retry_delay(0, "garbage") == 0.5  # non-numeric Retry-After -> backoff
