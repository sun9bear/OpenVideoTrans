"""OBS (#24) worker side: the /progress (heartbeat) telemetry contract is redaction-safe by
construction. ProgressTelemetry carries NAMES + INTS only — it structurally cannot hold a provider
key, raw body, plaintext IP, or original filename — and the control plane re-validates against the
same allowlist before storing (apps/control-plane/src/obs.ts parseProgressMeta)."""
from __future__ import annotations

import dataclasses
import json
import time

from media_worker.control_plane import ProgressTelemetry, build_progress_body
from media_worker.worker import Heartbeat
from mw_fakes import FAST_CONFIG, FakeControlPlane

_ALLOWED = {
    "claim_version",
    "stage",
    "stage_elapsed_ms",
    "provider",
    "chunk_index",
    "chunk_count",
    "free_pool_result",
}


def test_progress_telemetry_is_names_and_ints_only() -> None:
    # The dataclass has NO field that could carry a filename / IP / key / raw body — the redaction
    # guarantee is the field SET, enforced here so a future field addition is a conscious decision.
    fields = {f.name for f in dataclasses.fields(ProgressTelemetry)}
    assert fields == {
        "stage_elapsed_ms",
        "provider",
        "chunk_index",
        "chunk_count",
        "free_pool_result",
    }


def test_progress_body_only_allowlisted_fields_and_omits_none() -> None:
    body = build_progress_body(7, "asr", ProgressTelemetry(stage_elapsed_ms=1500, provider="groq"))
    assert set(body) <= _ALLOWED
    assert body["claim_version"] == 7
    assert body["stage"] == "asr"
    assert body["stage_elapsed_ms"] == 1500
    assert body["provider"] == "groq"
    # stub-forced Nones are OMITTED, not serialized as null
    assert "chunk_index" not in body
    assert "chunk_count" not in body
    assert "free_pool_result" not in body


def test_progress_body_serialization_is_redaction_safe() -> None:
    body = build_progress_body(
        1,
        "tts",
        ProgressTelemetry(
            stage_elapsed_ms=10,
            provider="deepl",
            chunk_index=0,
            chunk_count=3,
            free_pool_result="ok",
        ),
    )
    blob = json.dumps(body)
    leaks = ["/work", ".mp4", ".mov", "10.0.0", "192.168", "AKIA", "Bearer", "api_key", "filename"]
    for leak in leaks:
        assert leak not in blob


def test_progress_body_no_telemetry_is_just_claim_version_and_stage() -> None:
    assert build_progress_body(3, None, None) == {"claim_version": 3}
    assert build_progress_body(3, "claimed", None) == {"claim_version": 3, "stage": "claimed"}


def test_heartbeat_thread_reports_stage_timing_telemetry() -> None:
    # The independent heartbeat timer folds a stage_elapsed_ms into each /progress report. The stub
    # selects no provider and never touches the free pool, so provider / chunk_* / free_pool_result
    # are None until the real pipeline (M2-CLOSE) — the CONTRACT ships now, the VALUES land later.
    cp = FakeControlPlane(config=FAST_CONFIG)
    hb = Heartbeat(cp, "job_x", 7, interval_sec=0.02)
    hb.start()
    deadline = time.monotonic() + 3.0
    while len(cp.telemetry) < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    hb.stop()
    assert cp.telemetry, "expected at least one telemetry report"
    sample = cp.telemetry[0]
    assert sample is not None
    assert sample.stage_elapsed_ms is not None and sample.stage_elapsed_ms >= 0
    assert sample.provider is None
    assert sample.chunk_count is None
    assert sample.free_pool_result is None
