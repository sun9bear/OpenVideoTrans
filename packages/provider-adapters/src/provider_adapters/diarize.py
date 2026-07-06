"""Speaker diarization adapter (P4, 分角色配音) — sherpa-onnx, pure ONNX/CPU.

Runs the pyannote-segmentation-3.0 algorithm + a speaker-embedding model and clusters the
result into single-speaker turns, returned as ``(start_ms, end_ms, "SPEAKER_NN")`` tuples for
the kernel's ``diarize`` stage to relabel transcript lines with. No torch, no GPU.

Boundary (plan §5): the sherpa-onnx wheel + the ONNX model files live HERE, in provider-adapters
— ``autodub-core`` NEVER imports sherpa-onnx (keeps the kernel lightweight / network-free /
GPU-free). The kernel declares only the ``DiarizerProvider`` shape (``autodub_core.providers``)
and takes an instance by injection into ``run_pipeline(diarizer=...)``; the worker constructs this
under its RAM mutex — never two model-loaded CPU tasks synthesizing at once (plan §7 constraint 1).

This is a FREE, local provider (no paid variant), so it is deliberately NOT routed through the
paid-API ``select()`` gate / ``AUTO_LADDER``: the worker builds it directly with ``build_diarizer``
and injects it ONLY when it is ``available()`` (wheel installed + models present). Model paths and
clustering knobs come from the environment (the worker bakes the models in and points these at
them, like ``FVD_PIPER_VOICES_DIR``); a caller/test may override them via constructor kwargs.
"""

from __future__ import annotations

import contextlib
import wave
from pathlib import Path
from typing import Any

from ._env import env
from .base import ProviderInfo, ProviderUnavailable, _BaseProvider, has_module

# Model files + clustering knobs (worker/CLI config). A speech.wav extracted by the kernel is
# 16 kHz mono — the sample rate pyannote-segmentation-3.0 expects (see ``_read_wav_mono_f32``).
_SEG_MODEL_ENV = "FVD_DIARIZE_SEGMENTATION_MODEL"  # pyannote-segmentation-3.0 .onnx
_EMB_MODEL_ENV = "FVD_DIARIZE_EMBEDDING_MODEL"  # speaker-embedding .onnx (3D-Speaker zh default)
_NUM_SPEAKERS_ENV = "FVD_DIARIZE_NUM_SPEAKERS"  # >0 fixes the cluster count; else auto (threshold)
_THRESHOLD_ENV = "FVD_DIARIZE_CLUSTER_THRESHOLD"  # distance threshold when auto (default 0.8)
_THREADS_ENV = "FVD_DIARIZE_NUM_THREADS"  # onnxruntime intra-op threads (default 1; 2-core box)


def _int_env(name: str) -> int | None:
    raw = env(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _float_env(name: str, default: float) -> float:
    raw = env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _speaker_label(speaker: int) -> str:
    """sherpa speaker index (0-based int) -> the kernel's ``SPEAKER_NN`` label (matches the ASR
    single-speaker baseline ``SPEAKER_00`` — a 1-speaker diarization reproduces today's labels)."""
    return f"SPEAKER_{int(speaker):02d}"


def _segments_to_turns(segments: Any) -> list[tuple[int, int, str]]:
    """Map sherpa diarization segments (``.start`` / ``.end`` float seconds, ``.speaker`` int) to
    the kernel's ``(start_ms, end_ms, speaker_id)`` turns: round seconds to ms, drop non-positive
    spans, and sort by (start, end, speaker). Pure — no sherpa / model / numpy needed, so the label
    + rounding contract is unit-tested against fakes."""
    turns: list[tuple[int, int, str]] = []
    for seg in segments:
        start_ms = int(round(float(seg.start) * 1000))
        end_ms = int(round(float(seg.end) * 1000))
        if end_ms <= start_ms:
            continue
        turns.append((start_ms, end_ms, _speaker_label(seg.speaker)))
    turns.sort(key=lambda t: (t[0], t[1], t[2]))
    return turns


def _read_wav_mono_f32(path: str, expected_sr: int) -> Any:
    """Read a PCM wav as a 1-D mono float32 array in [-1, 1] at ``expected_sr`` (what sherpa's
    ``process`` consumes). Raises ``ProviderUnavailable`` on a sample-rate mismatch (the kernel
    extracts speech.wav at 16 kHz mono, so this holds — a mismatch means a mis-staged input; we add
    no resampler here) or an unsupported sample width."""
    import numpy as np  # type: ignore[import-not-found]  # lazy; ships with sherpa-onnx

    with contextlib.closing(wave.open(path, "rb")) as w:
        n_channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        framerate = w.getframerate()
        raw = w.readframes(w.getnframes())
    if framerate != expected_sr:
        raise ProviderUnavailable(
            f"diarizer expects {expected_sr} Hz audio but {path} is {framerate} Hz; the kernel "
            f"extracts speech.wav at 16 kHz mono — re-run ingest/prepare (no resampler here)."
        )
    dtype = {2: np.int16, 4: np.int32}.get(sampwidth)
    if dtype is None:
        # The kernel writes 16-bit PCM; 8-bit (unsigned) / 24-bit are not produced here, so reject
        # rather than mis-scale them.
        raise ProviderUnavailable(f"diarizer: unsupported wav sample width {sampwidth} bytes")
    data = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if n_channels > 1:
        data = data.reshape(-1, n_channels).mean(axis=1)
    max_val = float(1 << (8 * sampwidth - 1))  # full-scale magnitude for signed PCM
    return (data / max_val).astype(np.float32)


class SherpaOnnxDiarizer(_BaseProvider):
    """Offline speaker diarization via sherpa-onnx. Satisfies ``autodub_core.providers``'
    ``DiarizerProvider`` by shape (``info`` + ``diarize``); reuses ``_BaseProvider`` for
    ``name`` / ``_ensure_available`` but is NOT registered (free, single, local — no ``select``)."""

    info = ProviderInfo(
        "sherpa_onnx", "diarizer", paid=False,
        requires=(
            f"pip install sherpa-onnx + segmentation/embedding ONNX models "
            f"({_SEG_MODEL_ENV} + {_EMB_MODEL_ENV})"
        ),
        languages="language-agnostic (embedding model tuned for zh-Hans by default)",
        notes="local ONNX/CPU; opt-in; run under the worker RAM mutex (not concurrent with synth)",
    )

    def __init__(
        self,
        *,
        segmentation_model: str | None = None,
        embedding_model: str | None = None,
        num_speakers: int | None = None,
        cluster_threshold: float | None = None,
        num_threads: int | None = None,
    ) -> None:
        # None -> read the environment (worker/CLI config), like the Piper voices dir.
        self._seg = segmentation_model or env(_SEG_MODEL_ENV)
        self._emb = embedding_model or env(_EMB_MODEL_ENV)
        self._num_speakers = (
            num_speakers if num_speakers is not None else _int_env(_NUM_SPEAKERS_ENV)
        )
        # 0.8 = sherpa-onnx's own recommended FastClustering threshold for pyannote-segmentation-3.0
        # (its bundled example) — validated: on the canonical 4-speaker sample 0.5 over-clusters to
        # 5, while 0.6-0.8 recover the correct 4. Higher merges more; env-tunable per deployment.
        self._threshold = (
            cluster_threshold if cluster_threshold is not None else _float_env(_THRESHOLD_ENV, 0.8)
        )
        self._num_threads = (
            num_threads if num_threads is not None else (_int_env(_THREADS_ENV) or 1)
        )

    def available(self) -> bool:
        """True only when the wheel is importable AND both ONNX model files exist — so the worker
        can gate injection on this and never inject a diarizer that would fail mid-job."""
        return bool(
            has_module("sherpa_onnx")
            and self._seg and Path(self._seg).is_file()
            and self._emb and Path(self._emb).is_file()
        )

    def _build_sd(self) -> Any:
        import sherpa_onnx  # type: ignore[import-not-found]  # lazy; heavy ONNX wheel

        # num_clusters=-1 lets sherpa auto-pick the speaker count using `threshold`; a positive
        # FVD_DIARIZE_NUM_SPEAKERS fixes it (P4c may surface this as `max_speakers`).
        num_clusters = self._num_speakers if (self._num_speakers and self._num_speakers > 0) else -1
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=self._seg),
                num_threads=self._num_threads,
                provider="cpu",  # no GPU on the target box (plan §1)
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=self._emb, num_threads=self._num_threads, provider="cpu",
            ),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=num_clusters, threshold=self._threshold,
            ),
        )
        if not config.validate():
            raise ProviderUnavailable(
                f"{self.info.name}: invalid sherpa-onnx diarization config "
                f"(check {_SEG_MODEL_ENV} / {_EMB_MODEL_ENV})"
            )
        return sherpa_onnx.OfflineSpeakerDiarization(config)

    def diarize(self, audio_path: str) -> list[tuple[int, int, str]]:
        """Diarize ``audio_path`` (the kernel's speech.wav) into speaker turns. Fails loud
        (``ProviderUnavailable``) when unconfigured — the worker only injects an ``available()``
        diarizer, so a raise here means a genuine deploy misconfiguration, not a routine miss."""
        self._ensure_available()
        sd = self._build_sd()
        samples = _read_wav_mono_f32(audio_path, sd.sample_rate)
        result = sd.process(samples).sort_by_start_time()
        return _segments_to_turns(result)


def build_diarizer(**kwargs: Any) -> SherpaOnnxDiarizer:
    """Construct the diarizer the worker injects into ``run_pipeline(diarizer=...)``.

    A thin factory (no registry / ``select()`` — free, single, local provider). The worker builds
    it and injects ONLY when ``available()``; the kernel then relabels speakers. Kwargs override the
    environment (segmentation_model / embedding_model / num_speakers / cluster_threshold /
    num_threads) for explicit worker config + tests."""
    return SherpaOnnxDiarizer(**kwargs)
