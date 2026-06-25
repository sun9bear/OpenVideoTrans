from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from autodub_core.isolation import PathEscapeError
from media_worker.worker import (
    artifact_key,
    clean_orphan_workdirs,
    job_workdir,
    run_forever,
    run_once,
    source_key_for,
)
from mw_fakes import TEST_CONFIG, Claim, FakeControlPlane, FakeStorage, make_job


def test_source_key_matches_control_plane_convention() -> None:
    # Drift guard: must equal the control plane's signUpload key `uploads/${upload_session_id}`.
    # (apps/control-plane/src/uploads.ts; the schemas Job omits source_key, the worker re-derives.)
    assert source_key_for(make_job(upload_session_id="us_xyz")) == "uploads/us_xyz"


def test_closed_loop_copies_source_to_artifact_and_completes(tmp_path: Path) -> None:
    job = make_job(job_id="job_a", upload_session_id="us_a", output_mode="dub_only")
    cp = FakeControlPlane(config=TEST_CONFIG, claims=[Claim(job=job, claim_version=1, attempt=1)])
    storage = FakeStorage({"uploads/us_a": b"VIDEOBYTES"})
    jid = run_once(cp, storage, workdir_base=tmp_path, config=TEST_CONFIG)
    assert jid == "job_a"
    akey = artifact_key("job_a", 1, "output.mp4")
    assert storage.objects[akey] == b"VIDEOBYTES"  # input copied as-is to output (stub)
    assert cp.completed == [("job_a", 1, {"video_key": akey})]
    assert cp.failed == []
    assert not (tmp_path / "job_a").exists()  # try/finally cleaned the workdir


def test_subtitle_only_completes_with_srt_key(tmp_path: Path) -> None:
    job = make_job(job_id="job_s", upload_session_id="us_s", output_mode="subtitle_only")
    cp = FakeControlPlane(config=TEST_CONFIG, claims=[Claim(job=job, claim_version=2, attempt=1)])
    storage = FakeStorage({"uploads/us_s": b"X"})
    run_once(cp, storage, workdir_base=tmp_path, config=TEST_CONFIG)
    assert cp.completed == [("job_s", 2, {"srt_key": artifact_key("job_s", 2, "output.srt")})]


def test_no_job_returns_none_without_touching_storage(tmp_path: Path) -> None:
    cp = FakeControlPlane(config=TEST_CONFIG, claims=[])
    storage = FakeStorage({})
    assert run_once(cp, storage, workdir_base=tmp_path, config=TEST_CONFIG) is None
    assert storage.uploads == []


def test_failure_reports_fail_and_cleans_workdir(tmp_path: Path) -> None:
    job = make_job(job_id="job_f", upload_session_id="us_missing", output_mode="dub_only")
    cp = FakeControlPlane(config=TEST_CONFIG, claims=[Claim(job=job, claim_version=1, attempt=1)])
    storage = FakeStorage({})  # source object absent -> download raises
    jid = run_once(cp, storage, workdir_base=tmp_path, config=TEST_CONFIG)
    assert jid == "job_f"
    assert cp.completed == []
    assert len(cp.failed) == 1
    fjid, fcv, code, detail = cp.failed[0]
    assert (fjid, fcv, code) == ("job_f", 1, "internal_error")
    assert detail is None  # never leak exception text (it may carry a path/URL)
    assert not (tmp_path / "job_f").exists()


def test_heartbeat_renews_during_a_long_single_stage(tmp_path: Path) -> None:
    # plan §6: a single stage can exceed the lease TTL, so the heartbeat must renew mid-stage.
    # A blocking download stands in for a long stage; assert a renewal lands before it returns.
    from mw_fakes import FAST_CONFIG

    entered = threading.Event()
    release = threading.Event()

    class _BlockingStorage(FakeStorage):
        def download(self, key: str) -> bytes:
            data = super().download(key)
            entered.set()
            assert release.wait(3.0)
            return data

    job = make_job(job_id="job_l", upload_session_id="us_l", output_mode="dub_only")
    cp = FakeControlPlane(config=FAST_CONFIG, claims=[Claim(job=job, claim_version=1, attempt=1)])
    storage = _BlockingStorage({"uploads/us_l": b"DATA"})
    worker = threading.Thread(
        target=run_once,
        args=(cp, storage),
        kwargs={"workdir_base": tmp_path, "config": FAST_CONFIG},
    )
    worker.start()
    assert entered.wait(3.0)
    deadline = time.monotonic() + 3.0
    while cp.heartbeat_count < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert cp.heartbeat_count >= 1  # renewed mid-stage, no stage edge required
    release.set()
    worker.join(5.0)
    assert not worker.is_alive()
    assert cp.completed and not (tmp_path / "job_l").exists()


def test_job_workdir_rejects_traversal_and_reserved_names(tmp_path: Path) -> None:
    assert job_workdir(tmp_path, "job_ok") == (tmp_path / "job_ok").resolve()
    for bad in ("../evil", "a/b", "..", "CON", "x\\y"):
        with pytest.raises(PathEscapeError):
            job_workdir(tmp_path, bad)


def test_startup_cleans_orphan_workdirs(tmp_path: Path) -> None:
    orphan = tmp_path / "stale_job"
    (orphan / "sub").mkdir(parents=True)
    (orphan / "sub" / "f.bin").write_bytes(b"leftover")
    removed = clean_orphan_workdirs(tmp_path)
    assert "stale_job" in removed
    assert not orphan.exists()


def test_run_forever_cleans_orphans_at_startup(tmp_path: Path) -> None:
    orphan = tmp_path / "crashed_job"
    orphan.mkdir()
    (orphan / "f").write_bytes(b"x")
    stop = threading.Event()
    stop.set()  # pre-stopped: the claim loop body never runs, but startup cleanup does
    cp = FakeControlPlane(config=TEST_CONFIG, claims=[])
    run_forever(cp, FakeStorage({}), workdir_base=tmp_path, config=TEST_CONFIG, stop_event=stop)
    assert not orphan.exists()
