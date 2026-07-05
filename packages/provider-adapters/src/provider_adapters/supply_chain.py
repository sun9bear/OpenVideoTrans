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


# Baked multi-voice Piper pins (P1b-bake): the rhasspy/piper-voices@v1.0.0 content sha256 for each
# voice baked into the default worker image, keyed by rhasspy BASENAME. These are the HF tree API
# `lfs.oid` (HuggingFace's authoritative stored content hash for the immutable v1.0.0 tag) — NOT
# computed from a local download, so committing them pins the image against later tag-drift / a
# tampered mirror / a corrupted fetch (build fails closed on mismatch). All piper voices are MIT.
# verify_piper_voice reads THIS table ONLY (never the FVD_<NAME>_SHA256 env override): the
# basename comes from a file in the voices dir, so a same-named env var would let an actor who
# controls both the file and the env forge a pin. The Dockerfile downloads exactly these
# into the voices dir and verifies each against this table at BUILD time; verify_piper_voices_dir
# re-checks at CLI admission / doctor. To add/repin a voice: update the HF `lfs.oid` here + the
# Dockerfile PIPER_VOICE_PATHS list together (a drift between them fails the build).
_PIPER_VOICE_SHA256: dict[str, str] = {
    "en_US-ryan-medium": "abf4c274862564ed647ba0d2c47f8ee7c9b717d27bdad9219100eb310db4047a",
    "en_US-amy-medium": "b3a6e47b57b8c7fbe6a0ce2518161a50f59a9cdd8a50835c02cb02bdd6206c18",
    "zh_CN-huayan-medium": "9929917bf8cabb26fd528ea44d3a6699c11e87317a14765312420be230be0f3d",
    "es_ES-davefx-medium": "6658b03b1a6c316ee4c265a9896abc1393353c2d9e1bca7d66c2c442e222a917",
    "es_ES-sharvard-medium": "40febfb1679c69a4505ff311dc136e121e3419a13a290ef264fdf43ddedd0fb1",
    "fr_FR-gilles-low": "5cd711846720e261c2a176f6924c198a7424d0a75dd4b0a5357a5fb9cb739285",
    "fr_FR-siwis-medium": "641d1ab097da2b81128c076810edb052b385decc8be3381814802a64a73baf99",
    "de_DE-thorsten-medium": "7e64762d8e5118bb578f2eea6207e1a35a8e0c30595010b666f983fc87bb7819",
    "de_DE-eva_k-x_low": "e88cf290fbfb768bf111330d2e8a46e376b0d85e3423a28bfebbc863a260dad8",
    "pt_BR-faber-medium": "858555e3a064209c57088fe6bd70c4c3dc54d03eaa00c45d5ecaf43a33f95aa7",
    "pt_BR-edresson-low": "de4cecee38b30bb1a6378a337af605d59f0c377df702c6a6752870db8991cd84",
    "ru_RU-dmitri-medium": "f073356ebc4bd0f80c5af58df2953a5988bd5bdab1eb38635ce960b071fbefcb",
    "ru_RU-irina-medium": "8ff38212d23da300bbe3705c645e6e5b9475f0bfde01558eb17813e22acaaaaa",
    "it_IT-paola-medium": "6fc918b5a0ea6137382833dddfa567bffbe6a5060c02043c87192ee59c04210c",
}

# Curated integrity pins for artifacts vendored into the default image (the baked Piper voice set +
# any future fixed-name artifact). A fixed-name single artifact may ALSO be operator-pinned via
# FVD_<NAME>_SHA256 (see expected_sha256); baked-voice basenames are the curated set only.
_PINNED: dict[str, PinnedArtifact] = {
    name: PinnedArtifact(sha256=digest, license_id="MIT")
    for name, digest in _PIPER_VOICE_SHA256.items()
}

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
