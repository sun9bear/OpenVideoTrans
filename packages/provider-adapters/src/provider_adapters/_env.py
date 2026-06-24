"""Environment-variable helpers for provider credentials/config.

Providers read their keys/config from the environment only — never from call
arguments, never written to disk (secrets hygiene). ``env`` treats a blank string
as unset so an empty exported var doesn't mask a missing key.
"""

from __future__ import annotations

import os


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if (v is not None and v.strip() != "") else default


def require_env(name: str) -> str:
    v = env(name)
    if v is None:
        raise RuntimeError(
            f"{name} is not set. Export it in the environment and re-run "
            f"(never paste secrets into a command)."
        )
    return v
