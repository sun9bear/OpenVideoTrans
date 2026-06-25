"""Make the (non-installed) ``spike`` validation package importable: add apps/control-plane to
sys.path. The spike is a T2.0 tool, not a uv workspace member, so it isn't editable-installed."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # -> apps/control-plane
