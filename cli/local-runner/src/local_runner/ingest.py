"""Source ingest for the local runner.

A local file passes straight through — the kernel's own ``ingest`` stage applies the T1.3c
format/protocol allowlist before extracting audio. URL ingest via yt-dlp is opened **only here**
(backlog "URL/yt-dlp 仅此开"); the kernel itself stays network-free. A URL-downloaded file is an
attacker-influenced new surface, so it is re-validated by the same T1.3c container allowlist
(``assert_allowed_input_format``) before the kernel touches it.

Network-level egress control (blocking private / link-local / IMDS hosts, playlist fan-out) is the
abuse-gate's job (T2.4); here we only disable playlists and refuse non-http(s) schemes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from autodub_core import ffmpeg_utils as ff

# A URI scheme is 2+ chars (``http``, ``file``, ``data``…). A SINGLE-char "scheme" is a Windows
# drive letter (``C:\videos\x.mp4``), which is a local path, not a URI.
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]+):")


class IngestError(RuntimeError):
    """The source could not be located / fetched / validated."""


def fetch_source(source: str, work_dir: Path) -> Path:
    """Resolve ``source`` to a local media file. A URL is downloaded (yt-dlp) into ``work_dir``
    and format-validated; a local path is existence-checked (the kernel validates its format)."""
    match = _SCHEME.match(source)
    scheme = match.group(1).lower() if match else None
    if scheme in ("http", "https"):
        return _fetch_url(source, work_dir)
    if scheme is not None:  # file:// / ftp:// / data: / s3:// … — refused (only http(s) or local)
        raise IngestError(f"unsupported source scheme: {source!r} (use http(s):// or a local path)")
    path = Path(source)
    if not path.exists():
        raise IngestError(f"source file not found: {source}")
    return path


def _fetch_url(url: str, work_dir: Path) -> Path:
    if shutil.which("yt-dlp") is None:
        raise IngestError("URL ingest needs yt-dlp; install it (pip install 'local-runner[url]')")
    work_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(work_dir / "download.%(ext)s")
    # --no-playlist: never fan a playlist URL into many downloads (abuse + surprise). Single item.
    proc = subprocess.run(
        ["yt-dlp", "--no-playlist", "--no-warnings", "--no-progress",
         "-f", "bv*+ba/b", "-o", out_tmpl, url],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise IngestError(f"yt-dlp failed: {proc.stderr[-400:]}")
    downloaded = next(iter(sorted(work_dir.glob("download.*"))), None)
    if downloaded is None:
        raise IngestError("yt-dlp produced no output file")
    ff.assert_allowed_input_format(downloaded)  # T1.3c allowlist on the fetched media
    return downloaded
