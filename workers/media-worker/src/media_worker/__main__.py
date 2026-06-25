"""Entry point: ``python -m media_worker`` runs the claim loop against the control plane + R2.

Config from env (secrets are read from env ONLY — never logged or passed on a command line):
  OVT_CONTROL_PLANE_URL   base URL of the control plane
  OVT_INTERNAL_TOKEN      worker bearer token (the /internal shared secret)
  OVT_WORKDIR             jobs scratch dir (default /var/lib/ovt/jobs)
  R2_ACCOUNT_ID / R2_BUCKET / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY   R2 S3 credentials
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .control_plane import HttpControlPlane
from .storage import R2Settings, S3Storage
from .worker import run_forever


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    base_url = os.environ["OVT_CONTROL_PLANE_URL"]
    token = os.environ["OVT_INTERNAL_TOKEN"]
    workdir = Path(os.environ.get("OVT_WORKDIR", "/var/lib/ovt/jobs"))
    cp = HttpControlPlane(base_url, token)
    storage = S3Storage(R2Settings.from_env(os.environ))
    run_forever(cp, storage, workdir_base=workdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
