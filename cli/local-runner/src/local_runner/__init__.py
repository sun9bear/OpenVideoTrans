"""local-runner — the Tier-1 local CLI (T1.4 = M1).

The orchestrator that wires autodub-core's ``run_pipeline`` to the provider-adapters ``Resolver``
and runs the admission / supply-chain gates the kernel cannot (it has no import edge to
provider-adapters). Entry point: ``local_runner.cli:main`` (the ``ovt`` console script).
"""

from __future__ import annotations

from .admission import Admission, admit
from .ingest import IngestError, fetch_source
from .runner import RunResult, run_job

__all__ = ["Admission", "IngestError", "RunResult", "admit", "fetch_source", "run_job"]
