"""T2.4 — worker ffprobe re-admission: the authoritative source gate at claim time.

The control plane HEAD-checks the uploaded object's size/type at POST /api/jobs, but the presigned
PUT stays valid for its short TTL, so a client can swap the object after that HEAD and before the
worker claims (a TOCTOU swap; routed from T2.1). This module re-reads the ACTUAL downloaded bytes
and re-checks them authoritatively before any processing: container format / SSRF allowlist (reuses
autodub-core's T1.3c guard), real byte size vs the cap, and duration vs the per-output_mode cap. A
violation raises SourceRejected(error_code); the claim loop deletes the source + fails the job.
"""
from __future__ import annotations

from pathlib import Path

from autodub_core import ffmpeg_utils as ff
from ovt_schemas import Job

from .config import WorkerConfig


class SourceRejected(Exception):
    """The source failed re-admission. error_code is the Job error_code the worker reports."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def admit_source(path: Path, job: Job, config: WorkerConfig) -> None:
    """Re-admit the downloaded source; raise SourceRejected on a format/size/duration violation.

    Order matters: the format / SSRF allowlist runs FIRST so a disguised playlist's demuxer is
    never asked to compute a duration (that probe could dereference sub-resources). ffprobe
    failures map to an error_code, never leaking ffmpeg/path text to the control plane.
    """
    # 1. container format / SSRF allowlist — playlist/concat/network demuxer refused (T1.3c guard).
    try:
        ff.assert_allowed_input_format(path)
    except ff.FfmpegError as exc:
        raise SourceRejected("unsupported_format") from exc
    # 2. size immutability: re-check the ACTUAL bytes. The control-plane HEAD may have passed a
    #    smaller object that was then swapped via the still-valid presigned PUT.
    if path.stat().st_size > config.max_upload_bytes:
        raise SourceRejected("upload_too_large")
    # A burned/both subtitle delivery runs a video re-encode (like a dub): bind it by the tighter
    # dub-class cap AND require a video stream — computed once, used by both gates below.
    burns = job.subtitle_delivery in ("burned", "both") and job.output_mode in (
        "subtitle_only",
        "both",
    )
    # 3. duration vs the per-mode hard cap. advisory_duration_ms is sort-only; THIS is the enforced
    #    cap — a lie to jump the queue is stopped here. A burn job uses the tighter dub-class cap
    #    (NOT the loose srt-only cap): a 30-min burn re-encode would blow the small box.
    try:
        duration_ms = ff.probe_duration_ms(path)
    except ff.FfmpegError as exc:
        raise SourceRejected("unsupported_format") from exc
    cap_mode = "dub_only" if burns else job.output_mode
    if duration_ms > config.duration_cap_sec(cap_mode) * 1000:
        raise SourceRejected("over_duration")
    # 4. burned subtitles (M2.1) paint onto pixels, so a burn job NEEDS a video stream. Uploads
    #    allow audio/*; an audio-only source with burned/both delivery can't be burned, so reject
    #    with a coded terminal instead of failing deep in the libass re-encode.
    if burns:
        try:
            ff.probe_dimensions(path)  # raises FfmpegError when the source has no video stream
        except ff.FfmpegError as exc:
            raise SourceRejected("unsupported_format") from exc
