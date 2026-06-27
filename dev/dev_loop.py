"""DEVLOOP (#25) — one command that runs the whole local upload→claim→complete loop with NO cloud.

`just dev` (or `python dev/dev_loop.py`) brings up three local processes and drives one real job end
to end:

  • local_s3      — an in-memory S3 stub (this process, a background thread)
  • cp_server.ts  — the control-plane Worker as a local HTTP server (better-sqlite3 D1 + in-mem KV),
                    object ops pointed at the stub via R2_S3_ENDPOINT
  • media-worker  — the REAL worker (`python -m media_worker`), pointed at the local CP + the stub

then, as a browser would: POST /api/uploads/sign → PUT the source → POST /api/jobs → poll until done
→ presigned GET the artifact, asserting the bytes round-trip. Exit 0 = loop healthy.

Prereqs: ffmpeg/ffprobe (the worker re-admits the source through ffprobe — no dev bypass of it)
and node + pnpm (the control-plane dev server runs via tsx). If a prereq is missing the loop SKIPs
loudly (exit 0) rather than failing — it is a dev accelerator, not a CI gate (the seams it relies on
are unit-tested in CI: test/devloop.test.ts + tests/test_devloop_storage.py).

Stdlib only.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_s3 import LocalS3  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
CP_DIR = REPO_ROOT / "apps" / "control-plane"
BUCKET = "ovt-media"
ANON = "anon_devloop"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(
    method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post_json(url: str, payload: dict[str, object], headers: dict[str, str]) -> tuple[int, dict]:
    h = {**headers, "Content-Type": "application/json"}
    status, raw = _http("POST", url, headers=h, body=json.dumps(payload).encode())
    return status, (json.loads(raw) if raw else {})


def _wait_ready(url: str, headers: dict[str, str], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _ = _http("GET", url, headers=headers)
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def _make_clip(path: Path) -> None:
    # A tiny real mp4 so the worker's ffprobe re-admission (T2.4) accepts it — we feed the gate
    # a real source, never bypass it. 1s well under the dub_only 5-min cap.
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=1:size=128x96:rate=10", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )


def _kill(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
    else:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _tail(path: Path, n: int = 20) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:])
    except OSError:
        return "(no output)"


def run() -> int:
    pnpm = shutil.which("pnpm")
    if (
        not shutil.which("ffmpeg")
        or not shutil.which("ffprobe")
        or not shutil.which("node")
        or not pnpm
    ):
        print("SKIP dev loop: needs ffmpeg + ffprobe + node + pnpm on PATH.")
        print("  (The seam tests run in CI; this full loop is a local accelerator.)")
        return 0

    token = "devloop-" + os.urandom(8).hex()
    cp_port = _free_port()
    cp_url = f"http://127.0.0.1:{cp_port}"
    worker_headers = {"Authorization": f"Bearer {token}"}
    actor_headers = {"X-OVT-Anon-Id": ANON}

    tmp = Path(tempfile.mkdtemp(prefix="ovt-devloop-"))
    cp_log, worker_log = tmp / "cp.log", tmp / "worker.log"
    s3 = LocalS3()
    cp_proc: subprocess.Popen[bytes] | None = None
    worker_proc: subprocess.Popen[bytes] | None = None
    try:
        s3.start()
        print(f"[*] local_s3      {s3.url}")

        cp_env = {**os.environ, "PORT": str(cp_port), "INTERNAL_TOKEN": token,
                  "R2_S3_ENDPOINT": s3.url, "R2_BUCKET": BUCKET}
        with cp_log.open("wb") as fh:
            cp_proc = subprocess.Popen([pnpm, "exec", "tsx", "dev/cp_server.ts"],
                                       cwd=CP_DIR, env=cp_env, stdout=fh, stderr=subprocess.STDOUT)
        if not _wait_ready(f"{cp_url}/internal/config", worker_headers):
            print(f"[FAIL] control-plane did not become ready.\n{_tail(cp_log)}")
            return 1
        print(f"[*] cp_server     {cp_url}")

        worker_env = {**os.environ, "OVT_CONTROL_PLANE_URL": cp_url, "OVT_INTERNAL_TOKEN": token,
                      "OVT_R2_ENDPOINT": s3.url, "OVT_WORKDIR": str(tmp / "jobs"),
                      "PYTHONUNBUFFERED": "1"}
        # Launch the worker via uv's workspace-member context so `media_worker` resolves even on a
        # fresh checkout / direct run (not only via `just dev`'s install); fall back to the current
        # interpreter when uv isn't on PATH (it always is via `just dev` / `uv run`).
        uv = shutil.which("uv")
        worker_cmd = (
            [uv, "run", "--package", "media-worker", "python", "-m", "media_worker"]
            if uv
            else [sys.executable, "-m", "media_worker"]
        )
        with worker_log.open("wb") as fh:
            worker_proc = subprocess.Popen(worker_cmd, env=worker_env, stdout=fh,
                                           stderr=subprocess.STDOUT)
        print("[*] media-worker  started")

        clip = tmp / "source.mp4"
        _make_clip(clip)
        data = clip.read_bytes()
        print(f"\n[1] upload    ({len(data)} bytes)")

        status, sign = _post_json(f"{cp_url}/api/uploads/sign",
                                  {"declared_bytes": len(data), "declared_type": "video/mp4"},
                                  actor_headers)
        if status != 200:
            print(f"[FAIL] sign failed: {status} {sign}")
            return 1
        put_status, _ = _http(
            "PUT", sign["put_url"], headers={"Content-Type": "video/mp4"}, body=data
        )
        if put_status not in (200, 204):
            print(f"[FAIL] PUT to stub failed: {put_status}")
            return 1

        status, created = _post_json(
            f"{cp_url}/api/jobs",
            {"upload_session_id": sign["upload_session_id"], "target_lang": "zh-Hans",
             "output_mode": "dub_only", "subtitle_delivery": "srt", "subtitle_lang": "target"},
            actor_headers,
        )
        if status != 201:
            print(f"[FAIL] create job failed: {status} {created}")
            return 1
        job_id = created["job"]["job_id"]
        print(f"[2] job       {job_id} created (queued)")

        deadline = time.monotonic() + 90
        state = "queued"
        while time.monotonic() < deadline:
            # Fast-fail if the worker died (e.g. `No module named media_worker` when the workspace
            # wasn't synced) instead of waiting out the timeout on a job that can never be claimed.
            if worker_proc.poll() is not None:
                code = worker_proc.returncode
                print(f"[FAIL] worker exited early (code {code}); tail:\n{_tail(worker_log)}")
                return 1
            _, jr = _http("GET", f"{cp_url}/api/jobs/{job_id}", headers=actor_headers)
            state = json.loads(jr)["job"]["status"]
            if state in ("done", "failed"):
                break
            time.sleep(0.5)
        print(f"[3] claim     worker reached status={state}")
        if state != "done":
            print(f"[FAIL] not done (status={state}); worker tail:\n{_tail(worker_log)}")
            return 1

        _, dl = _http("GET", f"{cp_url}/api/jobs/{job_id}/download/video", headers=actor_headers)
        art_status, art = _http("GET", json.loads(dl)["url"])
        if art_status != 200 or art != data:
            print(
                f"[FAIL] artifact mismatch (status={art_status}, {len(art)} bytes vs {len(data)})"
            )
            return 1

        print(f"[4] complete  artifact downloaded, {len(art)} bytes round-tripped\n")
        print("PASS: DEV LOOP -- upload -> claim -> complete, fully local, no cloud.")
        return 0
    finally:
        _kill(worker_proc)
        _kill(cp_proc)
        s3.stop()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(run())
