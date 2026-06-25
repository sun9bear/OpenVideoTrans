"""The media worker's claim loop (T2.2 stub).

claim -> download source from R2 -> copy it straight to the output artifact (NO real pipeline;
that is M2-CLOSE) -> complete. A standalone heartbeat thread renews the lease every
`heartbeat_interval_sec` independent of stage progress (plan §6: one stage can outlast the lease).
Each job's workdir is namespace-isolated + path-contained and always removed in a finally; orphan
workdirs from a previous crash are cleared at startup.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

from autodub_core.isolation import ensure_within, safe_component
from ovt_schemas import Job

from .config import WorkerConfig
from .control_plane import Claim, ControlPlane, StaleClaimError
from .storage import Storage

logger = logging.getLogger("media_worker")

# Per output_mode: (Job.artifacts field, output filename, content-type). The stub copies the source
# bytes into this single artifact; the control plane's complete() requires the claim_version prefix.
_ARTIFACT_BY_MODE: dict[str, tuple[str, str, str]] = {
    "subtitle_only": ("srt_key", "output.srt", "application/x-subrip"),
    "dub_only": ("video_key", "output.mp4", "video/mp4"),
    "both": ("video_key", "output.mp4", "video/mp4"),
}


def source_key_for(job: Job) -> str:
    """R2 key of the uploaded source.

    Mirrors the control plane's signUpload convention (apps/control-plane/src/uploads.ts):
    ``uploads/${upload_session_id}``. The schemas Job intentionally omits source_key, so the worker
    re-derives it from upload_session_id (a deterministic, no-client-path server convention).
    """
    return f"uploads/{job.upload_session_id}"


def artifact_key(job_id: str, claim_version: int, name: str) -> str:
    """Artifact R2 key.

    MUST match the control plane's complete() guard ``artifacts/{job_id}/{claim_version}/...`` so a
    reclaimed stale worker (a different claim_version) cannot overwrite the winning artifact path.
    """
    return f"artifacts/{job_id}/{claim_version}/{name}"


def job_workdir(base: Path | str, job_id: str) -> Path:
    """Namespace-isolated, path-contained on-disk dir for one job: ``base/<job_id>``.

    job_id is validated as a single safe component and the result is contained within base (reuses
    autodub-core's T1.3a guards) — no traversal / reserved-name / separator escape.
    """
    base_path = Path(base)
    return ensure_within(base_path, base_path / safe_component(job_id, label="job_id"))


def clean_orphan_workdirs(base: Path | str) -> list[str]:
    """Remove every child dir under base (orphans from a crashed run). Returns the removed names."""
    base_path = Path(base)
    if not base_path.exists():
        return []
    removed: list[str] = []
    for child in base_path.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
            removed.append(child.name)
    return removed


class Heartbeat:
    """Independent timer thread that renews a job's lease until stopped or the lease is lost."""

    def __init__(
        self,
        cp: ControlPlane,
        job_id: str,
        claim_version: int,
        *,
        interval_sec: float,
        stage: str = "processing",
        on_lost: Callable[[], None] | None = None,
    ) -> None:
        self._cp = cp
        self._job_id = job_id
        self._claim_version = claim_version
        self._interval = interval_sec
        self._stage = stage
        self._on_lost = on_lost
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"heartbeat-{self._job_id}", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        # wait() returns True only when stop() is set, so the loop ticks once per interval and exits
        # promptly on stop. Renewal is driven purely by this timer, never by stage edges (plan §6).
        while not self._stop.wait(self._interval):
            try:
                self._cp.heartbeat(self._job_id, self._claim_version, stage=self._stage)
            except StaleClaimError:
                # Lease reclaimed -> this worker is stale; stop renewing. Exactly-once is enforced
                # server-side (claim_version-gated complete/fail), so we needn't race the new owner.
                logger.warning("job %s lease lost (stale claim); stopping heartbeat", self._job_id)
                if self._on_lost is not None:
                    self._on_lost()
                return
            except Exception:
                # Transient control-plane/network blip: keep the lease alive by retrying next tick
                # rather than abandoning the job. Log the job id only (no exception text: secret
                # hygiene — it could carry a URL/host).
                logger.warning("job %s heartbeat renew failed; will retry", self._job_id)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)


def process_job(
    cp: ControlPlane,
    storage: Storage,
    claim: Claim,
    *,
    workdir_base: Path | str,
    config: WorkerConfig,
) -> None:
    """Run one claimed job through the stub: copy source -> artifact -> complete (or fail)."""
    job = claim.job
    claim_version = claim.claim_version
    job_id = job.job_id
    workdir = job_workdir(workdir_base, job_id)
    workdir.mkdir(parents=True, exist_ok=True)
    heartbeat = Heartbeat(cp, job_id, claim_version, interval_sec=config.heartbeat_interval_sec)
    heartbeat.start()
    try:
        field, name, content_type = _ARTIFACT_BY_MODE.get(
            job.output_mode, _ARTIFACT_BY_MODE["dub_only"]
        )
        source = storage.download(source_key_for(job))
        in_path = workdir / "input"
        in_path.write_bytes(source)
        # STUB: the output IS the input, copied straight through (no transcode/translate/tts).
        out_path = workdir / name
        shutil.copyfile(in_path, out_path)
        key = artifact_key(job_id, claim_version, name)
        storage.upload(key, out_path.read_bytes(), content_type=content_type)
        cp.complete(job_id, claim_version, artifacts={field: key})
        logger.info("job %s completed (stub copy -> %s)", job_id, key)
    except Exception:
        # Fail closed. error_detail is omitted on purpose: exception text can carry a path/URL,
        # so it must never reach the control plane / user (the stable error_code is enough).
        cp.fail(job_id, claim_version, error_code="internal_error")
        logger.warning("job %s failed (stub)", job_id)
    finally:
        heartbeat.stop()
        shutil.rmtree(workdir, ignore_errors=True)


def run_once(
    cp: ControlPlane,
    storage: Storage,
    *,
    workdir_base: Path | str,
    config: WorkerConfig,
) -> str | None:
    """Claim one job and process it. Returns the job_id, or None if nothing was claimable."""
    claim = cp.claim()
    if claim is None:
        return None
    process_job(cp, storage, claim, workdir_base=workdir_base, config=config)
    return claim.job.job_id


def run_forever(
    cp: ControlPlane,
    storage: Storage,
    *,
    workdir_base: Path | str,
    config: WorkerConfig | None = None,
    poll_idle_sec: float = 2.0,
    stop_event: threading.Event | None = None,
) -> None:
    """Long-poll the claim loop until stopped. Clears orphan dirs at startup (crash recovery)."""
    base = Path(workdir_base)
    base.mkdir(parents=True, exist_ok=True)
    cleared = clean_orphan_workdirs(base)
    if cleared:
        logger.info("cleared %d orphan workdir(s) at startup", len(cleared))
    cfg = config if config is not None else cp.get_config()
    while stop_event is None or not stop_event.is_set():
        try:
            job_id = run_once(cp, storage, workdir_base=base, config=cfg)
        except Exception:
            # A single iteration's unexpected error (e.g. a control-plane blip on claim) must not
            # kill the long-running worker. Log generically — no exception text (secret hygiene).
            logger.warning("worker iteration failed; continuing")
            job_id = None
        if job_id is None:
            if stop_event is not None:
                if stop_event.wait(poll_idle_sec):
                    break
            else:
                time.sleep(poll_idle_sec)
