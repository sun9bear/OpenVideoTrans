"""Tests for the worker's SECRETS bootstrap (#21): pull every other secret from the control plane
over the authed /internal channel, hold it in memory only, and survive a zero-downtime bootstrap
secret rotation via the staged `next` token. All use injected fake openers — no network."""
from __future__ import annotations

import contextlib
import json
import threading
import urllib.error
import urllib.request

import pytest
from media_worker.control_plane import (
    ControlPlaneError,
    HttpControlPlane,
    StaleClaimError,
    parse_credentials,
)
from media_worker.storage import R2Settings

_R2 = {
    "accountId": "acct123",
    "bucket": "ovt-media",
    "accessKeyId": "AKIA_X",
    "secretAccessKey": "secret_x",
}


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _JsonOpener:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.requests: list[urllib.request.Request] = []

    def open(self, req: urllib.request.Request, timeout: float | None = None) -> _Resp:
        self.requests.append(req)
        return _Resp(json.dumps(self._payload).encode())


class _RotatingOpener:
    """Models a control plane that accepts exactly one bearer (the post-rotation `next`): any other
    Authorization gets 401. Records the bearer seen on each call so the retry/promote is visible."""

    def __init__(self, accepted: str, payload: object) -> None:
        self._accepted = accepted
        self._payload = payload
        self.bearers: list[str | None] = []

    def open(self, req: urllib.request.Request, timeout: float | None = None) -> _Resp:
        self.bearers.append(req.get_header("Authorization"))
        if req.get_header("Authorization") != self._accepted:
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)  # type: ignore[arg-type]
        return _Resp(json.dumps(self._payload).encode())


# ── parse_credentials ─────────────────────────────────────────────────────────


def test_parse_credentials_maps_r2_and_providers() -> None:
    creds = parse_credentials(
        {"r2": _R2, "providers": {"groq": {"apiKey": "gk"}, "deepl": {"apiKey": "dk"}}}
    )
    assert creds.r2 == R2Settings("acct123", "ovt-media", "AKIA_X", "secret_x")
    assert creds.providers["groq"] == {"apiKey": "gk"}
    assert creds.providers["deepl"] == {"apiKey": "dk"}
    assert "cloudflare" not in creds.providers


def test_parse_credentials_missing_r2_raises_without_leaking() -> None:
    with pytest.raises(ControlPlaneError) as ei:
        parse_credentials({"providers": {}})
    # The error names the missing field, never a secret value.
    assert "secret" not in str(ei.value)


def test_parse_credentials_rejects_empty_or_null_r2_field() -> None:
    # A present-but-empty/null/non-str r2 field must FAIL LOUD at the bootstrap boundary, not
    # silently coerce to a junk string (str(None) -> "None") that only surfaces as a confusing
    # S3/DNS error later. The error names the field, never the real secret value.
    for bad in ({**_R2, "accountId": ""}, {**_R2, "bucket": None}, {**_R2, "accessKeyId": "  "}):
        with pytest.raises(ControlPlaneError) as ei:
            parse_credentials({"r2": bad})
        assert "secret_x" not in str(ei.value)  # the (valid) secret value never leaks


def test_parse_credentials_tolerates_absent_providers() -> None:
    creds = parse_credentials({"r2": _R2})
    assert creds.providers == {}


# ── HttpControlPlane.get_credentials ───────────────────────────────────────────


def test_get_credentials_fetches_over_authed_channel() -> None:
    opener = _JsonOpener({"r2": _R2, "providers": {"groq": {"apiKey": "gk"}}})
    cp = HttpControlPlane("https://cp.example", "tok", opener=opener)
    creds = cp.get_credentials()
    assert creds.r2.access_key_id == "AKIA_X"
    assert creds.providers["groq"] == {"apiKey": "gk"}
    req = opener.requests[0]
    assert req.get_method() == "GET"
    assert req.full_url.endswith("/internal/credentials")
    assert req.get_header("Authorization") == "Bearer tok"
    assert "tok" not in req.full_url  # bearer never in the URL


def test_get_credentials_holds_in_memory_only(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Memory-only invariant: pulling credentials must not persist them anywhere on disk — the secret
    # is reachable ONLY through the returned in-memory object.
    monkeypatch.chdir(tmp_path)
    opener = _JsonOpener({"r2": _R2, "providers": {"groq": {"apiKey": "gk-secret"}}})
    cp = HttpControlPlane("https://cp.example", "tok", opener=opener)
    creds = cp.get_credentials()
    assert creds.providers["groq"]["apiKey"] == "gk-secret"
    assert list(tmp_path.iterdir()) == []  # nothing written to disk


# ── dual bootstrap secret: zero-downtime rotation ──────────────────────────────


def test_dual_token_retries_with_next_then_promotes() -> None:
    opener = _RotatingOpener("Bearer new_tok", {"job": None})
    cp = HttpControlPlane("https://cp.example", "old_tok", next_token="new_tok", opener=opener)
    # First call: the retired `old` token is rejected (401) -> retry with staged `next` succeeds.
    assert cp.claim() is None
    assert opener.bearers == ["Bearer old_tok", "Bearer new_tok"]
    # Promotion: subsequent calls go straight to the working token (no extra round-trip).
    opener.bearers.clear()
    assert cp.claim() is None
    assert opener.bearers == ["Bearer new_tok"]


def test_dual_token_promotion_is_thread_safe() -> None:
    # The worker calls the control plane from BOTH the main thread and the heartbeat thread. If a
    # rotation retires the old token mid-job, both can enter the 401-retry path at once; a
    # non-atomic promotion could set _auth to None (poison the client). Force the race with a
    # barrier and check every call still succeeds via `next`.
    accepted = "Bearer new_tok"
    barrier = threading.Barrier(2)

    class _ConcurrentOpener:
        def open(self, req: urllib.request.Request, timeout: float | None = None) -> _Resp:
            if req.get_header("Authorization") != accepted:
                # Sync both threads here so they both promote near-simultaneously.
                with contextlib.suppress(threading.BrokenBarrierError):
                    barrier.wait(timeout=5)
                raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)  # type: ignore[arg-type]
            return _Resp(json.dumps({"job": None}).encode())

    cp = HttpControlPlane(
        "https://cp.example", "old_tok", next_token="new_tok", opener=_ConcurrentOpener()
    )
    results: list[object] = []
    lock = threading.Lock()

    def run() -> None:
        try:
            value = cp.claim()
        except Exception as exc:  # noqa: BLE001 — record any failure for the assertion
            value = exc
        with lock:
            results.append(value)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [None, None]  # both raced through the retry and succeeded via `next`
    # A subsequent call must still work — would raise if _auth had been poisoned to None.
    assert cp.claim() is None


def test_insecure_control_plane_url_is_refused() -> None:
    # The credentials channel carries the bootstrap bearer + R2/provider secrets; a plaintext http
    # origin must fail closed at construction, before any secret crosses the wire.
    with pytest.raises(ControlPlaneError):
        HttpControlPlane("http://cp.example", "tok")


def test_localhost_http_is_allowed_for_dev() -> None:
    # Narrow dev exception so DEVLOOP can run the worker against a local control plane over http.
    HttpControlPlane("http://localhost:8787", "tok")
    HttpControlPlane("http://127.0.0.1:8787", "tok")
    HttpControlPlane("https://cp.example", "tok")  # https always allowed


def test_no_next_token_propagates_401() -> None:
    class _Opener401:
        def open(self, req: urllib.request.Request, timeout: float | None = None) -> _Resp:
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)  # type: ignore[arg-type]

    cp = HttpControlPlane("https://cp.example", "tok", opener=_Opener401())
    with pytest.raises(ControlPlaneError):
        cp.get_config()


def test_dual_token_does_not_mask_stale_claim_409() -> None:
    # A 409 must still surface as StaleClaimError even with a next token configured — the dual-token
    # retry is for auth (401) only, never for the lease-reclaim signal.
    class _Opener409:
        def open(self, req: urllib.request.Request, timeout: float | None = None) -> _Resp:
            raise urllib.error.HTTPError(req.full_url, 409, "conflict", {}, None)  # type: ignore[arg-type]

    cp = HttpControlPlane("https://cp.example", "tok", next_token="next", opener=_Opener409())
    with pytest.raises(StaleClaimError):
        cp.heartbeat("job_x", 1, stage="processing")
