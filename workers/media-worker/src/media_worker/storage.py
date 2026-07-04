"""R2 (S3-compatible) object client for the worker: signed GET (source) + PUT (artifact).

Stdlib urllib + SigV4 (see sigv4.py). R2 credentials come from the worker box's env (same names
as the control plane) and are used ONLY to sign — never logged, never placed in a URL or error.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .sigv4 import sign_request

# Cloudflare's edge blocks the default "Python-urllib/x.y" User-Agent (403); R2 is behind Cloudflare
# too, so send a normal app UA on every object request. Not part of the SigV4 SignedHeaders set.
USER_AGENT = "OpenVideoTrans-Worker/1.0"

_REGION = "auto"  # R2 uses region "auto" for SigV4
_SERVICE = "s3"


class StorageError(RuntimeError):
    """An object-storage operation failed or storage is misconfigured."""


class SourceTooLargeError(StorageError):
    """A bounded download read more than the byte cap — the object exceeds max_upload_bytes (e.g. a
    post-HEAD swap to an oversized object). Surfaced so the caller rejects upload_too_large."""


class Storage(Protocol):
    def head(self, key: str) -> int | None: ...
    def download(self, key: str, *, max_bytes: int | None = None) -> bytes: ...
    def upload(self, key: str, data: bytes, *, content_type: str) -> None: ...
    def delete(self, key: str) -> None: ...


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
        endpoint: str | None = None,
        opener: Any = None,
        clock: Callable[[], datetime] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._s = settings
        # DEVLOOP (#25): a NON-secret S3 base override (scheme://host[:port]) for the dev loop,
        # from OVT_R2_ENDPOINT. When set the worker signs + sends to a local S3 stub (path-style
        # {bucket}/{key}); when None (prod) it targets the real R2 host exactly as before. The box's
        # default-drop egress (nftables) blocks any non-R2 host in prod regardless of this value.
        self._endpoint = endpoint.rstrip("/") if endpoint else None
        self._opener = opener if opener is not None else urllib.request.build_opener()
        self._clock = clock if clock is not None else _utcnow
        self._timeout = timeout

    def _url_for(self, key: str) -> str:
        if self._endpoint:
            return f"{self._endpoint}/{self._s.bucket}/{key}"
        return self._s.url_for(key)

    def _sign(
        self, method: str, key: str, payload: bytes, extra: Mapping[str, str] | None = None
    ) -> tuple[str, dict[str, str]]:
        url = self._url_for(key)
        headers = sign_request(
            method=method,
            url=url,
            region=_REGION,
            service=_SERVICE,
            access_key=self._s.access_key_id,
            secret_key=self._s.secret_access_key,
            payload=payload,
            amz_datetime=self._clock(),
            extra_headers=extra,
        )
        return url, headers

    def head(self, key: str) -> int | None:
        # Signed HEAD -> the object's Content-Length, so the worker can reject an oversized source
        # BEFORE buffering its body (the post-HEAD-swap DoS). None for a missing object (404); R2
        # always returns Content-Length for an existing object. Transport errors propagate.
        url, headers = self._sign("HEAD", key, b"")
        req = urllib.request.Request(url, method="HEAD")
        for name, value in headers.items():
            req.add_header(name, value)
        req.add_header("User-Agent", USER_AGENT)
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                length = resp.headers.get("Content-Length")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise StorageError(f"HEAD {key} -> {e.code}") from None
        return int(length) if length is not None else None

    def download(self, key: str, *, max_bytes: int | None = None) -> bytes:
        # With max_bytes set, read at most max_bytes+1 and reject if it overflows: this CAPS the
        # buffered body even if the object was swapped to an oversized one AFTER the HEAD precheck
        # (the TOCTOU race the HEAD alone can't close), so memory/disk stays bounded by the cap.
        url, headers = self._sign("GET", key, b"")
        req = urllib.request.Request(url, method="GET")
        for name, value in headers.items():
            req.add_header(name, value)
        req.add_header("User-Agent", USER_AGENT)
        with self._opener.open(req, timeout=self._timeout) as resp:
            if max_bytes is None:
                return resp.read()
            data: bytes = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise SourceTooLargeError(f"source exceeds the {max_bytes}-byte cap")
        return data

    def upload(self, key: str, data: bytes, *, content_type: str) -> None:
        # Content-Type joins the signed header set (its integrity is covered by the signature).
        url, headers = self._sign("PUT", key, data, {"Content-Type": content_type})
        req = urllib.request.Request(url, data=data, method="PUT")
        for name, value in headers.items():
            req.add_header(name, value)
        req.add_header("User-Agent", USER_AGENT)
        with self._opener.open(req, timeout=self._timeout) as resp:
            resp.read()

    def delete(self, key: str) -> None:
        # Signed DELETE of an object (the worker removes a source rejected by ffprobe re-admission).
        url, headers = self._sign("DELETE", key, b"")
        req = urllib.request.Request(url, method="DELETE")
        for name, value in headers.items():
            req.add_header(name, value)
        req.add_header("User-Agent", USER_AGENT)
        with self._opener.open(req, timeout=self._timeout) as resp:
            resp.read()
