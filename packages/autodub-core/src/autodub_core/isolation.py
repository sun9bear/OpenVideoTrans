"""Job isolation + the kernel-side paid-API pin (T1.3a, red line).

Two concerns, both security-critical:

* **Namespace isolation / path containment.** A job's on-disk root is
  ``base/<owner_id>/<job_id>``. ``owner_id`` and ``job_id`` come from the control
  plane / CLI and are attacker-influenced, so they are *validated* (single safe
  path components) and the assembled root is *containment-checked* against the
  jobs base — two owners/jobs never collide and neither id can traverse out of
  the base via ``..`` / separators / absolute or drive-qualified paths.
* **allow_paid pinned False.** The kernel already passes ``allow_paid=False`` from
  every stage; ``pin_resolver`` wraps the injected resolver so the invariant
  cannot regress — any ``select(..., allow_paid=True)`` reaching the kernel is a
  programming error and is refused *here*, before the underlying resolver (and
  its own paid gate) is ever consulted (CLAUDE.md red line §1/§14 — ``allow_paid``
  is constant False, §14 不可改).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, overload

from .providers import AsrProvider, MtProvider, Resolver, TtsProvider


class PathEscapeError(ValueError):
    """A job_id / owner_id / artifact path would escape the jobs root."""


class PaidPinViolation(RuntimeError):
    """A ``select`` reached the kernel with ``allow_paid`` not False (red line §1/§14)."""


# --------------------------------------------------------------------------- #
# Namespace isolation / path containment
# --------------------------------------------------------------------------- #
_ILLEGAL_IN_COMPONENT = ("/", "\\", "\x00", ":")


def safe_component(value: str, *, label: str) -> str:
    """Validate a single trusted-but-untrusted path component (owner_id / job_id).

    Rejects anything that could traverse, absolutize, or *collide* the path:
    empty, surrounding whitespace, a parent/self reference, a path separator, a
    NUL, a drive/UNC marker, or a trailing dot. The value is NOT trimmed — surrounding
    whitespace is rejected, not silently stripped, because Windows normalizes
    trailing dots/spaces, so ``'u.'`` / ``'u '`` would otherwise collide with ``'u'``
    and break the non-colliding-namespace guarantee (these ids are attacker-influenced).
    """
    if not value or value != value.strip():
        raise PathEscapeError(
            f"{label} must be a non-empty component with no surrounding whitespace: {value!r}")
    if value in (".", ".."):
        raise PathEscapeError(f"{label} must not be a parent/self reference: {value!r}")
    if value != value.rstrip("."):
        # Windows strips trailing dots from names: 'u.' resolves to the same dir as 'u'.
        raise PathEscapeError(f"{label} must not end with a dot (Windows normalizes it): {value!r}")
    if ".." in value or any(ch in value for ch in _ILLEGAL_IN_COMPONENT):
        raise PathEscapeError(f"{label} contains an illegal path character: {value!r}")
    return value


def ensure_within(root: str | Path, candidate: str | Path) -> Path:
    """Return the resolved ``candidate`` iff it stays within ``root``; else raise.

    Resolves both (collapsing ``..`` and symlinks) before the containment check,
    so this is the canonical anti-traversal guard for any path built from
    external input. ``candidate == root`` is allowed (the root contains itself).
    """
    root_r = Path(root).resolve()
    cand_r = Path(candidate).resolve()
    if not cand_r.is_relative_to(root_r):
        raise PathEscapeError(f"path escapes job root: {candidate!r} is not within {root!r}")
    return cand_r


def job_root(base_dir: str | Path, owner_id: str, job_id: str) -> Path:
    """Build the isolated on-disk root for one job: ``base/<owner_id>/<job_id>``.

    ``owner_id`` / ``job_id`` are validated as single safe components and the
    final path is containment-checked against ``base`` — two jobs never collide
    and neither id can traverse out of the jobs base (T1.3a path-escape guard).
    """
    base = Path(base_dir).resolve()
    owner = safe_component(owner_id, label="owner_id")
    job = safe_component(job_id, label="job_id")
    return ensure_within(base, base / owner / job)


# --------------------------------------------------------------------------- #
# allow_paid pin (kernel boundary backstop)
# --------------------------------------------------------------------------- #
class _PaidPinnedResolver:
    """Wraps the injected resolver and pins ``allow_paid=False`` at the kernel edge.

    Structural backstop over the per-stage hard-False: a ``select`` arriving with
    ``allow_paid`` other than ``False`` is refused before the inner resolver is
    reached, so the red line can never regress through a future kernel change.
    """

    def __init__(self, inner: Resolver) -> None:
        self._inner = inner

    @overload
    def select(self, kind: Literal["asr"], requested: str | None,
               allow_paid: bool) -> AsrProvider: ...
    @overload
    def select(self, kind: Literal["mt"], requested: str | None,
               allow_paid: bool) -> MtProvider: ...
    @overload
    def select(self, kind: Literal["tts"], requested: str | None,
               allow_paid: bool) -> TtsProvider: ...

    def select(self, kind: str, requested: str | None,
               allow_paid: bool) -> AsrProvider | MtProvider | TtsProvider:
        if allow_paid is not False:
            raise PaidPinViolation(
                f"the Tier-1 kernel never enables paid providers (allow_paid must be "
                f"False; got {allow_paid!r}) — CLAUDE.md red line §1/§14"
            )
        return self._inner.select(kind, requested, allow_paid)  # type: ignore[call-overload]


def pin_resolver(resolver: Resolver) -> Resolver:
    """Wrap ``resolver`` so the kernel can never select a paid provider (§1/§14)."""
    return _PaidPinnedResolver(resolver)
