"""autodub-core hard-boundary lint (STEP0-C, red line AD-13/14 — plan §4/§14).

autodub-core MUST NOT import gateway / control-plane / billing / payment /
entitlement / real credentials. This statically scans the package source (AST) for
forbidden import roots. It passes now (clean placeholder) and MUST keep passing
through the T1.1 port — this is the guardrail that lands *before* the port (CodeX P1.1).
"""
from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.redline

_CORE_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "autodub_core"

# Forbidden import roots: anything across the autodub-core hard boundary.
_FORBIDDEN = (
    "gateway",
    "control_plane",
    "controlplane",
    "billing",
    "payment",
    "entitlement",
    "credentials",
    "secrets_store",
)


def _imported_modules(path: pathlib.Path) -> set[str]:
    """All module names imported by a Python file (both ``import`` and ``from``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_core_imports_stay_inside_boundary() -> None:
    """No autodub-core source imports gateway / control-plane / billing / credentials."""
    assert _CORE_SRC.is_dir(), f"autodub_core source not found at {_CORE_SRC}"
    offenders: list[str] = []
    for py in sorted(_CORE_SRC.rglob("*.py")):
        for mod in _imported_modules(py):
            low = mod.lower()
            if any(token in low for token in _FORBIDDEN):
                offenders.append(f"{py.name}: imports {mod}")
    assert not offenders, "autodub-core boundary violation: " + "; ".join(offenders)
