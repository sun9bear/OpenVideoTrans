"""Atomic JSON read/write for the file-based, resumable pipeline.

Pure stdlib. The pipeline writes its own artifacts and reads them back through
the ``ovt_schemas`` Pydantic contracts, so the round trip is exact; these
helpers only handle the on-disk file (atomic replace, UTF-8, stable indent).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically (tmp file + replace)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def read_json(path: str | Path) -> dict[str, Any]:
    """Read and parse a JSON object from ``path``."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
