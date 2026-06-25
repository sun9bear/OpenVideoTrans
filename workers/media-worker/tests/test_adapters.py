"""Tests for the worker's real outbound I/O adapters: the SigV4 signer, the R2 S3
storage client, and the HTTP control-plane client. All use injected fake openers — no
network — so they pin the signing/parsing/auth logic without a live R2 or control plane.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest
from media_worker.control_plane import ControlPlaneError, HttpControlPlane, StaleClaimError
from media_worker.sigv4 import sign_request
from media_worker.storage import R2Settings, S3Storage, StorageError
from mw_fakes import make_job

# ── SigV4 ────────────────────────────────────────────────────────────────────


def test_sigv4_matches_aws_known_answer() -> None:
    # AWS's documented SigV4 "GET Object" example — a fixed known-answer vector pinning the signer.
    headers = sign_request(
        method="GET",
        url="https://examplebucket.s3.amazonaws.com/test.txt",
        region="us-east-1",
        service="s3",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        payload=b"",
        amz_datetime=datetime(2013, 5, 24, 0, 0, 0, tzinfo=UTC),
        extra_headers={"Range": "bytes=0-9"},
    )
    auth = headers["Authorization"]
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in auth
    assert "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41" in auth


def test_sigv4_hashes_put_payload() -> None:
    headers = sign_request(
        method="PUT",
        url="https://acct.r2.cloudflarestorage.com/bucket/artifacts/job_x/1/output.mp4",
        region="auto",
        service="s3",
        access_key="AKIA_TEST",
        secret_key="secret_test",
        payload=b"hello",
        amz_datetime=datetime(2026, 6, 25, 1, 2, 3, tzinfo=UTC),
    )
    assert headers["x-amz-content-sha256"] == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"  # sha256("hello")
    )
    assert headers["x-amz-date"] == "20260625T010203Z"
    assert "Signature=" in headers["Authorization"]


# ── R2 / S3 storage ──────────────────────────────────────────────────────────


def test_r2settings_from_env_requires_all_keys() -> None:
    with pytest.raises(StorageError):
        R2Settings.from_env({"R2_ACCOUNT_ID": "a", "R2_BUCKET": "b", "R2_ACCESS_KEY_ID": "k"})


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        return None

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _RecordingOpener:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.requests: list[urllib.request.Request] = []

    def open(self, req: urllib.request.Request, timeout: float | None = None) -> _Resp:
        self.requests.append(req)
        return _Resp(self._body)


def test_s3_storage_signs_and_targets_r2_without_leaking_creds() -> None:
    opener = _RecordingOpener(b"DATA")
    settings = R2Settings("acct123", "ovt-media", "AKIA_X", "secret_x")
    storage = S3Storage(
        settings, opener=opener, clock=lambda: datetime(2026, 6, 25, tzinfo=UTC)
    )
    assert storage.download("uploads/us_abc") == b"DATA"
    req = opener.requests[0]
    assert "acct123.r2.cloudflarestorage.com/ovt-media/uploads/us_abc" in req.full_url
    auth = req.get_header("Authorization")
    assert auth is not None and auth.startswith("AWS4-HMAC-SHA256")
    # The secret key must never appear in the URL — only in the signature math.
    assert "secret_x" not in req.full_url


def test_s3_storage_upload_puts_bytes() -> None:
    opener = _RecordingOpener(b"")
    settings = R2Settings("acct123", "ovt-media", "AKIA_X", "secret_x")
    storage = S3Storage(
        settings, opener=opener, clock=lambda: datetime(2026, 6, 25, tzinfo=UTC)
    )
    storage.upload("artifacts/job_x/1/output.mp4", b"PAYLOAD", content_type="video/mp4")
    req = opener.requests[0]
    assert req.get_method() == "PUT"
    assert req.data == b"PAYLOAD"
    assert req.get_header("Content-type") == "video/mp4"
    # Content-Type is part of the signed header set (integrity-protected by the signature).
    auth = req.get_header("Authorization")
    assert auth is not None and "content-type" in auth


# ── HTTP control plane ───────────────────────────────────────────────────────


class _JsonOpener:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.requests: list[urllib.request.Request] = []

    def open(self, req: urllib.request.Request, timeout: float | None = None) -> _Resp:
        self.requests.append(req)
        return _Resp(json.dumps(self._payload).encode())


def test_http_control_plane_claim_parses_envelope_and_authorizes() -> None:
    job = make_job(job_id="job_q", output_mode="dub_only")
    payload = {"job": json.loads(job.model_dump_json()), "claim_version": 3, "attempt": 1}
    opener = _JsonOpener(payload)
    cp = HttpControlPlane("https://cp.example", "tok_secret", opener=opener)
    claim = cp.claim()
    assert claim is not None
    assert claim.job.job_id == "job_q"
    assert claim.claim_version == 3
    assert claim.attempt == 1
    req = opener.requests[0]
    assert req.get_header("Authorization") == "Bearer tok_secret"
    assert "tok_secret" not in req.full_url  # bearer token never in the URL


def test_http_control_plane_claim_empty_returns_none() -> None:
    cp = HttpControlPlane("https://cp.example", "t", opener=_JsonOpener({"job": None}))
    assert cp.claim() is None


def test_http_control_plane_heartbeat_409_raises_stale() -> None:
    class _StaleOpener:
        def open(self, req: urllib.request.Request, timeout: float | None = None) -> _Resp:
            raise urllib.error.HTTPError(
                req.full_url, 409, "conflict", {}, io.BytesIO(b'{"error":"stale_claim"}')  # type: ignore[arg-type]
            )

    cp = HttpControlPlane("https://cp.example", "t", opener=_StaleOpener())
    with pytest.raises(StaleClaimError):
        cp.heartbeat("job_q", 3, stage="processing")


def test_http_control_plane_other_http_error_is_control_plane_error() -> None:
    class _BoomOpener:
        def open(self, req: urllib.request.Request, timeout: float | None = None) -> _Resp:
            raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)  # type: ignore[arg-type]

    cp = HttpControlPlane("https://cp.example", "t", opener=_BoomOpener())
    with pytest.raises(ControlPlaneError):
        cp.complete("job_q", 3, artifacts={"video_key": "artifacts/job_q/3/output.mp4"})
