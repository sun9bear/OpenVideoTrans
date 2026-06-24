"""Atomic JSON read/write for the file-based, resumable pipeline.

Pure stdlib. The pipeline writes its own artifacts and reads them back through
the ``ovt_schemas`` Pydantic contracts, so the round trip is exact; these
helpers only handle the on-disk file (atomic replace, UTF-8, stable indent).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any


@contextlib.contextmanager
def atomic_output(final: str | Path) -> Iterator[Path]:
    """Yield a temp path next to ``final``; on clean exit, atomically replace
    ``final`` with it (and always clean the temp up).

    The whole pipeline caches stage outputs by file existence, so a crash must
    never leave a partial file at a cache path — an .exists() check would treat
    it as a valid cached artifact. The temp keeps ``final``'s suffix so
    format-by-extension tools (ffmpeg) still pick the right muxer.
    """
    final = Path(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.parent / (final.stem + ".part" + final.suffix)
    try:
        yield tmp
        tmp.replace(final)
    finally:
        tmp.unlink(missing_ok=True)


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically (temp file + replace)."""
    with atomic_output(path) as tmp:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: str | Path) -> dict[str, Any]:
    """Read and parse a JSON object from ``path``."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
