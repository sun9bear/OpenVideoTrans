"""HTTP client for the control plane's /internal worker endpoints.

Stdlib urllib only. The internal bearer token authorizes every call and is sent ONLY in the
Authorization header — never in a URL, log line, or error message. Errors carry method + path +
status, never the response body (which could echo a secret) or the token.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ovt_schemas import Job

from .config import WorkerConfig, parse_config
from .storage import R2Settings

# Sent on every outbound HTTP request. Cloudflare's edge blocks the default "Python-urllib/x.y"
# User-Agent (managed bot rule → 403 before the request reaches the Worker); a normal app UA clears
# it. storage.py carries its own copy for its own requests; this one is the control-plane client's.
USER_AGENT = "OpenVideoTrans-Worker/1.0"


@dataclass(frozen=True)
class Claim:
    job: Job
    claim_version: int
    attempt: int


@dataclass(frozen=True)
class ProgressTelemetry:
    """OBS (#24) worker /progress telemetry — NAMES + INTS only. By construction it cannot hold a
    provider key, raw request/response, plaintext IP, or original filename (脱敏), and the control
    plane re-validates against the same allowlist before storing (apps/control-plane/src/obs.ts
    parseProgressMeta). The T2.2 stub selects no provider and never touches the free pool, so
    provider / chunk_* / free_pool_result stay None until the real pipeline (M2-CLOSE) — the
    CONTRACT ships now so M2-CLOSE only fills the values."""

    stage_elapsed_ms: int | None = None
    provider: str | None = None
    chunk_index: int | None = None
    chunk_count: int | None = None
    free_pool_result: str | None = None


def build_progress_body(
    claim_version: int, stage: str | None, telemetry: ProgressTelemetry | None
) -> dict[str, Any]:
    """Assemble the /progress (heartbeat) JSON body. Only claim_version + stage + the telemetry's
    NON-None fields are included (an omitted field is never serialized as null), so the payload
    carries solely the allowlisted observability fields — redaction-safe by construction."""
    body: dict[str, Any] = {"claim_version": claim_version}
    if stage is not None:
        body["stage"] = stage
    if telemetry is not None:
        if telemetry.stage_elapsed_ms is not None:
            body["stage_elapsed_ms"] = telemetry.stage_elapsed_ms
        if telemetry.provider is not None:
            body["provider"] = telemetry.provider
        if telemetry.chunk_index is not None:
            body["chunk_index"] = telemetry.chunk_index
        if telemetry.chunk_count is not None:
            body["chunk_count"] = telemetry.chunk_count
        if telemetry.free_pool_result is not None:
            body["free_pool_result"] = telemetry.free_pool_result
    return body


class ControlPlaneError(RuntimeError):
    """A control-plane call failed (a non-2xx other than the modeled 409). Carries the HTTP status
    (when known) so the dual-token retry can recognize a 401 without re-parsing the message."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class StaleClaimError(ControlPlaneError):
    """The lease was reclaimed (409): this worker no longer owns the job and must stop renewing."""


@dataclass(frozen=True)
class ProviderSnapshot:
    """The shared FREE-POOL circuit-breaker snapshot the worker pulls from the control plane
    (GET /internal/providers/availability). Field names mirror the control-plane JSON AND the
    provider-adapters ``ProviderAvailability`` dataclass (now_ms / exhausted_until) so the routing
    layer hydrates it directly — but it is defined HERE (transport-owned, stdlib-only) so this
    client keeps no provider-adapters import edge."""

    now_ms: int
    exhausted_until: Mapping[str, int]


@dataclass(frozen=True)
class WorkerCredentials:
    """Secrets pulled from the control plane at startup and held in MEMORY ONLY — never written to
    the worker box's disk (SECRETS #21). ``r2`` drives the storage client; ``providers`` carries
    each configured free provider's keys for the FREE-POOL adapters to consume."""

    r2: R2Settings
    providers: Mapping[str, Mapping[str, str]]


def parse_credentials(payload: Mapping[str, Any]) -> WorkerCredentials:
    """Parse a /internal/credentials response into WorkerCredentials. Raises ControlPlaneError
    naming a missing FIELD (never a secret value) so a misconfigured control plane fails loud."""
    r2 = payload.get("r2")
    if not isinstance(r2, Mapping):
        raise ControlPlaneError("GET /internal/credentials -> missing r2 settings")

    def r2_field(name: str) -> str:
        # Validate the VALUE, not just key presence: a present-but-null/non-str/empty field must
        # fail loud at this bootstrap trust boundary rather than str()-coercing to junk
        # (str(None)->"None") that only surfaces as a confusing S3/DNS error at first I/O. The error
        # names the field, never the value.
        value = r2.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ControlPlaneError(f"GET /internal/credentials -> invalid r2 field {name!r}")
        return value

    settings = R2Settings(
        account_id=r2_field("accountId"),
        bucket=r2_field("bucket"),
        access_key_id=r2_field("accessKeyId"),
        secret_access_key=r2_field("secretAccessKey"),
    )
    raw = payload.get("providers")
    providers: dict[str, Mapping[str, str]] = {}
    if isinstance(raw, Mapping):
        for name, fields in raw.items():
            if isinstance(fields, Mapping):
                providers[str(name)] = {str(k): str(v) for k, v in fields.items()}
    return WorkerCredentials(r2=settings, providers=providers)


# Hosts allowed over plaintext http for local dev only (DEVLOOP). Everything else MUST be https.
_DEV_PLAINTEXT_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def require_secure_base(base_url: str) -> None:
    """Fail closed unless the control-plane URL is TLS (https). Every /internal call carries the
    bootstrap bearer, and /internal/credentials returns the R2 + provider secrets, so a plaintext
    http origin would expose them all (SECRETS contract: TLS + shared-secret auth). A narrow
    localhost exception keeps local dev usable. Raises ControlPlaneError naming only the scheme."""
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and (parsed.hostname or "").lower() in _DEV_PLAINTEXT_HOSTS:
        return
    raise ControlPlaneError(
        f"control-plane URL must be https (scheme {parsed.scheme!r}); refusing to send "
        "credentials over plaintext"
    )


class ControlPlane(Protocol):
    def get_config(self) -> WorkerConfig: ...
    # light_only (M2-CLOSE PR-D, #26): when the worker's heavy budget is full it asks for a LIGHT
    # (subtitle_only) job only, so the reserved free_min_share slots are never taken by a dub job.
    def claim(self, *, light_only: bool = False) -> Claim | None: ...
    def heartbeat(
        self,
        job_id: str,
        claim_version: int,
        *,
        stage: str | None = None,
        telemetry: ProgressTelemetry | None = None,
    ) -> None: ...
    def complete(
        self, job_id: str, claim_version: int, *, artifacts: Mapping[str, str]
    ) -> None: ...
    def fail(
        self, job_id: str, claim_version: int, *, error_code: str, error_detail: str | None = None
    ) -> None: ...
    def get_provider_availability(self) -> ProviderSnapshot: ...
    def report_provider_exhausted(
        self, provider: str, *, reset_at_ms: int, reason: str | None = None
    ) -> None: ...


class HttpControlPlane:
    """ControlPlane over HTTP(S) against the control-plane Worker's /internal endpoints."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        next_token: str | None = None,
        opener: Any = None,
        timeout: float = 30.0,
    ) -> None:
        require_secure_base(base_url)  # fail closed on a plaintext control-plane URL
        self._base = base_url.rstrip("/")
        self._auth = f"Bearer {token}"
        # Staged next bootstrap secret for a zero-downtime rotation (SECRETS): if the current token
        # is rejected (401), _call retries once with `next` and promotes it. None when not rotating.
        self._next_auth = f"Bearer {next_token}" if next_token else None
        # The worker calls the control plane from BOTH the main thread and the heartbeat thread, so
        # the token state is shared. This lock guards ONLY the in-memory snapshot + promotion (never
        # held across network I/O), so a concurrent rotation can't poison _auth (e.g. set it None).
        self._auth_lock = threading.Lock()
        self._opener = opener if opener is not None else urllib.request.build_opener()
        self._timeout = timeout

    def _send(self, method: str, path: str, data: bytes | None, auth: str) -> dict[str, Any]:
        req = urllib.request.Request(f"{self._base}{path}", data=data, method=method)
        req.add_header("Authorization", auth)
        # Explicit User-Agent: the default "Python-urllib/x.y" is blocked at the Cloudflare edge
        # (managed bot rule) with a 403 before reaching the Worker. A normal app UA clears it.
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 409:
                raise StaleClaimError(f"{method} {path} -> 409 stale_claim") from None
            # Never include the response body (it could echo a secret) — status + path only.
            raise ControlPlaneError(f"{method} {path} -> {e.code}", status=e.code) from None
        if not raw:
            return {}
        parsed: Any = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}

    def _call(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        with self._auth_lock:
            auth, next_auth = self._auth, self._next_auth  # snapshot; don't hold the lock over I/O
        try:
            return self._send(method, path, data, auth)
        except ControlPlaneError as err:
            # Zero-downtime rotation: our current bootstrap secret was retired (401). Retry once
            # with the staged `next` token and, on success, promote it so later calls skip the
            # retry. A 401 means the rejected request did nothing server-side, so re-sending the
            # same body is safe. A 409 (StaleClaimError, status None) never matches here, so the
            # lease signal stays intact.
            if err.status == 401 and next_auth is not None:
                result = self._send(method, path, data, next_auth)
                self._promote(next_auth)
                return result
            raise

    def _promote(self, used: str) -> None:
        # Promote a successfully-used `next` token to current exactly once. CAS under the lock so a
        # concurrent retry on another thread (heartbeat vs main) can't double-promote and clobber
        # _auth to None: only the thread that still sees `used` staged wins; the rest are no-ops.
        with self._auth_lock:
            if self._next_auth == used:
                self._auth = used
                self._next_auth = None

    def get_config(self) -> WorkerConfig:
        return parse_config(self._call("GET", "/internal/config"))

    def get_credentials(self) -> WorkerCredentials:
        # Bootstrap pull (SECRETS): R2 storage creds + free-provider keys over the authed channel.
        return parse_credentials(self._call("GET", "/internal/credentials"))

    def claim(self, *, light_only: bool = False) -> Claim | None:
        # light_only=True restricts the claim to a LIGHT (subtitle_only) job (free_min_share
        # reserved slot, PR-D). Sent only when True so a normal claim keeps its empty body
        # (backward compatible).
        body = {"light_only": True} if light_only else None
        resp = self._call("POST", "/internal/jobs/claim", body)
        job_data = resp.get("job")
        if not job_data:
            return None
        return Claim(
            job=Job.model_validate(job_data),
            claim_version=int(resp["claim_version"]),
            attempt=int(resp["attempt"]),
        )

    def heartbeat(
        self,
        job_id: str,
        claim_version: int,
        *,
        stage: str | None = None,
        telemetry: ProgressTelemetry | None = None,
    ) -> None:
        # OBS (#24): the worker folds redaction-safe stage timing / provider / chunk / free-pool
        # telemetry into the same /progress body. build_progress_body emits only the allowlisted,
        # non-None fields; the control plane re-validates before storing.
        body = build_progress_body(claim_version, stage, telemetry)
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

    def get_provider_availability(self) -> ProviderSnapshot:
        # FREE-POOL (#23): pull the shared circuit-breaker snapshot so routing skips a provider that
        # ANOTHER box already saw 429 on. A partial/garbled field hydrates safe defaults (no
        # exhaustion) rather than crashing the claim loop on a malformed control-plane response.
        resp = self._call("GET", "/internal/providers/availability")
        raw = resp.get("exhausted_until")
        exhausted: dict[str, int] = {}
        if isinstance(raw, Mapping):
            for name, until in raw.items():
                if isinstance(until, (int, float)) and not isinstance(until, bool):
                    exhausted[str(name)] = int(until)
        now = resp.get("now_ms")
        now_ms = int(now) if isinstance(now, (int, float)) and not isinstance(now, bool) else 0
        return ProviderSnapshot(now_ms=now_ms, exhausted_until=exhausted)

    def report_provider_exhausted(
        self, provider: str, *, reset_at_ms: int, reason: str | None = None
    ) -> None:
        # Report a 429/quota-exhausted FREE provider so EVERY box circuit-breaks it (429 不复撞).
        # The control plane validates: a paid name is rejected 403 (a paid API is never
        # auto-invoked), so the worker only ever reports the FREE provider it routed. `reason` is a
        # short telemetry tag.
        body: dict[str, Any] = {"provider": provider, "resetAt": reset_at_ms}
        if reason is not None:
            body["reason"] = reason
        self._call("POST", "/internal/providers/exhausted", body)
