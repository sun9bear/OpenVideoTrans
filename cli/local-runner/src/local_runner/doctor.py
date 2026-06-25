"""``ovt doctor`` — a preflight that surfaces what's configured and enforces the supply-chain
gates the mechanisms in provider-adapters only *expose* (wiring the @CodeX finding that the
T1.3g pin/license gate had no caller):

  - lists every ASR/MT/TTS provider and whether it's available (keys/binaries present);
  - verifies the configured piper ``.onnx`` against its pinned SHA-256 (``verify_piper_model``);
  - runs the non-commercial license gate over the bundled models (``assert_default_image_allowed``).

It mutates nothing and never calls a paid provider — safe to run anytime.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from provider_adapters import (
    SupplyChainError,
    assert_default_image_allowed,
    probe,
    verify_ffmpeg,
    verify_piper_model,
)

_BUNDLED_MODELS = ("piper", "whisper", "faster_whisper", "edge_tts")


def run_doctor(out: Callable[[str], None] = print) -> int:
    """Print provider availability + supply-chain pin/license status. Returns 0 (always — doctor
    reports, it does not fail the shell), 1 if a configured piper model fails its pin."""
    ok = True
    out("OpenVideoTrans doctor - providers")
    for kind in ("asr", "mt", "tts"):
        out(f"  [{kind}]")
        for name, available, info in probe(kind):
            flag = "OK " if available else "-- "
            paid = " (paid, opt-in)" if info.paid else ""
            out(f"    {flag}{name}{paid}")

    out("supply-chain")
    model = os.getenv("FVD_PIPER_MODEL")
    if model:
        try:
            ref = verify_piper_model(model)
            out(f"  piper model pin: OK ({(ref.sha256 or '')[:12]})")
        except (SupplyChainError, OSError) as exc:
            # OSError/FileNotFoundError: a stale FVD_PIPER_MODEL pointing at a moved/deleted file
            # must be REPORTED as a failed pin, not crash doctor (@CodeX CLI).
            ok = False
            out(f"  piper model pin: FAIL - {exc}")
    else:
        out("  piper model pin: skipped (FVD_PIPER_MODEL unset)")

    # The ffmpeg binary is the other T1.3g-pinned artifact run_pipeline invokes; enforce its pin
    # here too (verify_ffmpeg had no caller before — @CodeX CLI), when the operator has set one.
    if os.getenv("FVD_FFMPEG_SHA256"):
        try:
            ref = verify_ffmpeg()
            out(f"  ffmpeg pin: OK ({(ref.sha256 or '')[:12]})")
        except (SupplyChainError, OSError) as exc:
            ok = False
            out(f"  ffmpeg pin: FAIL - {exc}")
    else:
        out("  ffmpeg pin: skipped (FVD_FFMPEG_SHA256 unset)")

    for name in _BUNDLED_MODELS:
        try:
            assert_default_image_allowed(name)
            out(f"  license[{name}]: allowed in default image")
        except SupplyChainError as exc:
            out(f"  license[{name}]: {exc}")

    return 0 if ok else 1
