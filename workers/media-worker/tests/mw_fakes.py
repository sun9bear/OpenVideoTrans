"""In-memory fakes + a valid-Job factory for the media-worker tests.

The worker's two outbound ports — the control-plane HTTP client and the R2 storage
client — are injected, so these fakes stand in for them and let the claim loop /
heartbeat / cleanup be exercised with no live control plane or R2.
"""
from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path

from media_worker.config import WorkerConfig
from media_worker.control_plane import (
    Claim,
    ControlPlaneError,
    ProgressTelemetry,
    ProviderSnapshot,
    StaleClaimError,
)
from media_worker.storage import SourceTooLargeError
from media_worker.worker import artifact_key
from ovt_schemas import AigcMarking, Job, JobArtifacts, JobPlan

# Production-shaped knobs (30s heartbeat / 180s lease), mirroring the control-plane defaults.
TEST_CONFIG = WorkerConfig(
    heartbeat_interval_sec=30.0,
    lease_ttl_sec=180.0,
    job_hard_timeout_sec=2700.0,
    max_attempts=2,
)

# A fast heartbeat so tests that need the independent timer to actually tick don't wait 30s.
FAST_CONFIG = WorkerConfig(
    heartbeat_interval_sec=0.02,
    lease_ttl_sec=1.0,
    job_hard_timeout_sec=5.0,
    max_attempts=2,
)


def make_job(**overrides: object) -> Job:
    """Build a minimal valid Job (same shape as packages/schemas tests) with overrides applied."""
    base = Job(
        job_id="job_x",
        anon_or_user_id="anon_abc",
        tier="tier1",
        status="running",
        source_type="upload",
        upload_session_id="us_abc",
        target_lang="zh-Hans",
        output_mode="dub_only",
        subtitle_delivery="srt",
        subtitle_lang="target",
        plan=JobPlan(asr="auto", mt="auto", tts="auto"),
        settings_version=1,
        aigc_marking=AigcMarking(enabled=True, implicit=True, explicit=True, form="tail_notice"),
        priority=0,
        enqueue_at=1_700_000_000_000,
        deadline_at=1_700_003_600_000,
        created_at=1_700_000_000_000,
        expires_at=1_700_086_400_000,
        artifacts=JobArtifacts(),
        attempt=1,
        claim_version=1,
        counted_job=False,
        counted_minutes=False,
        refunded=False,
    )
    return base.model_copy(update=overrides) if overrides else base


class FakeControlPlane:
    """Records every worker->control-plane call; thread-safe (heartbeat runs on its own thread)."""

    def __init__(
        self,
        *,
        config: WorkerConfig = TEST_CONFIG,
        claims: list[Claim] | None = None,
        stale: bool = False,
        complete_error: bool = False,
        config_error: bool = False,
        fail_error: bool = False,
        availability: ProviderSnapshot | None = None,
    ) -> None:
        self._config = config
        self._claims = list(claims or [])
        self._stale = stale
        self._complete_error = complete_error
        self._config_error = config_error
        self._fail_error = fail_error
        self._availability = availability or ProviderSnapshot(now_ms=0, exhausted_until={})
        self._lock = threading.Lock()
        self.heartbeats: list[tuple[str, int, str | None]] = []
        # FREE-POOL (M2-CLOSE): every report_provider_exhausted call, recorded for assertions.
        self.exhausted_reports: list[tuple[str, int, str | None]] = []
        # OBS (#24): the telemetry passed alongside each heartbeat, recorded in parallel so the
        # existing 3-tuple `heartbeats` unpacks stay intact.
        self.telemetry: list[ProgressTelemetry | None] = []
        self.completed: list[tuple[str, int, dict[str, str]]] = []
        self.failed: list[tuple[str, int, str, str | None]] = []

    def get_config(self) -> WorkerConfig:
        if self._config_error:
            raise ControlPlaneError("config endpoint unavailable")
        return self._config

    def claim(self) -> Claim | None:
        with self._lock:
            return self._claims.pop(0) if self._claims else None

    def heartbeat(
        self,
        job_id: str,
        claim_version: int,
        *,
        stage: str | None = None,
        telemetry: ProgressTelemetry | None = None,
    ) -> None:
        with self._lock:
            self.heartbeats.append((job_id, claim_version, stage))
            self.telemetry.append(telemetry)
        if self._stale:
            raise StaleClaimError(f"job {job_id} claim {claim_version} superseded")

    def complete(self, job_id: str, claim_version: int, *, artifacts: Mapping[str, str]) -> None:
        if self._complete_error:
            raise ControlPlaneError(f"transient error completing {job_id}")
        with self._lock:
            self.completed.append((job_id, claim_version, dict(artifacts)))

    def fail(
        self,
        job_id: str,
        claim_version: int,
        *,
        error_code: str,
        error_detail: str | None = None,
    ) -> None:
        if self._fail_error:
            raise ControlPlaneError(f"transient error failing {job_id}")
        with self._lock:
            self.failed.append((job_id, claim_version, error_code, error_detail))

    def get_provider_availability(self) -> ProviderSnapshot:
        return self._availability

    def report_provider_exhausted(
        self, provider: str, *, reset_at_ms: int, reason: str | None = None
    ) -> None:
        with self._lock:
            self.exhausted_reports.append((provider, reset_at_ms, reason))

    @property
    def heartbeat_count(self) -> int:
        with self._lock:
            return len(self.heartbeats)


class FakeStorage:
    """In-memory object store standing in for the worker's R2 client."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects: dict[str, bytes] = dict(objects or {})
        self.uploads: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.downloads: list[str] = []

    def head(self, key: str) -> int | None:
        obj = self.objects.get(key)
        return len(obj) if obj is not None else None

    def download(self, key: str, *, max_bytes: int | None = None) -> bytes:
        self.downloads.append(key)
        try:
            data = self.objects[key]
        except KeyError as e:
            raise FileNotFoundError(key) from e
        if max_bytes is not None and len(data) > max_bytes:
            raise SourceTooLargeError(f"source exceeds the {max_bytes}-byte cap")
        return data

    def upload(self, key: str, data: bytes, *, content_type: str) -> None:
        self.objects[key] = data
        self.uploads.append((key, content_type))

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)


# A pass-through admitter for the stub copy-loop tests, which exercise the heartbeat / cleanup /
# completion paths on synthetic (non-media) bytes — the real ffprobe re-admission is covered
# separately in test_admission.py + the disguised-playlist integration test in test_worker.py.
def ALLOW_ADMIT(path: object, job: object, config: object) -> None:  # noqa: N802, ARG001
    return None


# A copy-loop artifact producer for the orchestration tests: writes the SOURCE bytes to each
# output_mode artifact (what the T2.2 stub did), so the heartbeat / cleanup / completion / timeout
# tests need no ffmpeg or real providers. The REAL producer (run_real_pipeline) is exercised in
# test_pipeline.py + `just dev`.
_COPY_ARTIFACTS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "subtitle_only": (("srt_key", "output.srt", "application/x-subrip"),),
    "dub_only": (("video_key", "output.mp4", "video/mp4"),),
    "both": (
        ("video_key", "output.mp4", "video/mp4"),
        ("srt_key", "output.srt", "application/x-subrip"),
    ),
}


def COPY_PRODUCE(  # noqa: N802
    _cp: object,
    storage: FakeStorage,
    claim: Claim,
    _workdir: Path | str,
    in_path: Path,
    **_kw: object,
) -> dict[str, str]:
    data = in_path.read_bytes()
    artifacts: dict[str, str] = {}
    for field, name, content_type in _COPY_ARTIFACTS[claim.job.output_mode]:
        key = artifact_key(claim.job.job_id, claim.claim_version, name)
        storage.upload(key, data, content_type=content_type)
        artifacts[field] = key
    return artifacts
