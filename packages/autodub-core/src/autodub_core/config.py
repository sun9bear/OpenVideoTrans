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
# Burned-in subtitles (re-encode with libass) are an M2.1 fast-follow; the kernel
# carries a guarded placeholder and the front-end disables the option in M1
# (T1.3d). Flipping this on without the M2.1 burn implementation is a programming
# error — the mux placeholder raises rather than silently shipping plain video.
BURN_SUBTITLES_ENABLED = False
