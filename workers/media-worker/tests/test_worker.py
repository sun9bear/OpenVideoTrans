from __future__ import annotations

import threading
import time
from pathlib import Path

import media_worker.admission as adm
import pytest
from autodub_core import ffmpeg_utils as ff
from autodub_core.isolation import PathEscapeError
from media_worker.config import WorkerConfig
from media_worker.pipeline import FreePoolExhausted
from media_worker.worker import (
    artifact_key,
    clean_orphan_workdirs,
    job_workdir,
    process_job,
    run_forever,
    run_once,
    source_key_for,
)
from mw_fakes import (
    ALLOW_ADMIT,
    COPY_PRODUCE,
    TEST_CONFIG,
    Claim,
    FakeControlPlane,
    FakeStorage,
    make_job,
)
from provider_adapters import LanguageError


def test_source_key_matches_control_plane_convention() -> None:
    # Drift guard: must equal the control plane's signUpload key `uploads/${upload_session_id}`.
    # (apps/control-plane/src/uploads.ts; the schemas Job omits source_key, the worker re-derives.)
    assert source_key_for(make_job(upload_session_id="us_xyz")) == "uploads/us_xyz"


def test_source_key_rejects_unsafe_session_id() -> None:
    # Defense-in-depth: a hostile upload_session_id can't inject a path/query separator.
    for bad in ("../evil", "a/b", "x?y", "*"):
        with pytest.raises(PathEscapeError):
            source_key_for(make_job(upload_session_id=bad))


def test_closed_loop_copies_source_to_artifact_and_completes(tmp_path: Path) -> None:
    job = make_job(job_id="job_a", upload_session_id="us_a", output_mode="dub_only")
    cp = FakeControlPlane(config=TEST_CONFIG, claims=[Claim(job=job, claim_version=1, attempt=1)])
    storage = FakeStorage({"uploads/us_a": b"VIDEOBYTES"})
    jid = run_once(
        cp, storage, workdir_base=tmp_path, config=TEST_CONFIG, admit=ALLOW_ADMIT,
        produce=COPY_PRODUCE,
    )
    assert jid == "job_a"
    akey = artifact_key("job_a", 1, "output.mp4")
    assert storage.objects[akey] == b"VIDEOBYTES"  # the fake producer copied the source bytes
    assert cp.completed == [("job_a", 1, {"video_key": akey})]
    assert cp.failed == []
    assert not (tmp_path / "job_a__1").exists()  # try/finally cleaned the cv-scoped workdir


def test_subtitle_only_completes_with_srt_key(tmp_path: Path) -> None:
    job = make_job(job_id="job_s", upload_session_id="us_s", output_mode="subtitle_only")
    cp = FakeControlPlane(config=TEST_CONFIG, claims=[Claim(job=job, claim_version=2, attempt=1)])
    storage = FakeStorage({"uploads/us_s": b"X"})
    run_once(
        cp, storage, workdir_base=tmp_path, config=TEST_CONFIG, admit=ALLOW_ADMIT,
        produce=COPY_PRODUCE,
    )
    assert cp.completed == [("job_s", 2, {"srt_key": artifact_key("job_s", 2, "output.srt")})]


def test_both_mode_completes_with_both_artifacts(tmp_path: Path) -> None:
    # `both` must produce a video AND an SRT, else the download API 404s on /srt for the job.
    job = make_job(job_id="job_b", upload_session_id="us_b", output_mode="both")
    cp = FakeControlPlane(config=TEST_CONFIG, claims=[Claim(job=job, claim_version=1, attempt=1)])
    storage = FakeStorage({"uploads/us_b": b"SRC"})
    run_once(
        cp, storage, workdir_base=tmp_path, config=TEST_CONFIG, admit=ALLOW_ADMIT,
        produce=COPY_PRODUCE,
    )
    vkey = artifact_key("job_b", 1, "output.mp4")
    skey = artifact_key("job_b", 1, "output.srt")
    assert cp.completed == [("job_b", 1, {"video_key": vkey, "srt_key": skey})]
    assert storage.objects[vkey] == b"SRC"
    assert storage.objects[skey] == b"SRC"


def test_language_gate_failure_maps_to_its_error_code(tmp_path: Path) -> None:
    # A produce step that fails closed on the language gate is reported with its schema error_code
    # (not internal_error), so the user sees a precise reason.
    job = make_job(job_id="job_lg", upload_session_id="us_lg", output_mode="dub_only")
    cp = FakeControlPlane(config=TEST_CONFIG, claims=[Claim(job=job, claim_version=1, attempt=1)])
    storage = FakeStorage({"uploads/us_lg": b"X"})

    def _raise(*_a: object, **_k: object) -> dict[str, str]:
        raise LanguageError("no_tts_model_for_language", "no commercial-safe voice")

    run_once(
        cp, storage, workdir_base=tmp_path, config=TEST_CONFIG, admit=ALLOW_ADMIT, produce=_raise
    )
    assert cp.failed == [("job_lg", 1, "no_tts_model_for_language", None)]
    assert cp.completed == []


def test_free_pool_exhausted_maps_to_error_code(tmp_path: Path) -> None:
    # Every free provider for a needed stage exhausted -> free_pool_exhausted, a coded terminal,
    # NEVER a paid escalation (red line §1/§14).
    job = make_job(job_id="job_fp", upload_session_id="us_fp", output_mode="dub_only")
    cp = FakeControlPlane(config=TEST_CONFIG, claims=[Claim(job=job, claim_version=1, attempt=1)])
    storage = FakeStorage({"uploads/us_fp": b"X"})

    def _raise(*_a: object, **_k: object) -> dict[str, str]:
        raise FreePoolExhausted("asr")

    run_once(
        cp, storage, workdir_base=tmp_path, config=TEST_CONFIG, admit=ALLOW_ADMIT, produce=_raise
    )
    assert cp.failed == [("job_fp", 1, "free_pool_exhausted", None)]
    assert cp.completed == []


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
    assert not (tmp_path / "job_f__1").exists()


def test_completion_transport_error_does_not_fail_job(tmp_path: Path) -> None:
    # A transient error reporting /complete must NOT mark a successful job failed (CodeX P2): the
    # artifact uploaded fine, so leave the job for lease recovery instead of failing it.
    job = make_job(job_id="job_c", upload_session_id="us_c", output_mode="dub_only")
    cp = FakeControlPlane(
        config=TEST_CONFIG, claims=[Claim(job=job, claim_version=1, attempt=1)], complete_error=True
    )
    storage = FakeStorage({"uploads/us_c": b"X"})
    run_once(
        cp, storage, workdir_base=tmp_path, config=TEST_CONFIG, admit=ALLOW_ADMIT,
        produce=COPY_PRODUCE,
    )
    assert cp.failed == []  # NOT failed despite the /complete transport error
    assert storage.objects[artifact_key("job_c", 1, "output.mp4")] == b"X"  # work succeeded
    assert not (tmp_path / "job_c__1").exists()  # workdir still cleaned


def test_heartbeat_renews_during_a_long_single_stage(tmp_path: Path) -> None:
    # plan §6: a single stage can exceed the lease TTL, so the heartbeat must renew mid-stage.
    # A blocking download stands in for a long stage; assert a renewal lands before it returns.
    from mw_fakes import FAST_CONFIG

    entered = threading.Event()
    release = threading.Event()

    class _BlockingStorage(FakeStorage):
        def download(self, key: str, *, max_bytes: int | None = None) -> bytes:
            data = super().download(key, max_bytes=max_bytes)
            entered.set()
            assert release.wait(3.0)
            return data

    job = make_job(job_id="job_l", upload_session_id="us_l", output_mode="dub_only")
    cp = FakeControlPlane(config=FAST_CONFIG, claims=[Claim(job=job, claim_version=1, attempt=1)])
    storage = _BlockingStorage({"uploads/us_l": b"DATA"})
    worker = threading.Thread(
        target=run_once,
        args=(cp, storage),
        kwargs={
            "workdir_base": tmp_path, "config": FAST_CONFIG, "admit": ALLOW_ADMIT,
            "produce": COPY_PRODUCE,
        },
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
    assert cp.completed and not (tmp_path / "job_l__1").exists()


def test_exceeding_hard_timeout_reports_processing_timeout(tmp_path: Path) -> None:
    # Precise elapsed gate (CodeX P2): a job whose work finishes after the hard cap is failed
    # processing_timeout even within one heartbeat interval. Inject a clock that jumps past the cap.
    cfg = WorkerConfig(
        heartbeat_interval_sec=30.0, lease_ttl_sec=180.0, job_hard_timeout_sec=10.0, max_attempts=2
    )
    job = make_job(job_id="job_t", upload_session_id="us_t", output_mode="dub_only")
    cp = FakeControlPlane(config=cfg)
    storage = FakeStorage({"uploads/us_t": b"X"})
    times = iter([0.0, 100.0])  # started=0; post-produce check=100 (>= 10s cap)
    process_job(
        cp,
        storage,
        Claim(job=job, claim_version=1, attempt=1),
        workdir_base=tmp_path,
        config=cfg,
        clock=lambda: next(times, 100.0),
        admit=ALLOW_ADMIT,
        produce=COPY_PRODUCE,
    )
    assert cp.completed == []  # NOT marked done
    assert cp.failed == [("job_t", 1, "processing_timeout", None)]
    assert not (tmp_path / "job_t__1").exists()


def test_run_forever_refreshes_config_per_claim(tmp_path: Path) -> None:
    # plan §14: the worker re-pulls /internal/config at claim time so lease-knob changes take effect
    # without a restart (CodeX P2) — not just once at startup.
    job = make_job(job_id="job_r", upload_session_id="us_r", output_mode="dub_only")

    class _OneJobThenStop(FakeControlPlane):
        def __init__(self, stop: threading.Event) -> None:
            super().__init__(
                config=TEST_CONFIG, claims=[Claim(job=job, claim_version=1, attempt=1)]
            )
            self._stop_after = stop
            self.config_calls = 0

        def get_config(self) -> WorkerConfig:
            self.config_calls += 1
            return super().get_config()

        def claim(self) -> Claim | None:
            claimed = super().claim()
            if claimed is None:
                self._stop_after.set()  # work drained -> end the loop
            return claimed

    stop = threading.Event()
    cp = _OneJobThenStop(stop)
    storage = FakeStorage({"uploads/us_r": b"X"})
    # config=None -> refresh path; admit pass-through (the copy-loop test, not the source gate).
    run_forever(
        cp, storage, workdir_base=tmp_path, stop_event=stop, admit=ALLOW_ADMIT,
        produce=COPY_PRODUCE,
    )
    assert cp.config_calls == 2  # 1 at startup + 1 at the claim
    assert cp.completed == [("job_r", 1, {"video_key": artifact_key("job_r", 1, "output.mp4")})]


def test_job_workdir_rejects_traversal_and_reserved_names(tmp_path: Path) -> None:
    assert job_workdir(tmp_path, "job_ok", 1) == (tmp_path / "job_ok__1").resolve()
    for bad in ("../evil", "a/b", "..", "CON", "x\\y"):
        with pytest.raises(PathEscapeError):
            job_workdir(tmp_path, bad, 1)


def test_job_workdir_isolated_by_claim_version(tmp_path: Path) -> None:
    # A reclaim (same job_id, new claim_version) gets a distinct scratch dir (CodeX P2), mirroring
    # the cv-scoped R2 artifact keys so a stale attempt can't clobber the winner's local files.
    assert job_workdir(tmp_path, "job_z", 1) != job_workdir(tmp_path, "job_z", 2)


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


def test_run_forever_falls_back_to_defaults_on_config_error(tmp_path: Path) -> None:
    # A boot-time /internal/config failure must NOT exit the worker (CodeX P2): use defaults.
    stop = threading.Event()
    stop.set()
    cp = FakeControlPlane(claims=[], config_error=True)
    # config=None forces the startup fetch; it raises -> must be caught + defaulted, not propagated.
    run_forever(cp, FakeStorage({}), workdir_base=tmp_path, stop_event=stop)


# --------------------------------------------------------------------------- #
# T2.4 — ffprobe re-admission wired into the claim loop (the DEFAULT admit path)
# --------------------------------------------------------------------------- #
def test_process_job_rejects_disguised_playlist_and_deletes_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A source whose real container is a playlist (hls/concat) is refused by the worker's ffprobe
    # re-admission BEFORE any artifact is produced: fail(unsupported_format) + delete the R2 source.
    def _raise(p: object) -> None:
        raise ff.FfmpegError("input container 'hls,applehttp' refused (SSRF guard, T1.3c)")

    monkeypatch.setattr(adm.ff, "assert_allowed_input_format", _raise)
    job = make_job(job_id="job_p", upload_session_id="us_p", output_mode="dub_only")
    cp = FakeControlPlane(config=TEST_CONFIG, claims=[Claim(job=job, claim_version=1, attempt=1)])
    storage = FakeStorage({"uploads/us_p": b"\x00disguised"})
    run_once(cp, storage, workdir_base=tmp_path, config=TEST_CONFIG)  # DEFAULT admit
    assert cp.failed == [("job_p", 1, "unsupported_format", None)]  # no error_detail (hygiene)
    assert cp.completed == []
    assert storage.deleted == ["uploads/us_p"]  # over-cap/wrong-format object removed promptly
    assert storage.uploads == []  # never produced an artifact
    assert not (tmp_path / "job_p__1").exists()  # workdir still cleaned


def test_process_job_rejects_swapped_oversize_source_and_deletes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The post-HEAD swap: the control plane HEAD-passed a small object, then a larger one was PUT
    # before claim. The worker re-reads the ACTUAL bytes and rejects upload_too_large + deletes.
    monkeypatch.setattr(adm.ff, "assert_allowed_input_format", lambda p: None)
    monkeypatch.setattr(adm.ff, "probe_duration_ms", lambda p: 1_000)
    tiny_cap = WorkerConfig(
        heartbeat_interval_sec=30.0, lease_ttl_sec=180.0, job_hard_timeout_sec=2700.0,
        max_attempts=2, max_upload_bytes=8,
    )
    job = make_job(job_id="job_o", upload_session_id="us_o", output_mode="dub_only")
    cp = FakeControlPlane(config=tiny_cap, claims=[Claim(job=job, claim_version=1, attempt=1)])
    storage = FakeStorage({"uploads/us_o": b"\x00" * 64})  # 64 bytes > the 8-byte cap
    run_once(cp, storage, workdir_base=tmp_path, config=tiny_cap)  # DEFAULT admit
    assert cp.failed == [("job_o", 1, "upload_too_large", None)]
    assert cp.completed == []
    assert storage.deleted == ["uploads/us_o"]
    assert storage.downloads == []  # rejected at the HEAD pre-check — body never buffered (no OOM)


def test_reject_path_swap_after_head_is_bounded_by_download_cap(tmp_path: Path) -> None:
    # TOCTOU: the object is swapped oversized AFTER the HEAD precheck (head under-reports). The
    # body read must still be capped at max_upload_bytes and reject upload_too_large.
    tiny_cap = WorkerConfig(
        heartbeat_interval_sec=30.0, lease_ttl_sec=180.0, job_hard_timeout_sec=2700.0,
        max_attempts=2, max_upload_bytes=8,
    )

    class _SwapStorage(FakeStorage):
        def head(self, key: str) -> int | None:
            return 4  # claims small (passes the precheck) — then the real body is large

    job = make_job(job_id="job_w", upload_session_id="us_w", output_mode="dub_only")
    cp = FakeControlPlane(config=tiny_cap, claims=[Claim(job=job, claim_version=1, attempt=1)])
    storage = _SwapStorage({"uploads/us_w": b"\x00" * 64})  # 64 bytes > the 8-byte cap
    run_once(cp, storage, workdir_base=tmp_path, config=tiny_cap)  # DEFAULT admit
    assert cp.failed == [("job_w", 1, "upload_too_large", None)]  # bounded read caught the swap
    assert cp.completed == []
    assert storage.deleted == ["uploads/us_w"]


def test_reject_path_transient_fail_keeps_source_for_reclaim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If fail() blips transiently on the reject path, the source must NOT be deleted (so the lease
    # recovery re-admits + re-rejects with the same code) and the error must not escape run_once.
    def _raise(p: object) -> None:
        raise ff.FfmpegError("input container 'hls' refused (SSRF guard)")

    monkeypatch.setattr(adm.ff, "assert_allowed_input_format", _raise)
    job = make_job(job_id="job_e", upload_session_id="us_e", output_mode="dub_only")
    cp = FakeControlPlane(
        config=TEST_CONFIG, claims=[Claim(job=job, claim_version=1, attempt=1)], fail_error=True
    )
    storage = FakeStorage({"uploads/us_e": b"\x00disguised"})
    run_once(cp, storage, workdir_base=tmp_path, config=TEST_CONFIG)  # must NOT raise
    assert storage.deleted == []  # source kept — the reclaim re-reads + re-rejects it
    assert cp.completed == []
    assert not (tmp_path / "job_e__1").exists()  # workdir still cleaned
