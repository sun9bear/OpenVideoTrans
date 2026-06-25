"""R2 (S3-compatible) object client for the worker: signed GET (source) + PUT (artifact).

Stdlib urllib + SigV4 (see sigv4.py). R2 credentials come from the worker box's env (same names
as the control plane) and are used ONLY to sign — never logged, never placed in a URL or error.
"""
from __future__ import annotations

import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .sigv4 import sign_request

_REGION = "auto"  # R2 uses region "auto" for SigV4
_SERVICE = "s3"


class StorageError(RuntimeError):
    """An object-storage operation failed or storage is misconfigured."""


class Storage(Protocol):
    def download(self, key: str) -> bytes: ...
    def upload(self, key: str, data: bytes, *, content_type: str) -> None: ...


@dataclass(frozen=True)
class R2Settings:
    account_id: str
    bucket: str
    access_key_id: str
    secret_access_key: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> R2Settings:
        try:
            return cls(
                account_id=env["R2_ACCOUNT_ID"],
                bucket=env["R2_BUCKET"],
                access_key_id=env["R2_ACCESS_KEY_ID"],
                secret_access_key=env["R2_SECRET_ACCESS_KEY"],
            )
        except KeyError as e:
            # Name only — never echo the (possibly partially set) secret values.
            raise StorageError(f"missing R2 setting: {e.args[0]}") from None

    def url_for(self, key: str) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com/{self.bucket}/{key}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class S3Storage:
    """Storage backed by R2's S3 API. The opener/clock are injectable for tests."""

    def __init__(
        self,
        settings: R2Settings,
        *,
        opener: Any = None,
        clock: Callable[[], datetime] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._s = settings
        self._opener = opener if opener is not None else urllib.request.build_opener()
        self._clock = clock if clock is not None else _utcnow
        self._timeout = timeout

    def _sign(self, method: str, key: str, payload: bytes) -> tuple[str, dict[str, str]]:
        url = self._s.url_for(key)
        headers = sign_request(
            method=method,
            url=url,
            region=_REGION,
            service=_SERVICE,
            access_key=self._s.access_key_id,
            secret_key=self._s.secret_access_key,
            payload=payload,
            amz_datetime=self._clock(),
        )
        return url, headers

    def download(self, key: str) -> bytes:
        url, headers = self._sign("GET", key, b"")
        req = urllib.request.Request(url, method="GET")
        for name, value in headers.items():
            req.add_header(name, value)
        with self._opener.open(req, timeout=self._timeout) as resp:
            data: bytes = resp.read()
        return data

    def upload(self, key: str, data: bytes, *, content_type: str) -> None:
        url, headers = self._sign("PUT", key, data)
        req = urllib.request.Request(url, data=data, method="PUT")
        req.add_header("Content-Type", content_type)  # sent unsigned (not part of the SigV4 set)
        for name, value in headers.items():
            req.add_header(name, value)
        with self._opener.open(req, timeout=self._timeout) as resp:
            resp.read()
