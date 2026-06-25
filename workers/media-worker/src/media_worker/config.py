"""Worker runtime config: the subset of /internal/config the worker acts on.

The control plane returns RuntimeConfig with camelCase keys (apps/control-plane/src/config.ts):
millisecond time fields, a byte count for the upload cap, and a per-output_mode duration-cap map.
parse_config converts ms -> seconds and falls back to defaults for any missing key so a config-fetch
hiccup is safe (these are non-security operational values; CFG-GUARD owns the authoritative
settings). The lease/heartbeat knobs drive the claim loop; the upload-size + per-mode duration caps
drive the worker's ffprobe re-admission (T2.4).
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
    # T2.4 re-admission caps. max_upload_bytes re-checks the ACTUAL downloaded bytes (closes the
    # post-HEAD swap). One cap PER output_mode — the control plane advertises maxVideoDurationMs as
    # a per-mode map, so the worker honors each independently (a CFG-GUARD/KV change to `both` must
    # NOT be silently collapsed onto the dub cap). Defaults split by binding: subtitle-only by cloud
    # ASR/MT, dub/both by the tighter TTS wall-time.
    max_upload_bytes: int = 500 * 1024 * 1024
    max_video_duration_subtitle_sec: int = 1800  # subtitle_only (cloud ASR/MT bound)
    max_video_duration_dub_sec: int = 300  # dub_only (TTS wall-time bound)
    max_video_duration_both_sec: int = 300  # both (subtitle + dub; TTS wall-time bound)

    def duration_cap_sec(self, output_mode: str) -> int:
        """The hard duration cap (seconds) for an output_mode (plan §13), honoring each mode's
        configured value. An unknown mode falls back to the tightest configured cap (fail-safe)
        rather than an unbounded default."""
        caps = {
            "subtitle_only": self.max_video_duration_subtitle_sec,
            "dub_only": self.max_video_duration_dub_sec,
            "both": self.max_video_duration_both_sec,
        }
        return caps.get(output_mode, min(caps.values()))


# Mirrors the control-plane DEFAULT_CONFIG (30s heartbeat / 180s lease / 45m cap / 2 attempts /
# 500 MiB upload cap / 5m dub + 30m subtitle duration caps).
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

    def duration_ms_to_sec(mode: str, default_sec: int) -> int:
        caps = payload.get("maxVideoDurationMs")
        if not isinstance(caps, Mapping):
            return default_sec
        value = caps.get(mode)
        return default_sec if value is None else int(int(value) / 1000)

    return WorkerConfig(
        heartbeat_interval_sec=secs("heartbeatIntervalMs", DEFAULT_CONFIG.heartbeat_interval_sec),
        lease_ttl_sec=secs("leaseTtlMs", DEFAULT_CONFIG.lease_ttl_sec),
        job_hard_timeout_sec=secs("jobHardTimeoutMs", DEFAULT_CONFIG.job_hard_timeout_sec),
        max_attempts=count("maxAttempts", DEFAULT_CONFIG.max_attempts),
        max_upload_bytes=count("maxUploadBytes", DEFAULT_CONFIG.max_upload_bytes),
        max_video_duration_subtitle_sec=duration_ms_to_sec(
            "subtitle_only", DEFAULT_CONFIG.max_video_duration_subtitle_sec
        ),
        max_video_duration_dub_sec=duration_ms_to_sec(
            "dub_only", DEFAULT_CONFIG.max_video_duration_dub_sec
        ),
        max_video_duration_both_sec=duration_ms_to_sec(
            "both", DEFAULT_CONFIG.max_video_duration_both_sec
        ),
    )
