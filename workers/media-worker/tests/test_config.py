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


def test_parse_config_honors_each_per_mode_duration_cap() -> None:
    # the control plane advertises maxVideoDurationMs per output_mode; the worker must honor `both`
    # INDEPENDENTLY, not collapse it onto the dub cap (CodeX bot P2).
    cfg = parse_config(
        {"maxVideoDurationMs": {"subtitle_only": 1_800_000, "dub_only": 300_000, "both": 600_000}}
    )
    assert cfg.duration_cap_sec("subtitle_only") == 1800
    assert cfg.duration_cap_sec("dub_only") == 300
    assert cfg.duration_cap_sec("both") == 600  # honored, not forced to the 300s dub cap
    assert cfg.duration_cap_sec("nonsense") == 300  # unknown -> tightest configured (fail-safe)


def test_parse_config_falls_back_to_defaults_when_missing() -> None:
    assert parse_config({}) == DEFAULT_CONFIG


def test_default_config_mirrors_control_plane() -> None:
    # The control-plane DEFAULT_CONFIG: 30s heartbeat, 180s lease, 2 attempts. The worker's offline
    # defaults must match so a config-fetch failure is safe.
    assert DEFAULT_CONFIG.heartbeat_interval_sec == 30.0
    assert DEFAULT_CONFIG.lease_ttl_sec == 180.0
    assert DEFAULT_CONFIG.job_hard_timeout_sec == 2700.0
    assert DEFAULT_CONFIG.max_attempts == 2
