"""DEVLOOP (#25) worker↔store seam: the S3Storage OVT_R2_ENDPOINT override lets the REAL worker
storage client (real SigV4 signing) drive a local S3 stub, so the dev loop needs no cloud R2.

These run in CI (the `py` job): the seam over real HTTP + real signing, no node / ffmpeg needed.
The full 3-process upload→claim→complete loop is `dev/dev_loop.py` (`just dev`), run locally.
"""
from __future__ import annotations

import sys
from pathlib import Path

from media_worker.storage import R2Settings, S3Storage

# local_s3 lives under dev/ (not a package); put it on the path the way the orchestrator does.
_DEV = Path(__file__).resolve().parents[3] / "dev"
if str(_DEV) not in sys.path:
    sys.path.insert(0, str(_DEV))

from local_s3 import LocalS3  # noqa: E402

_SETTINGS = R2Settings(
    account_id="acct-dev",
    bucket="ovt-media",
    access_key_id="AKIDEV",
    secret_access_key="secret-dev",
)


def test_endpoint_override_builds_path_style_url() -> None:
    storage = S3Storage(_SETTINGS, endpoint="http://127.0.0.1:9000")
    assert storage._url_for("uploads/us_abc") == "http://127.0.0.1:9000/ovt-media/uploads/us_abc"


def test_no_endpoint_targets_real_r2_host() -> None:
    storage = S3Storage(_SETTINGS)
    assert storage._url_for("uploads/us_abc") == (
        "https://acct-dev.r2.cloudflarestorage.com/ovt-media/uploads/us_abc"
    )


def test_trailing_slash_in_endpoint_is_normalized() -> None:
    storage = S3Storage(_SETTINGS, endpoint="http://127.0.0.1:9000/")
    assert storage._url_for("k") == "http://127.0.0.1:9000/ovt-media/k"


def test_round_trip_put_head_get_delete_against_local_stub() -> None:
    # Real worker storage client (real SigV4 signing) against the real stub over HTTP. The stub
    # ignores the signature (local only), so this proves the signed-request plumbing + the
    # endpoint override end to end, exactly as the worker will hit the dev stub.
    with LocalS3() as stub:
        storage = S3Storage(_SETTINGS, endpoint=stub.url)
        key = "uploads/us_round_trip"
        payload = b"\x00\x01video-bytes\xff"

        storage.upload(key, payload, content_type="video/mp4")
        assert stub.object_count() == 1
        assert storage.head(key) == len(payload)
        assert storage.download(key) == payload

        # max_bytes cap still applies on this path too (the worker's HEAD->GET TOCTOU guard).
        assert storage.download(key, max_bytes=len(payload)) == payload

        storage.delete(key)
        assert storage.head(key) is None
        assert stub.object_count() == 0


def test_head_missing_object_is_none() -> None:
    with LocalS3() as stub:
        storage = S3Storage(_SETTINGS, endpoint=stub.url)
        assert storage.head("uploads/never-put") is None
