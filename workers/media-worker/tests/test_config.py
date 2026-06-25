from __future__ import annotations

from media_worker.config import DEFAULT_CONFIG, parse_config


def test_parse_config_converts_ms_to_seconds() -> None:
    cfg = parse_config(
        {
            "heartbeatIntervalMs": 30000,
            "leaseTtlMs": 180000,
            "jobHardTimeoutMs": 2700000,
            "maxAttempts": 2,
            # extra keys returned by /internal/config are ignored
            "maxUploadBytes": 524288000,
            "allowedUploadTypes": ["video/mp4"],
        }
    )
    assert cfg.heartbeat_interval_sec == 30.0
    assert cfg.lease_ttl_sec == 180.0
    assert cfg.job_hard_timeout_sec == 2700.0
    assert cfg.max_attempts == 2


def test_parse_config_falls_back_to_defaults_when_missing() -> None:
    assert parse_config({}) == DEFAULT_CONFIG


def test_default_config_mirrors_control_plane() -> None:
    # The control-plane DEFAULT_CONFIG: 30s heartbeat, 180s lease, 2 attempts. The worker's offline
    # defaults must match so a config-fetch failure is safe.
    assert DEFAULT_CONFIG.heartbeat_interval_sec == 30.0
    assert DEFAULT_CONFIG.lease_ttl_sec == 180.0
    assert DEFAULT_CONFIG.job_hard_timeout_sec == 2700.0
    assert DEFAULT_CONFIG.max_attempts == 2
