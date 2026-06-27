"""Entry point: ``python -m media_worker`` runs the claim loop against the control plane + R2.

Config from env (secrets are read from env ONLY — never logged or passed on a command line):
  OVT_CONTROL_PLANE_URL    base URL of the control plane
  OVT_INTERNAL_TOKEN       worker bootstrap shared secret (the /internal bearer)
  OVT_INTERNAL_TOKEN_NEXT  optional staged next bootstrap secret (zero-downtime rotation)
  OVT_WORKDIR              jobs scratch dir (default /var/lib/ovt/jobs)
  OVT_R2_ENDPOINT          optional NON-secret S3 base override for the local dev loop
                           (DEVLOOP #25); unset in prod ⇒ the real R2 host

The box holds ONLY the bootstrap secret(s) (SECRETS #21): the R2 storage creds and the free-provider
API keys are pulled from the control plane's /internal/credentials at startup and kept in memory —
never on the box disk. R2 creds drive the storage client; the provider creds are held (in `creds`,
alive for the worker's lifetime) for the FREE-POOL adapters to consume (routed).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .control_plane import HttpControlPlane
from .pipeline import inject_provider_env
from .storage import S3Storage
from .worker import run_forever

logger = logging.getLogger("media_worker")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    base_url = os.environ["OVT_CONTROL_PLANE_URL"]
    token = os.environ["OVT_INTERNAL_TOKEN"]
    next_token = os.environ.get("OVT_INTERNAL_TOKEN_NEXT") or None
    workdir = Path(os.environ.get("OVT_WORKDIR", "/var/lib/ovt/jobs"))
    # DEVLOOP (#25): NON-secret S3 endpoint override for the local dev loop (points at a local
    # S3 stub so the box needs no real R2). Unset in prod ⇒ the real R2 host; the box's egress
    # allowlist gates it regardless. R2 ACCESS creds still come from /internal/credentials.
    r2_endpoint = os.environ.get("OVT_R2_ENDPOINT") or None
    cp = HttpControlPlane(base_url, token, next_token=next_token)
    # Pull R2 + free-provider creds over the authed /internal channel and keep them in memory only.
    creds = cp.get_credentials()
    storage = S3Storage(creds.r2, endpoint=r2_endpoint)
    # Fold the pulled free-provider keys into the process env so provider-adapters' env-keyed
    # select() resolves them when the pipeline routes (M2-CLOSE). In-process env ONLY (SECRETS keeps
    # the keys off the box disk). Returns the configured NAMES (never values) for the startup log.
    configured = inject_provider_env(creds.providers)
    # Log provider NAMES only — never a key value.
    logger.info(
        "pulled worker credentials: storage configured; free providers: %s",
        ", ".join(configured) or "(none)",
    )
    run_forever(cp, storage, workdir_base=workdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
