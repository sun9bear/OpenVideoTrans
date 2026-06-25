"""Minimal AWS SigV4 request signer (Authorization-header variant) for R2's S3 API.

Zero dependencies (hmac / hashlib / urllib only), pinned by an AWS known-answer vector in the
tests. Used by storage.S3Storage to sign source GETs and artifact PUTs. The secret key is used
ONLY in the HMAC chain here — it never appears in a URL, a returned header value, a log, or an
error message.
"""
from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from collections.abc import Mapping
from datetime import datetime

_ALGORITHM = "AWS4-HMAC-SHA256"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _canonical_query(query: str) -> str:
    if not query:
        return ""
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    encoded = [
        (urllib.parse.quote(k, safe="-_.~"), urllib.parse.quote(v, safe="-_.~")) for k, v in pairs
    ]
    return "&".join(f"{k}={v}" for k, v in sorted(encoded))


def sign_request(
    *,
    method: str,
    url: str,
    region: str,
    service: str,
    access_key: str,
    secret_key: str,
    payload: bytes,
    amz_datetime: datetime,
    extra_headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the headers to add to the request (Authorization + x-amz-* + any extra_headers)."""
    parsed = urllib.parse.urlsplit(url)
    canonical_uri = urllib.parse.quote(parsed.path or "/", safe="/-_.~")
    canonical_qs = _canonical_query(parsed.query)
    amz_date = amz_datetime.strftime("%Y%m%dT%H%M%SZ")
    datestamp = amz_datetime.strftime("%Y%m%d")
    payload_hash = _sha256_hex(payload)

    # Canonical (signed) headers, lowercased + sorted: host + the two x-amz-* are always signed;
    # any extra_headers (e.g. Range) join them.
    signed: dict[str, str] = {k.lower(): v.strip() for k, v in (extra_headers or {}).items()}
    signed["host"] = parsed.netloc
    signed["x-amz-date"] = amz_date
    signed["x-amz-content-sha256"] = payload_hash
    signed_headers = ";".join(sorted(signed))
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in sorted(signed))

    canonical_request = "\n".join(
        [method, canonical_uri, canonical_qs, canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [_ALGORITHM, amz_date, scope, _sha256_hex(canonical_request.encode())]
    )
    k_date = _sign(f"AWS4{secret_key}".encode(), datestamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (
        f"{_ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    # Headers to actually send. `host` is omitted — urllib sets Host itself from the URL (matching
    # what we signed) — so we return only the x-amz-* values, Authorization, and any extra headers.
    out: dict[str, str] = dict(extra_headers or {})
    out["x-amz-date"] = amz_date
    out["x-amz-content-sha256"] = payload_hash
    out["Authorization"] = authorization
    return out
