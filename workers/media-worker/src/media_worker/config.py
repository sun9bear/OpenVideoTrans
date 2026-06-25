"""Worker runtime config: the subset of /internal/config the worker acts on.

The control plane returns RuntimeConfig with camelCase, millisecond keys
(apps/control-plane/src/config.ts). The worker only needs the lease/heartbeat knobs;
parse_config converts ms -> seconds and falls back to defaults for any missing key so a
config-fetch hiccup is safe (these are non-security operational values; CFG-GUARD owns the
authoritative settings).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkerConfig:
    heartbeat_interval_sec: float
    lease_ttl_sec: float
    job_hard_timeout_sec: float
    max_attempts: int


# Mirrors the control-plane DEFAULT_CONFIG (30s heartbeat / 180s lease / 45m cap / 2 attempts).
DEFAULT_CONFIG = WorkerConfig(
    heartbeat_interval_sec=30.0,
    lease_ttl_sec=180.0,
    job_hard_timeout_sec=2700.0,
    max_attempts=2,
)


def parse_config(payload: Mapping[str, Any]) -> WorkerConfig:
    """Parse a /internal/config response into a WorkerConfig (ms -> s; missing -> default)."""

    def secs(key: str, default: float) -> float:
        value = payload.get(key)
        return default if value is None else float(value) / 1000.0

    def count(key: str, default: int) -> int:
        value = payload.get(key)
        return default if value is None else int(value)

    return WorkerConfig(
        heartbeat_interval_sec=secs("heartbeatIntervalMs", DEFAULT_CONFIG.heartbeat_interval_sec),
        lease_ttl_sec=secs("leaseTtlMs", DEFAULT_CONFIG.lease_ttl_sec),
        job_hard_timeout_sec=secs("jobHardTimeoutMs", DEFAULT_CONFIG.job_hard_timeout_sec),
        max_attempts=count("maxAttempts", DEFAULT_CONFIG.max_attempts),
    )
