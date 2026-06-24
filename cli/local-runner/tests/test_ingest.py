"""T1.4 — source ingest: local file passthrough, scheme refusal, and the yt-dlp-missing path
(URL ingest is opened only here). Test-first.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from local_runner import ingest
from local_runner.ingest import IngestError, fetch_source


def test_local_file_passes_through(tmp_path: Path) -> None:
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00")
    assert fetch_source(str(f), tmp_path / "work") == f


def test_missing_local_file_raises(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="not found"):
        fetch_source(str(tmp_path / "nope.mp4"), tmp_path / "work")


def test_non_http_scheme_refused(tmp_path: Path) -> None:
    for src in ("file:///etc/passwd", "ftp://host/x.mp4", "data:text/plain,hi"):
        with pytest.raises(IngestError, match="unsupported source scheme"):
            fetch_source(src, tmp_path / "work")


def test_url_without_yt_dlp_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest.shutil, "which", lambda _name: None)  # yt-dlp absent
    with pytest.raises(IngestError, match="needs yt-dlp"):
        fetch_source("https://example.com/v.mp4", tmp_path / "work")


def test_pick_merged_excludes_merge_intermediates(tmp_path: Path) -> None:
    # @CodeX review P2: bv*+ba/b leaves video-only/audio-only intermediates (media.fNNN.*); the
    # picker must select the MERGED deliverable (media.mp4), not a sort-first intermediate.
    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / "media.f137.mp4").write_bytes(b"v")  # video-only intermediate (sorts first)
    (dl / "media.f140.m4a").write_bytes(b"a")  # audio-only intermediate
    (dl / "media.mp4").write_bytes(b"final")  # the merged deliverable
    assert ingest._pick_merged(dl).name == "media.mp4"
