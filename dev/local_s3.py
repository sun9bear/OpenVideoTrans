"""A minimal in-memory S3-compatible object store for the LOCAL dev loop (DEVLOOP #25).

⚠️ LOCAL DEV ONLY. This stub does NO authentication and NO signature verification — it exists so the
whole upload→claim→complete loop can run on one box with no cloud R2. It must NEVER be deployed or
reachable from anything but localhost. It lives under dev/ (not in any `src/`), so it is never
part of the hosted worker / control-plane bundle.

The real data plane is R2's S3 API (SigV4-signed PUT/GET/HEAD/DELETE on `{bucket}/{key}`). Both the
control plane (presigned URLs, verifyUpload HEAD/delete) and the worker (S3 client) speak that, so a
dumb path-style store that ignores the signature is a faithful enough stand-in for the dev loop: the
browser PUTs, the worker GET/PUTs, and the control plane HEAD/deletes, all against this one process.

Stdlib only. Run standalone: `python dev/local_s3.py --port 9000`. Import for tests: `LocalS3`.
"""
from __future__ import annotations

import argparse
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import urlsplit


class _Store(ThreadingHTTPServer):
    """The HTTP server carrying the in-memory object map (path -> (bytes, content_type))."""

    def __init__(self, addr: tuple[str, int]) -> None:
        super().__init__(addr, _Handler)
        self.lock = threading.Lock()
        self.objects: dict[str, tuple[bytes, str]] = {}


class _Handler(BaseHTTPRequestHandler):
    # Path-style /{bucket}/{key}; the presigned query string (?X-Amz-...) is irrelevant to the stub.
    def _key(self) -> str:
        return urlsplit(self.path).path

    def _store(self) -> _Store:
        return cast(_Store, self.server)

    def log_message(self, format: str, *args: object) -> None:  # base sig; silence per-req noise
        return

    def do_PUT(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "application/octet-stream")
        store = self._store()
        with store.lock:
            store.objects[self._key()] = (body, ctype)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        store = self._store()
        with store.lock:
            obj = store.objects.get(self._key())
        if obj is None:
            self._not_found()
            return
        body, ctype = obj
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        store = self._store()
        with store.lock:
            obj = store.objects.get(self._key())
        if obj is None:
            self._not_found()
            return
        body, ctype = obj
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def do_DELETE(self) -> None:
        store = self._store()
        with store.lock:
            store.objects.pop(self._key(), None)  # idempotent
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _not_found(self) -> None:
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class LocalS3:
    """A running local S3 stub on an ephemeral port, controllable from tests / the orchestrator."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = _Store((host, port))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> LocalS3:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        host, port = self._server.server_address[0], self.port
        return f"http://{host}:{port}"

    def object_count(self) -> int:
        with self._server.lock:
            return len(self._server.objects)


def main() -> int:
    parser = argparse.ArgumentParser(description="Local in-memory S3 stub (dev loop only).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    server = _Store((args.host, args.port))
    print(f"local_s3 listening on http://{args.host}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
