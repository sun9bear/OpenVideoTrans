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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from autodub_core.isolation import ensure_within, safe_component
from ovt_schemas import Job
from provider_adapters import LanguageError

from .admission import SourceRejected, admit_source
from .config import DEFAULT_CONFIG, WorkerConfig
from .control_plane import Claim, ControlPlane, ProgressTelemetry, StaleClaimError
from .pipeline import FreePoolExhausted, run_real_pipeline
from .scheduling import ClaimPlan, SlotTracker, weight_class
from .storage import SourceTooLargeError, Storage

logger = logging.getLogger("media_worker")

# The re-admission port (T2.4): (downloaded source path, job, config) -> None, raising
# SourceRejected on a format/size/duration violation. Injected so the stub copy-loop tests can pass
# a pass-through; the default is the real ffprobe re-admission.
Admitter = Callable[[Path, Job, WorkerConfig], None]


def source_key_for(job: Job) -> str:
    """R2 key of the uploaded source.

    Mirrors the control plane's signUpload convention (apps/control-plane/src/uploads.ts):
    ``uploads/${upload_session_id}``. The schemas Job intentionally omits source_key, so the worker
    re-derives it from upload_session_id (a deterministic, no-client-path server convention).

    upload_session_id is server-issued, but it is validated as a safe component defensively so a
    malformed/hostile value can never inject a path or query separator into the R2 object URL.
    """
    session = safe_component(job.upload_session_id, label="upload_session_id")
    return f"uploads/{session}"


def artifact_key(job_id: str, claim_version: int, name: str) -> str:
    """Artifact R2 key.

    MUST match the control plane's complete() guard ``artifacts/{job_id}/{claim_version}/...`` so a
    reclaimed stale worker (a different claim_version) cannot overwrite the winning artifact path.
    """
    return f"artifacts/{job_id}/{claim_version}/{name}"


def job_workdir(base: Path | str, job_id: str, claim_version: int) -> Path:
    """Namespace-isolated, path-contained scratch dir for one claim: ``base/<job_id>__<cv>``.

    Scoped by claim_version (like the R2 artifact keys) so that if an expired lease is reclaimed on
    the same host, the stale attempt and the winning attempt (same job_id, different claim_version)
    never share a scratch dir — neither can overwrite or delete the other's files. job_id is
    validated as a single safe component and the result is contained within base (reuses
    autodub-core's T1.3a guards) — no traversal / reserved-name / separator escape.
    """
    base_path = Path(base)
    name = f"{safe_component(job_id, label='job_id')}__{claim_version}"
    return ensure_within(base_path, base_path / name)


def clean_orphan_workdirs(base: Path | str) -> list[str]:
    """Remove every child dir under base (scratch left by a crashed run). Returns removed names.

    Assumes ONE worker process owns `base` (the deployed model: one media-worker container per box
    owning OVT_WORKDIR — plan §6/§deploy). It is therefore safe to remove all leftover scratch dirs
    at startup. If multiple worker processes ever share one OVT_WORKDIR, give each its own base (or
    add ownership/lock markers) so this sweep cannot delete another process's in-flight work — that
    multi-process hardening is a deploy/ops concern (routed), out of the T2.2 stub's scope.
    """
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
        max_total_sec: float | None = None,
        stage: str = "processing",
        on_lost: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cp = cp
        self._job_id = job_id
        self._claim_version = claim_version
        self._interval = interval_sec
        self._max_total_sec = max_total_sec
        self._stage = stage
        self._on_lost = on_lost
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # OBS (#24): the pipeline routing layer pushes the current routed provider + outcome here;
        # the timer thread folds them into each heartbeat. Guarded (main thread sets, hb reads).
        self._tele_lock = threading.Lock()
        self._tele_provider: str | None = None
        self._tele_free_pool_result: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"heartbeat-{self._job_id}", daemon=True
        )
        self._thread.start()

    def set_telemetry(
        self, *, provider: str | None = None, free_pool_result: str | None = None
    ) -> None:
        """Update the OBS telemetry the heartbeat reports (called by the pipeline routing layer)."""
        with self._tele_lock:
            if provider is not None:
                self._tele_provider = provider
            if free_pool_result is not None:
                self._tele_free_pool_result = free_pool_result

    def _run(self) -> None:
        # wait() returns True only when stop() is set, so the loop ticks once per interval and exits
        # promptly on stop. Renewal is driven purely by this timer, never by stage edges (plan §6).
        started = self._clock()
        while not self._stop.wait(self._interval):
            # One clock read per iteration, reused for BOTH the hard-cap check and the OBS stage
            # timing telemetry (so telemetry doesn't change the lease/timeout tick semantics).
            tnow = self._clock()
            if self._max_total_sec is not None and tnow - started >= self._max_total_sec:
                # Hard cap reached (plan §6 job_hard_timeout_sec): stop renewing so the lease
                # lapses and the sweeper requeues/fails it (a wedged stage can't hold it forever).
                logger.warning("job %s hit the hard timeout; stopping lease renewal", self._job_id)
                if self._on_lost is not None:
                    self._on_lost()
                return
            try:
                # OBS (#24): report stage timing + (once the pipeline has routed) the current free
                # provider and routing outcome. build_progress_body omits any field still None.
                elapsed_ms = max(0, int((tnow - started) * 1000))
                with self._tele_lock:
                    provider, fpr = self._tele_provider, self._tele_free_pool_result
                self._cp.heartbeat(
                    self._job_id,
                    self._claim_version,
                    stage=self._stage,
                    telemetry=ProgressTelemetry(
                        stage_elapsed_ms=elapsed_ms, provider=provider, free_pool_result=fpr
                    ),
                )
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


# The artifact-production port (M2-CLOSE): (cp, storage, claim, workdir, in_path) -> {field: key}.
# Default = the real autodub-core pipeline with free-pool routing; injected so the orchestration
# tests can drive the claim loop with a fake producer (no ffmpeg / no real providers).
ArtifactProducer = Callable[..., dict[str, str]]


def _default_produce(
    cp: ControlPlane,
    storage: Storage,
    claim: Claim,
    workdir: Path,
    in_path: Path,
    *,
    on_telemetry: Callable[..., None] | None = None,
) -> dict[str, str]:
    return run_real_pipeline(
        cp, storage, claim.job, claim.claim_version,
        in_path=in_path, workdir=workdir, make_key=artifact_key, on_telemetry=on_telemetry,
    )


def process_job(
    cp: ControlPlane,
    storage: Storage,
    claim: Claim,
    *,
    workdir_base: Path | str,
    config: WorkerConfig,
    clock: Callable[[], float] = time.monotonic,
    admit: Admitter = admit_source,
    produce: ArtifactProducer = _default_produce,
) -> None:
    """Run one claimed job: fetch -> re-admit -> run the pipeline -> complete (or fail)."""
    job = claim.job
    claim_version = claim.claim_version
    job_id = job.job_id
    workdir = job_workdir(workdir_base, job_id, claim_version)
    workdir.mkdir(parents=True, exist_ok=True)
    lease_lost = threading.Event()
    heartbeat = Heartbeat(
        cp,
        job_id,
        claim_version,
        interval_sec=config.heartbeat_interval_sec,
        max_total_sec=config.job_hard_timeout_sec,
        on_lost=lease_lost.set,
    )
    heartbeat.start()
    started = clock()
    try:
        # Fetch the source, then RE-ADMIT it (T2.4): the authoritative format/size/duration gate on
        # the actual bytes, closing a post-HEAD swap. A re-admission rejection is a specific
        # user-facing error_code (+ delete the offending source); a genuine fetch failure is
        # internal_error. error_detail is always omitted (exception text can carry a path/URL).
        try:
            _precheck_source_size(storage, job, config)
            in_path = _download_source(storage, job, workdir, config)
            admit(in_path, job, config)
        except SourceRejected as rej:
            # Record the rejection FIRST, then delete the source — and only if the report landed. A
            # transient fail() blip then leaves the job running with its source intact, so the lease
            # recovery re-admits + re-rejects with the SAME error_code (idempotent), instead of the
            # source being gone and the retry mislabeling it internal_error.
            if _report_fail(cp, job_id, claim_version, rej.error_code):
                _delete_source_quietly(storage, job)
                logger.info("job %s rejected at re-admission (%s); deleted", job_id, rej.error_code)
            return
        except Exception:
            _report_fail(cp, job_id, claim_version, "internal_error")
            logger.warning("job %s failed (stub)", job_id)
            return
        try:
            artifacts = produce(
                cp, storage, claim, workdir, in_path, on_telemetry=heartbeat.set_telemetry
            )
        except LanguageError as le:
            # Fail-closed language gate (T1.3f): a coded, user-facing rejection (unsupported pair /
            # no commercial-safe TTS voice). Record the schema error_code, not internal_error.
            _report_fail(cp, job_id, claim_version, le.code)
            logger.info("job %s rejected by the language gate (%s)", job_id, le.code)
            return
        except FreePoolExhausted:
            # Every free provider for a needed stage is exhausted/unconfigured: a coded terminal,
            # NEVER a paid escalation (red line §1/§14).
            _report_fail(cp, job_id, claim_version, "free_pool_exhausted")
            logger.warning("job %s failed: free pool exhausted", job_id)
            return
        except Exception:
            _report_fail(cp, job_id, claim_version, "internal_error")
            logger.warning("job %s failed (pipeline)", job_id)
            return
        # Hard-timeout gate, checked PRECISELY here (not only on the coarse heartbeat tick): a job
        # that finished after the cap — even within a heartbeat interval — must not be marked done.
        # lease_lost also covers a lease reclaimed mid-run. Either way report processing_timeout (a
        # no-op server-side if the lease was already reclaimed; terminal if we still hold it).
        if clock() - started >= config.job_hard_timeout_sec or lease_lost.is_set():
            _report_fail(cp, job_id, claim_version, "processing_timeout")
            logger.warning("job %s exceeded the hard timeout; reported processing_timeout", job_id)
            return
        # Processing succeeded. Reporting completion is a separate concern: a transient transport
        # error on /complete must NOT fail a successful job. Leave it running for lease recovery
        # (the sweeper re-queues it after the lease expires; the idempotent copy + complete re-run).
        try:
            cp.complete(job_id, claim_version, artifacts=artifacts)
            logger.info("job %s completed (stub copy)", job_id)  # job id only (key encodes cv)
        except Exception:
            logger.warning("job %s completion report failed; left for lease recovery", job_id)
    finally:
        heartbeat.stop()
        shutil.rmtree(workdir, ignore_errors=True)


def _precheck_source_size(storage: Storage, job: Job, config: WorkerConfig) -> None:
    """HEAD the source and reject an oversized object BEFORE buffering its body, so a post-HEAD swap
    to a huge object can't exhaust the worker's memory/disk (admit_source's in_path.stat() size
    check runs only AFTER the full download). A missing object (head None) falls through to the
    download, which fails internal_error as before — R2 always returns Content-Length for an
    existing object, so the only None case is a 404 of an already-gone source."""
    size = storage.head(source_key_for(job))
    if size is not None and size > config.max_upload_bytes:
        raise SourceRejected("upload_too_large")


def _download_source(storage: Storage, job: Job, workdir: Path, config: WorkerConfig) -> Path:
    """Fetch the source into the job workdir, capping the buffered body at max_upload_bytes so a
    swap to an oversized object AFTER the HEAD precheck (the TOCTOU race) can't OOM the box."""
    try:
        source = storage.download(source_key_for(job), max_bytes=config.max_upload_bytes)
    except SourceTooLargeError as exc:
        raise SourceRejected("upload_too_large") from exc
    in_path = workdir / "input"
    in_path.write_bytes(source)
    return in_path


def _delete_source_quietly(storage: Storage, job: Job) -> None:
    """Best-effort delete of a rejected source. The sweeper's TTL purge is the backstop, so a
    transient delete failure must not crash the claim loop — log generically (no secret text)."""
    try:
        storage.delete(source_key_for(job))
    except Exception:
        logger.warning("job %s source delete failed; left for the TTL sweeper", job.job_id)


def _report_fail(cp: ControlPlane, job_id: str, claim_version: int, error_code: str) -> bool:
    """Report a terminal failure, swallowing a transient transport error the way the success path
    guards cp.complete: a control-plane blip must not crash the loop OR escape run_once — leave the
    job running for lease recovery instead. Returns True iff the failure was durably recorded. Logs
    the job id only (no exception text — secret hygiene)."""
    try:
        cp.fail(job_id, claim_version, error_code=error_code)
        return True
    except Exception:
        logger.warning("job %s fail report failed; left for lease recovery", job_id)
        return False


def run_once(
    cp: ControlPlane,
    storage: Storage,
    *,
    workdir_base: Path | str,
    config: WorkerConfig,
    admit: Admitter = admit_source,
    produce: ArtifactProducer = _default_produce,
) -> str | None:
    """Claim one job and process it. Returns the job_id, or None if nothing was claimable."""
    claim = cp.claim()
    if claim is None:
        return None
    process_job(
        cp, storage, claim, workdir_base=workdir_base, config=config, admit=admit, produce=produce
    )
    return claim.job.job_id


def run_forever(
    cp: ControlPlane,
    storage: Storage,
    *,
    workdir_base: Path | str,
    config: WorkerConfig | None = None,
    poll_idle_sec: float = 2.0,
    stop_event: threading.Event | None = None,
    worker_concurrency: int = 1,
    light_slot_reserve: int = 1,
    admit: Admitter = admit_source,
    produce: ArtifactProducer = _default_produce,
) -> None:
    """Long-poll claim loop with bounded concurrency + the free_min_share light-slot reservation.

    A SINGLE dispatcher claims jobs and runs them in a pool of `worker_concurrency` threads,
    reserving `light_slot_reserve` slots for LIGHT (subtitle_only) jobs (scheduling.py): when the
    heavy budget is full it claims `light_only`, so a stream of dub jobs can never starve a
    subtitle job — WITHOUT preemption (运行中不抢占). Clears orphan dirs at startup; drains
    in-flight jobs on stop. Only the dispatcher calls tracker.add (so the box never over-fills);
    the pool threads call tracker.remove.
    """
    base = Path(workdir_base)
    base.mkdir(parents=True, exist_ok=True)
    cleared = clean_orphan_workdirs(base)
    if cleared:
        # List the names (job ids, never secrets) so an unexpected non-job dir under the dedicated
        # jobs base is visible in the log rather than silently destroyed.
        logger.info(
            "cleared %d orphan workdir(s) at startup: %s", len(cleared), ", ".join(sorted(cleared))
        )
    # A fixed `config` pins the knobs (test injection). Otherwise pull /internal/config at startup
    # AND refresh it per claim (plan §14: worker pulls config at startup + claim time) so runtime
    # lease-knob changes take effect without a restart; fall back to the last-known config on error.
    cfg = config if config is not None else _fetch_config(cp, DEFAULT_CONFIG)
    tracker = SlotTracker(worker_concurrency, light_slot_reserve)
    # Leaving the `with` calls shutdown(wait=True) -> every in-flight job finishes before
    # run_forever returns (a stop mid-job drains the pool, never abandons the job).
    with ThreadPoolExecutor(max_workers=worker_concurrency, thread_name_prefix="ovt-job") as pool:
        while stop_event is None or not stop_event.is_set():
            plan = tracker.plan()
            if plan is ClaimPlan.NONE:
                # Box full: park until a pool thread frees a slot (or the timeout elapses so we
                # re-check stop_event). No claim is issued while full.
                tracker.wait_for_slot(poll_idle_sec)
                continue
            claim = None
            try:
                # When only the reserved slots remain, ask for a LIGHT job ONLY, so a dub job
                # can never take a slot reserved for subtitles (free_min_share).
                claim = cp.claim(light_only=(plan is ClaimPlan.LIGHT_ONLY))
                if claim is not None:
                    if config is None:
                        cfg = _fetch_config(cp, cfg)
                    weight = weight_class(claim.job.output_mode)
                    tracker.add(weight)  # reserve the slot BEFORE submit (dispatcher = sole writer)
                    _spawn(pool, tracker, weight, cp, storage, claim, base, cfg, admit, produce)
            except Exception:
                # A single iteration's unexpected error (e.g. a control-plane blip) must not kill
                # the long-running worker. Log generically — no exception text (secret hygiene).
                logger.warning("worker iteration failed; continuing")
            if claim is None:
                # Nothing claimable for this plan (queue drained, or only heavy jobs remain under
                # a light-only plan): idle-poll, honoring stop. A freed heavy slot is picked up
                # next poll.
                if stop_event is not None:
                    if stop_event.wait(poll_idle_sec):
                        break
                else:
                    time.sleep(poll_idle_sec)


def _spawn(
    pool: ThreadPoolExecutor,
    tracker: SlotTracker,
    weight: str,
    cp: ControlPlane,
    storage: Storage,
    claim: Claim,
    base: Path,
    cfg: WorkerConfig,
    admit: Admitter,
    produce: ArtifactProducer,
) -> None:
    """Run a claimed job in the pool, ALWAYS releasing its slot when it finishes (success or
    crash)."""

    def _task() -> None:
        try:
            process_job(
                cp, storage, claim, workdir_base=base, config=cfg, admit=admit, produce=produce
            )
        except Exception:
            # process_job maps known failures to fail() itself; this guards a truly unexpected
            # crash so a pool thread can't die with the slot still counted. Job id only (hygiene).
            logger.warning("job %s task crashed unexpectedly", claim.job.job_id)
        finally:
            tracker.remove(weight)

    pool.submit(_task)


def _fetch_config(cp: ControlPlane, fallback: WorkerConfig) -> WorkerConfig:
    """Fetch runtime config, falling back to the last-known/default on any control-plane error."""
    try:
        return cp.get_config()
    except Exception:
        logger.warning("config fetch failed; using the last-known/default config")
        return fallback
