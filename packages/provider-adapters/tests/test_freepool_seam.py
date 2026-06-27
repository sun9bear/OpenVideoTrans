"""FREE-POOL (#23) seam boundary lint — the circuit-breaker ROUTING module stays PURE.

Backlog acceptance: "provider-adapters 不 import control-plane/D1/KV（CI 边界 lint）". The
state lives in the control-plane (D1/KV); provider-adapters only EATS a snapshot and RETURNS
a result (AD-13 — the free-pool state must never entangle the kernel/BYOK/Tier-3 path). This
is enforced structurally: parse circuit.py's AST and assert it imports nothing outside a tiny
pure allowlist — no network/IO, no control-plane/worker reach-in. A regression that makes the
router do I/O (or import the worker's control-plane client) fails THIS test, loudly, in CI.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.redline

_CIRCUIT = Path(__file__).resolve().parent.parent / "src" / "provider_adapters" / "circuit.py"

# circuit.py may import ONLY these (pure stdlib for the dataclasses/types + the in-package
# ladder for the free/paid sets). Anything else — requests/urllib/http/socket/os/json, or a
# reach into media_worker/apps/control_plane — is a seam violation.
_ALLOWED_ABSOLUTE = {
    "__future__", "dataclasses", "collections", "collections.abc", "typing", "enum",
}
_ALLOWED_RELATIVE = {"ladder"}

# Names that, if imported, prove the routing module has left its lane (IO / control-plane reach).
_FORBIDDEN = {
    "requests", "urllib", "http", "socket", "os", "sys", "json", "subprocess",
    "media_worker", "control_plane", "apps", "sqlite3", "pathlib",
}


def _imports(tree: ast.AST) -> tuple[set[str], set[str]]:
    absolute: set[str] = set()
    relative: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                absolute.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:  # `from .ladder import ...`
                if node.module:
                    relative.add(node.module)
            elif node.module:
                absolute.add(node.module)
    return absolute, relative


def test_circuit_module_exists() -> None:
    assert _CIRCUIT.is_file(), f"expected the routing module at {_CIRCUIT}"


def test_circuit_imports_are_pure() -> None:
    tree = ast.parse(_CIRCUIT.read_text(encoding="utf-8"))
    absolute, relative = _imports(tree)

    forbidden_hits = {m for m in absolute | relative if m.split(".")[0] in _FORBIDDEN}
    assert not forbidden_hits, f"circuit.py imports forbidden modules: {sorted(forbidden_hits)}"

    extra_absolute = absolute - _ALLOWED_ABSOLUTE
    assert not extra_absolute, (
        f"circuit.py imports unexpected absolute modules: {sorted(extra_absolute)}"
    )

    extra_relative = relative - _ALLOWED_RELATIVE
    assert not extra_relative, (
        f"circuit.py imports unexpected in-package modules: {sorted(extra_relative)}"
    )
