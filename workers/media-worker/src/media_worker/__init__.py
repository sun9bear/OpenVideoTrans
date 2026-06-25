"""media-worker — the OpenVideoTrans media worker.

T2.2 ships the *stub*: claim a job, copy the uploaded source straight to the output artifact
(no real pipeline), and complete it — with an independent heartbeat lease-renewal and try/finally
workdir cleanup. M2-CLOSE swaps the stub copy for the real autodub-core pipeline.
"""
from __future__ import annotations

from .config import DEFAULT_CONFIG, WorkerConfig, parse_config
from .control_plane import (
    Claim,
    ControlPlane,
    ControlPlaneError,
    HttpControlPlane,
    StaleClaimError,
)
from .storage import R2Settings, S3Storage, Storage, StorageError
from .worker import (
    Heartbeat,
    artifact_key,
    clean_orphan_workdirs,
    job_workdir,
    process_job,
    run_forever,
    run_once,
    source_key_for,
)

__version__ = "0.0.1"

__all__ = [
    "DEFAULT_CONFIG",
    "WorkerConfig",
    "parse_config",
    "Claim",
    "ControlPlane",
    "ControlPlaneError",
    "HttpControlPlane",
    "StaleClaimError",
    "R2Settings",
    "S3Storage",
    "Storage",
    "StorageError",
    "Heartbeat",
    "artifact_key",
    "clean_orphan_workdirs",
    "job_workdir",
    "process_job",
    "run_forever",
    "run_once",
    "source_key_for",
]
