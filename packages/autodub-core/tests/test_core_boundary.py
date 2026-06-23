"""autodub-core hard-boundary lint (STEP0-C, red line AD-13/14 — plan §4/§14).

autodub-core MUST NOT import gateway / control-plane / billing / payment /
entitlement / real credentials. This statically scans the package source (AST) for
forbidden import roots across every import form. It passes now (clean placeholder)
and MUST keep passing through the T1.1 port — the guardrail that lands *before* the
port (CodeX P1.1).

Coverage: ``import X``, ``from X import y``, ``from .X import y``, the bare relative
``from . import X`` / ``from .. import X`` (submodule in the alias list), and dynamic
``importlib.import_module("X")`` / ``__import__("X")`` with a string-literal argument.
Non-literal dynamic imports (computed module names) cannot be caught statically and
are out of scope — they would be conspicuous in code review.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.redline

_CORE_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "autodub_core"

# Forbidden import roots: anything across the autodub-core hard boundary.
# Singular "credential" subsumes "credentials" / "*_credential*" via substring match.
_FORBIDDEN = (
    "gateway",
    "control_plane",
    "controlplane",
    "billing",
    "payment",
    "entitlement",
    "credential",
    "secrets_store",
)


def _imported_module_roots(src: str) -> set[str]:
    """Module names a Python source imports, across every import form."""
    tree = ast.parse(src)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                # `from gateway import X` / `from .gateway import X` — module path (substring)
                mods.add(node.module)
            # Imported names may themselves be a forbidden submodule: `from pkg import gateway`
            # (module 'pkg' is clean) or `from . import gateway` (no module). Exact-match the
            # forbidden tokens so innocent symbols like `gateway_helper` are NOT flagged.
            mods.update(a.name for a in node.names if a.name.lower() in _FORBIDDEN)
        elif isinstance(node, ast.Call):
            # Dynamic string-literal imports: importlib.import_module("x") / __import__("x").
            fn = node.func
            is_dynamic_import = (isinstance(fn, ast.Attribute) and fn.attr == "import_module") or (
                isinstance(fn, ast.Name) and fn.id == "__import__"
            )
            if is_dynamic_import and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    mods.add(first.value)
    return mods


def _forbidden_hits(mods: set[str]) -> list[str]:
    """Subset of module names that cross the hard boundary."""
    return sorted(m for m in mods if any(token in m.lower() for token in _FORBIDDEN))


def test_core_imports_stay_inside_boundary() -> None:
    """No autodub-core source imports gateway / control-plane / billing / credentials."""
    assert _CORE_SRC.is_dir(), f"autodub_core source not found at {_CORE_SRC}"
    offenders: list[str] = []
    for py in sorted(_CORE_SRC.rglob("*.py")):
        hits = _forbidden_hits(_imported_module_roots(py.read_text(encoding="utf-8")))
        offenders += [f"{py.name}: imports {m}" for m in hits]
    assert not offenders, "autodub-core boundary violation: " + "; ".join(offenders)


# ── Regression fixtures: lock the lint against the forms that previously slipped ──

@pytest.mark.parametrize(
    "src",
    [
        "import gateway",
        "from gateway import Client",
        "from .gateway import Client",
        "from . import gateway",
        "from .. import billing",
        "from billing.api import charge",
        "import payment_processor",
        "from integrations import gateway",  # clean module, forbidden imported submodule (CodeX P2)
        "from .providers import payment",  # clean module, forbidden imported submodule (CodeX P2)
        "importlib.import_module('gateway')",
        "__import__('billing')",
    ],
)
def test_boundary_lint_flags_forbidden_forms(src: str) -> None:
    """Every forbidden import form (incl. bare relative + dynamic literal) is caught."""
    assert _forbidden_hits(_imported_module_roots(src)), f"missed forbidden import: {src!r}"


@pytest.mark.parametrize(
    "src",
    [
        "import json",
        "from typing import Any",
        "from . import pipeline",
        "from .media import ffprobe",
        "import importlib",
        "from foo import gateway_helper",  # a SYMBOL named *gateway* from an innocent module
        "importlib.import_module('autodub_core.media')",
    ],
)
def test_boundary_lint_allows_clean_forms(src: str) -> None:
    """Innocent imports (incl. a symbol that merely contains a forbidden token) pass."""
    assert not _forbidden_hits(_imported_module_roots(src)), f"false positive on: {src!r}"
