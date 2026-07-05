"""T1.3g — supply-chain pinning + model-license gate (red line: only permissive,
integrity-pinned artifacts ship in the default self-host image).

Two guards:

  1. **integrity pin (sha256)** — a vendored binary/model (ffmpeg, a piper ``.onnx`` voice)
     is verified against an expected SHA-256 before use. A mismatch (tampered / wrong /
     truncated download) raises ``SupplyChainError`` rather than running an unverified
     binary. Expected hashes come from the curated ``_PINNED`` table or, for an
     operator-downloaded artifact, ``FVD_<NAME>_SHA256``. An *unpinned* artifact is refused
     (fail-closed) — there is no "run whatever's on disk" path.

  2. **license gate** — a model whose license forbids commercial use (XTTS / F5-TTS /
     OpenVoice, all CC-BY-NC-style) is **blocked from the default image**. Permissive models
     (piper MIT, whisper MIT) are fine. Opt-in to a non-commercial model requires an explicit
     ``acknowledged=True`` (an audited acknowledgment — mirrors the §3 AIGC-marking disable
     gate), never a silent default.

No autodub-core import edge (T1.1 ∥ T1.2). The verify/gate functions are called by the
image build / doctor preflight / a future model-fetch path, not on the synthesis hot path
(hashing a large ``.onnx`` per call would be wasteful); they return an ``ovt_schemas.ModelRef``
so the worker can record the pin in ``manifest.json``.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from ovt_schemas.contracts import ModelRef

from ._env import env

_CHUNK = 1 << 20  # 1 MiB streaming read so a large model isn't slurped into memory


class SupplyChainError(RuntimeError):
    """An artifact failed its integrity pin or violated the default-image license policy."""


@dataclass(frozen=True)
class PinnedArtifact:
    sha256: str
    license_id: str


# Curated pins for artifacts vendored into the default image. Real hashes are filled when an
# artifact is vetted in; until then operators pin their own download via FVD_<NAME>_SHA256.
# (Left empty here: no binary is committed to this repo to hash against.)
#
# P1b-bake will ADD an entry per baked multi-voice Piper voice, keyed by its rhasspy BASENAME
# (e.g. "en_US-ryan-medium" -> PinnedArtifact(<real v1.0.0 sha256>, "MIT")). verify_piper_voice
# reads THIS table only (NOT the FVD_<NAME>_SHA256 env override): the basename comes from a file in
# FVD_PIPER_VOICES_DIR, so honoring a same-named env var would let an actor who controls both the
# file and the env forge a pin. The curated table is the sole pin source for the baked set.
_PINNED: dict[str, PinnedArtifact] = {}

# Bundled-model license classification. Permissive => default-image-OK; the CC-BY-NC family
# => blocked from the default image (opt-in needs an audited acknowledgment).
_MODEL_LICENSES: dict[str, str] = {
    "piper": "MIT",
    "whisper": "MIT",
    "faster_whisper": "MIT",
    "edge_tts": "proprietary-free",  # hosted MS service, no bundled weights
    "xtts": "coqui-cpml",  # non-commercial
    "xtts-v2": "coqui-cpml",
    "f5-tts": "cc-by-nc-4.0",  # non-commercial
    "f5_tts": "cc-by-nc-4.0",
    "openvoice": "cc-by-nc-4.0",  # non-commercial
}

_NON_COMMERCIAL_LICENSES = frozenset(
    {"coqui-cpml", "cc-by-nc", "cc-by-nc-sa", "cc-by-nc-2.0", "cc-by-nc-4.0", "non-commercial"}
)


def sha256_file(path: str) -> str:
    """Streaming SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def verify_sha256(path: str, expected_sha256: str) -> str:
    """Verify ``path`` hashes to ``expected_sha256`` (case-insensitive hex). Returns the
    verified digest; raises ``SupplyChainError`` on mismatch."""
    actual = sha256_file(path)
    if actual.lower() != expected_sha256.strip().lower():
        raise SupplyChainError(
            f"sha256 mismatch for {path!r}: expected {expected_sha256.strip().lower()}, "
            f"got {actual} — refusing to use an unverified artifact"
        )
    return actual


def expected_sha256(name: str) -> str | None:
    """Expected pin for a logical artifact ``name``: an ``FVD_<NAME>_SHA256`` override wins,
    else the curated ``_PINNED`` entry. ``None`` when the artifact is not pinned anywhere."""
    override = env(f"FVD_{name.upper()}_SHA256")
    if override:
        return override
    pin = _PINNED.get(name)
    return pin.sha256 if pin else None


def verify_pinned(name: str, path: str) -> ModelRef:
    """Verify a pinned artifact and return a ``ModelRef`` for the manifest. Fails closed when
    the artifact is unpinned (no expected hash) — there is no unverified-run path."""
    exp = expected_sha256(name)
    if exp is None:
        raise SupplyChainError(
            f"{name!r} is not pinned: set FVD_{name.upper()}_SHA256 to the vetted sha256 "
            f"(refusing to use an unpinned artifact)"
        )
    digest = verify_sha256(path, exp)
    return ModelRef(name=name, version=None, sha256=digest)


def verify_piper_model(model_path: str) -> ModelRef:
    """Integrity-pin a piper ``.onnx`` voice model before it is used."""
    return verify_pinned("piper_model", model_path)


def verify_piper_voice(basename: str, path: str) -> ModelRef:
    """Integrity-pin ONE multi-voice Piper ``.onnx`` (P1b) against the CURATED ``_PINNED`` table
    ONLY. The pin key is the rhasspy model BASENAME (e.g. ``en_US-ryan-medium``) — derived from a
    filename in ``FVD_PIPER_VOICES_DIR``, which an actor able to drop/rename files there controls.
    So, unlike the fixed-name single-model pin (``verify_piper_model``/``verify_pinned``), this path
    deliberately does NOT honor the ``FVD_<NAME>_SHA256`` env override: the basename AND the env are
    both attacker-influenceable, and pairing them would let a swapped voice carry a forged pin.
    Fails closed when the basename is unpinned in ``_PINNED``."""
    pin = _PINNED.get(basename)
    if pin is None:
        raise SupplyChainError(
            f"piper voice {basename!r} is not pinned in the curated _PINNED table: refusing an "
            f"unpinned multi-voice model (the FVD_*_SHA256 env override is intentionally not "
            f"honored for a filename-derived pin name)"
        )
    digest = verify_sha256(path, pin.sha256)
    return ModelRef(name=basename, version=None, sha256=digest)


def verify_piper_voices_dir(voices_dir: str) -> list[ModelRef]:
    """Integrity-pin EVERY installed ``<voices_dir>/*.onnx`` (P1b multi-voice mode). Each model must
    carry a curated pin keyed by its basename; ANY unpinned or hash-mismatched voice fails closed
    (``SupplyChainError``) before synthesis. Returns a ``ModelRef`` per voice for the manifest.

    This is the F1 fix: admission/doctor previously enforced the T1.3g pin by reading only
    ``FVD_PIPER_MODEL`` (unset in multi-voice mode), so the dir's models ran unverified. An empty
    dir raises — enabling multi-voice Piper with no installed model is a misconfiguration, not a
    silent no-op. Only the ``.onnx`` weights are pinned (mirrors the single-model path, which pins
    weights, not the small ``.onnx.json`` config). Callers are one-shot (CLI admission / doctor
    preflight), so hashing the whole baked set once per invocation is fine — not a per-job path."""
    onnx = sorted(Path(voices_dir).glob("*.onnx"))
    if not onnx:
        raise SupplyChainError(
            f"no *.onnx in FVD_PIPER_VOICES_DIR {voices_dir!r}: refusing to enable multi-voice "
            f"Piper with no installed model"
        )
    return [verify_piper_voice(p.stem, str(p)) for p in onnx]


def verify_ffmpeg() -> ModelRef:
    """Integrity-pin the ffmpeg binary on PATH before it is used."""
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise SupplyChainError("ffmpeg not found on PATH; cannot verify its pin")
    return verify_pinned("ffmpeg", binary)


def license_for(model_id: str) -> str | None:
    """The classified license id for a known bundled model, else ``None`` (unvetted)."""
    return _MODEL_LICENSES.get(model_id.lower())


def is_non_commercial(license_id: str) -> bool:
    """True for a CC-BY-NC-style / explicitly non-commercial license."""
    lic = license_id.strip().lower()
    return lic in _NON_COMMERCIAL_LICENSES or "-nc" in lic or "noncommercial" in lic


def assert_default_image_allowed(model_id: str, *, acknowledged: bool = False) -> None:
    """License gate for a model entering the default self-host image. Fails closed for an
    unvetted model; refuses a non-commercial model unless ``acknowledged`` (audited opt-in)."""
    lic = license_for(model_id)
    if lic is None:
        raise SupplyChainError(
            f"model {model_id!r} is not license-vetted; refusing it in the default image"
        )
    if is_non_commercial(lic) and not acknowledged:
        raise SupplyChainError(
            f"model {model_id!r} is {lic} (non-commercial) and is blocked from the default "
            f"image; opt-in requires an audited acknowledgment (acknowledged=True)"
        )
