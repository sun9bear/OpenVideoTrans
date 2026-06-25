"""Put tooling/guardrails on sys.path so the tests can import the (non-package) guardrail script."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
