"""HTTP client for the control plane's /internal worker endpoints.

Stdlib urllib only. The internal bearer token authorizes every call and is sent ONLY in the
Authorization header — never in a URL, log line, or error message. Errors carry method + path +
status, never the response body (which could echo a secret) or the token.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ovt_schemas import Job

from .config import WorkerConfig, parse_config


@dataclass(frozen=True)
class Claim:
    job: Job
    claim_version: int
    attempt: int


class ControlPlaneError(RuntimeError):
    """A control-plane call failed (a non-2xx other than the modeled 409)."""


class StaleClaimError(ControlPlaneError):
    """The lease was reclaimed (409): this worker no longer owns the job and must stop renewing."""


class ControlPlane(Protocol):
    def get_config(self) -> WorkerConfig: ...
    def claim(self) -> Claim | None: ...
    def heartbeat(self, job_id: str, claim_version: int, *, stage: str | None = None) -> None: ...
    def complete(
        self, job_id: str, claim_version: int, *, artifacts: Mapping[str, str]
    ) -> None: ...
    def fail(
        self, job_id: str, claim_version: int, *, error_code: str, error_detail: str | None = None
    ) -> None: ...


class HttpControlPlane:
    """ControlPlane over HTTP(S) against the control-plane Worker's /internal endpoints."""

    def __init__(
        self, base_url: str, token: str, *, opener: Any = None, timeout: float = 30.0
    ) -> None:
        self._base = base_url.rstrip("/")
        self._auth = f"Bearer {token}"
        self._opener = opener if opener is not None else urllib.request.build_opener()
        self._timeout = timeout

    def _call(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self._base}{path}", data=data, method=method)
        req.add_header("Authorization", self._auth)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 409:
                raise StaleClaimError(f"{method} {path} -> 409 stale_claim") from None
            # Never include the response body (it could echo a secret) — status + path only.
            raise ControlPlaneError(f"{method} {path} -> {e.code}") from None
        if not raw:
            return {}
        parsed: Any = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}

    def get_config(self) -> WorkerConfig:
        return parse_config(self._call("GET", "/internal/config"))

    def claim(self) -> Claim | None:
        resp = self._call("POST", "/internal/jobs/claim")
        job_data = resp.get("job")
        if not job_data:
            return None
        return Claim(
            job=Job.model_validate(job_data),
            claim_version=int(resp["claim_version"]),
            attempt=int(resp["attempt"]),
        )

    def heartbeat(self, job_id: str, claim_version: int, *, stage: str | None = None) -> None:
        body: dict[str, Any] = {"claim_version": claim_version}
        if stage is not None:
            body["stage"] = stage
        self._call("POST", f"/internal/jobs/{job_id}/progress", body)

    def complete(self, job_id: str, claim_version: int, *, artifacts: Mapping[str, str]) -> None:
        self._call(
            "POST",
            f"/internal/jobs/{job_id}/complete",
            {"claim_version": claim_version, "artifacts": dict(artifacts)},
        )

    def fail(
        self, job_id: str, claim_version: int, *, error_code: str, error_detail: str | None = None
    ) -> None:
        body: dict[str, Any] = {"claim_version": claim_version, "error_code": error_code}
        if error_detail is not None:
            body["error_detail"] = error_detail
        self._call("POST", f"/internal/jobs/{job_id}/fail", body)
