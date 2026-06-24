"""Write ``manifest.json`` into a job directory (T1.3a).

The manifest is the job's on-disk record: the authoritative ``Job`` (supplied by
the caller — the control plane in production, the local-runner in T1.4) plus the
``worker_meta`` the kernel assembles as it runs (ffprobe blob, the AIGC embed
method recorded by T1.3b, the supply-chain ``ModelRef`` pins recorded by T1.3g).

``Job`` / ``Manifest`` / ``WorkerMeta`` are plain ``ovt_schemas`` data contracts
(the single source of truth, STEP0-B) — reading them does not cross the
autodub-core hard boundary (that lint forbids gateway/control-plane *code*, not
the shared schema package).
"""

from __future__ import annotations

from pathlib import Path

from ovt_schemas.contracts import Job, Manifest, WorkerMeta

from .config import JobPaths
from .jsonio import write_json


def write_manifest(paths: JobPaths, job: Job, worker_meta: WorkerMeta | None = None) -> Path:
    """Write ``manifest.json`` = ``Manifest(job, worker_meta)`` into the job dir.

    ``worker_meta`` defaults to an empty record; T1.3b/T1.3g pass an assembled one
    (AIGC embed method / model sha256 pins). Atomic write (jsonio), so a crash
    never leaves a partial manifest.
    """
    paths.ensure()
    manifest = Manifest(job=job, worker_meta=worker_meta or WorkerMeta())
    write_json(paths.manifest, manifest.model_dump())
    return paths.manifest
