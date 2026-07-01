"""Job-directory layout + canonical audio format + alignment tuning.

This is the file-based, resumable contract for the autodub-core pipeline:
every stage reads/writes artifacts under a job directory and is a no-op on
re-run when its output already exists (unless ``force=True``).

Red line (CLAUDE.md / plan §5, §14): a paid external API is NEVER auto-invoked.
The provider ladder, the paid-provider set, and the ``select()`` safety gate do
NOT live here — they are owned by ``provider-adapters`` (T1.2) and reach the
pipeline only through an injected resolver (see ``autodub_core.providers``).
autodub-core is the language-agnostic kernel: pure stdlib, no gateway / billing /
credentials, no knowledge of which providers cost money.
"""

from __future__ import annotations

from pathlib import Path


# --------------------------------------------------------------------------- #
# Job directory layout (file-based, resumable contract)
# --------------------------------------------------------------------------- #
class JobPaths:
    """Canonical artifact locations for one job, rooted at ``job_dir``."""

    def __init__(self, job_dir: str | Path):
        self.root = Path(job_dir)
        self.video = self.root / "video"
        self.audio = self.root / "audio"
        self.tts = self.root / "tts"
        self.output = self.root / "output"

    def ensure(self) -> JobPaths:
        for d in (self.root, self.video, self.audio, self.tts, self.output):
            d.mkdir(parents=True, exist_ok=True)
        return self

    # canonical artifacts
    @property
    def original_audio(self) -> Path:
        return self.audio / "original.wav"

    @property
    def speech(self) -> Path:
        return self.audio / "speech.wav"

    @property
    def ambient(self) -> Path:
        return self.audio / "ambient.wav"

    @property
    def transcript(self) -> Path:
        return self.root / "transcript.json"

    @property
    def segments(self) -> Path:
        return self.root / "segments.json"

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def dubbed_audio(self) -> Path:
        return self.output / "dubbed_audio_complete.wav"

    @property
    def dubbed_video(self) -> Path:
        return self.output / "dubbed_video.mp4"

    @property
    def subtitles(self) -> Path:
        return self.output / "subtitles.srt"

    @property
    def burned_video(self) -> Path:
        # M2.1: the libass re-encode deliverable (subtitles burned into the picture).
        return self.output / "burned_video.mp4"

    def original_video(self) -> Path | None:
        if not self.video.exists():
            return None
        for p in sorted(self.video.glob("original.*")):
            if ".part" in p.suffixes:  # skip an atomic_output temp (original.part.<ext>)
                continue
            return p
        return None

    def tts_raw(self, index: int, ext: str) -> Path:
        return self.tts / f"segment_{index:04d}.{ext.lstrip('.')}"

    def tts_aligned(self, index: int) -> Path:
        return self.tts / f"segment_{index:04d}_aligned.wav"

    def find_tts_raw(self, index: int) -> Path | None:
        for p in sorted(self.tts.glob(f"segment_{index:04d}.*")):
            if not p.name.endswith("_aligned.wav"):
                return p
        return None


# --------------------------------------------------------------------------- #
# Canonical audio format for the composed dubbed track
# --------------------------------------------------------------------------- #
CANON_SR = 48000
CANON_CHANNELS = 1
CANON_SAMPLE_FMT = "s16"

# Alignment tuning
MAX_SPEEDUP = 2.0          # never speed a segment up more than 2x (quality floor)
DEFAULT_CHARS_PER_SEC = 15.0   # fallback budget hint when no probe is available

# Output feature flags
# Burned-in subtitles (M2.1): a libass re-encode that paints the subtitle into the
# picture. Implemented in mux() + ff.burn_subtitles(); enabled by default now that
# the burn path exists (the front-end exposes the option). Ops may set this False to
# fall back to srt-only delivery (a 'burned' request then needs an srt channel).
BURN_SUBTITLES_ENABLED = True

# Burn re-encode caps — keep a burn job bounded on a small (2GB) box:
#  * BURN_MAX_WIDTH / BURN_MAX_HEIGHT: downscale a source that exceeds EITHER cap (keeping
#    aspect, even dims, never upscaling) BEFORE the libass overlay, so a large-area source —
#    incl. an ultra-wide / anamorphic frame whose height alone is within the cap — can't blow
#    up encode time/memory on a small box.
#  * BURN_ENCODE_TIMEOUT_SEC: kill a wedged re-encode rather than hold the lease forever
#    (the worker maps the resulting failure to a coded terminal, never a paid retry).
#  * BURN_CRF / BURN_PRESET: x264 quality / speed tradeoff tuned for a free, time-bounded box.
BURN_MAX_WIDTH = 1920
BURN_MAX_HEIGHT = 1080
BURN_ENCODE_TIMEOUT_SEC = 1800
BURN_CRF = 23
BURN_PRESET = "veryfast"
