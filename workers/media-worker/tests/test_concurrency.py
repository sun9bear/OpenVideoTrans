"""Concurrent claim-loop integration: the free_min_share light-slot reservation end to end
(M2-CLOSE PR-D, #26).

run_forever runs up to `worker_concurrency` jobs at once via a single dispatcher + a worker pool,
reserving `light_slot_reserve` slots for LIGHT (subtitle_only) jobs. These tests gate the artifact
producer on events so job overlap is observed DETERMINISTICALLY (no sleeps): a heavy job is held
"in flight" while we assert what the dispatcher may and may not claim next.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, Thread

from media_worker.worker import run_forever
from mw_fakes import TEST_CONFIG, Claim, FakeControlPlane, FakeStorage, artifact_key, make_job


class Gate:
    """An event-gated artifact producer that records overlap. Each job blocks in produce() until the
    test releases it, so concurrency is observed deterministically (no sleeps)."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.live: set[str] = set()
        self.max_concurrent = 0
        self.max_heavy = 0
        self._started: dict[str, Event] = {}
        self._release: dict[str, Event] = {}

    def _ev(self, table: dict[str, Event], job_id: str) -> Event:
        with self._lock:
            return table.setdefault(job_id, Event())

    def started(self, job_id: str) -> Event:
        return self._ev(self._started, job_id)

    def release(self, job_id: str) -> None:
        self._ev(self._release, job_id).set()

    def snapshot_live(self) -> set[str]:
        with self._lock:
            return set(self.live)

    def produce(
        self, _cp: object, storage: FakeStorage, claim: Claim, _wd: Path, in_path: Path,
        **_kw: object,
    ) -> dict[str, str]:
        job = claim.job
        with self._lock:
            self.live.add(job.job_id)
            self.max_concurrent = max(self.max_concurrent, len(self.live))
            heavy = sum(1 for j in self.live if j.startswith("heavy"))
            self.max_heavy = max(self.max_heavy, heavy)
        self.started(job.job_id).set()
        assert self._ev(self._release, job.job_id).wait(5.0), f"{job.job_id} never released"
        data = in_path.read_bytes()  # mirror COPY_PRODUCE: write the source bytes to the artifact
        if job.output_mode == "subtitle_only":
            name, field = "output.srt", "srt_key"
        else:
            name, field = "output.mp4", "video_key"
        key = artifact_key(job.job_id, claim.claim_version, name)
        storage.upload(key, data, content_type="application/octet-stream")
        with self._lock:
            self.live.discard(job.job_id)
        return {field: key}


def _heavy(n: int) -> Claim:
    jid = f"heavy_{n}"
    job = make_job(job_id=jid, upload_session_id=f"us_{jid}", output_mode="dub_only")
    return Claim(job=job, claim_version=1, attempt=1)


def _light(n: int) -> Claim:
    jid = f"light_{n}"
    job = make_job(job_id=jid, upload_session_id=f"us_{jid}", output_mode="subtitle_only")
    return Claim(job=job, claim_version=1, attempt=1)


def _objects(claims: list[Claim]) -> dict[str, bytes]:
    return {f"uploads/us_{c.job.job_id}": c.job.job_id.encode() for c in claims}


def _run(
    cp: FakeControlPlane, storage: FakeStorage, gate: Gate, tmp_path: Path, stop: Event,
    *, concurrency: int, reserve: int,
) -> Thread:
    th = Thread(
        target=run_forever,
        args=(cp, storage),
        kwargs={
            "workdir_base": tmp_path, "config": TEST_CONFIG, "stop_event": stop,
            "worker_concurrency": concurrency, "light_slot_reserve": reserve,
            "poll_idle_sec": 0.02, "admit": lambda *_a, **_k: None, "produce": gate.produce,
        },
        daemon=True,
    )
    th.start()
    return th


def _wait_until(pred: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_light_runs_alongside_heavy_but_heavy_is_capped(tmp_path: Path) -> None:
    # concurrency=2, reserve=1: a heavy job + a light job run together (2 in flight), but a SECOND
    # heavy job must wait — the reserved slot is light-only. After the first heavy frees, heavy_2
    # runs.
    claims = [_heavy(1), _heavy(2), _light(1)]
    gate = Gate()
    cp = FakeControlPlane(config=TEST_CONFIG, claims=list(claims))
    storage = FakeStorage(_objects(claims))
    stop = Event()
    th = _run(cp, storage, gate, tmp_path, stop, concurrency=2, reserve=1)
    try:
        assert gate.started("heavy_1").wait(3.0)
        assert gate.started("light_1").wait(3.0)  # light job took the reserved slot alongside heavy
        assert not gate.started("heavy_2").is_set()  # heavy budget (1) full -> heavy_2 must wait
        assert gate.snapshot_live() == {"heavy_1", "light_1"}
        gate.release("heavy_1")  # free the heavy slot
        assert gate.started("heavy_2").wait(3.0)  # now heavy_2 may run
        gate.release("light_1")
        gate.release("heavy_2")
        assert _wait_until(lambda: len(cp.completed) == 3, 3.0), f"completed={cp.completed}"
        assert gate.max_concurrent == 2  # never exceeded worker_concurrency
        assert gate.max_heavy == 1  # never more than one heavy job at a time
        assert {c[0] for c in cp.completed} == {"heavy_1", "heavy_2", "light_1"}
    finally:
        stop.set()
        for jid in ("heavy_1", "heavy_2", "light_1"):
            gate.release(jid)
        th.join(5.0)
    assert not th.is_alive()


def test_reserved_slot_idles_when_only_heavy_queued(tmp_path: Path) -> None:
    # concurrency=2, reserve=1, only heavy jobs queued (no light): the reserved slot stays IDLE
    # rather than admitting a second heavy job (no preemption guarantee for light). Heavy runs
    # 1-at-a-time.
    claims = [_heavy(1), _heavy(2)]
    gate = Gate()
    cp = FakeControlPlane(config=TEST_CONFIG, claims=list(claims))
    storage = FakeStorage(_objects(claims))
    stop = Event()
    th = _run(cp, storage, gate, tmp_path, stop, concurrency=2, reserve=1)
    try:
        assert gate.started("heavy_1").wait(3.0)
        # heavy_2 must stay queued: the only free slot is reserved for a light job that doesn't
        # exist.
        assert not gate.started("heavy_2").wait(0.3)
        assert gate.snapshot_live() == {"heavy_1"}
        gate.release("heavy_1")
        assert gate.started("heavy_2").wait(3.0)  # freed slot lets heavy_2 run
        gate.release("heavy_2")
        assert _wait_until(lambda: len(cp.completed) == 2, 3.0)
        assert gate.max_concurrent == 1  # the reserved slot never admitted a 2nd heavy job
    finally:
        stop.set()
        for jid in ("heavy_1", "heavy_2"):
            gate.release(jid)
        th.join(5.0)
    assert not th.is_alive()


def test_never_exceeds_worker_concurrency_draining_many(tmp_path: Path) -> None:
    # A burst of light jobs (any slot eligible) must still never exceed worker_concurrency in
    # flight.
    claims = [_light(i) for i in range(6)]
    gate = Gate()
    cp = FakeControlPlane(config=TEST_CONFIG, claims=list(claims))
    storage = FakeStorage(_objects(claims))
    stop = Event()
    th = _run(cp, storage, gate, tmp_path, stop, concurrency=2, reserve=1)
    released: set[str] = set()

    def _drain() -> bool:
        for c in claims:
            jid = c.job.job_id
            if jid not in released and gate.started(jid).is_set():
                gate.release(jid)
                released.add(jid)
        return len(cp.completed) == 6

    try:
        assert _wait_until(_drain, 5.0)
        assert len(cp.completed) == 6
        assert gate.max_concurrent <= 2
    finally:
        stop.set()
        for c in claims:
            gate.release(c.job.job_id)
        th.join(5.0)
    assert not th.is_alive()


def test_stop_drains_inflight_job_before_returning(tmp_path: Path) -> None:
    # Setting stop_event while a job is mid-flight must not abandon it: run_forever drains the pool
    # (shutdown wait) so the in-flight job completes before the function returns.
    claims = [_heavy(1)]
    gate = Gate()
    cp = FakeControlPlane(config=TEST_CONFIG, claims=list(claims))
    storage = FakeStorage(_objects(claims))
    stop = Event()
    th = _run(cp, storage, gate, tmp_path, stop, concurrency=2, reserve=1)
    assert gate.started("heavy_1").wait(3.0)
    stop.set()  # ask the loop to stop while heavy_1 is still blocked in produce
    th.join(0.3)
    assert th.is_alive()  # must NOT return yet — it has to drain the in-flight job
    gate.release("heavy_1")
    th.join(5.0)
    assert not th.is_alive()
    assert cp.completed == [("heavy_1", 1, {"video_key": artifact_key("heavy_1", 1, "output.mp4")})]
