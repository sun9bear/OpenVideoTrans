"""``ovt`` — the Tier-1 local command-line entry point (T1.4 = M1).

    ovt run <file|url> --target-lang zh-Hans [--output-mode both] [--source-lang en] ...
    ovt doctor

``run`` admits the job (fail-closed language + commercial-safe-dub gates), ingests the source
(local file or URL/yt-dlp), and drives the kernel end-to-end to a marked mp4 + srt. ``doctor``
reports provider availability and the supply-chain pin/license status.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Sequence

from autodub_core.ffmpeg_utils import FfmpegError
from provider_adapters import (
    LanguageError,
    PaidProviderBlocked,
    ProviderUnavailable,
    SupplyChainError,
)

from .doctor import run_doctor
from .ingest import IngestError
from .runner import run_job

_OUTPUT_MODES = ("subtitle_only", "dub_only", "both")


def _default_out_dir() -> str:
    """A UNIQUE job directory per run. The kernel caches stages by artifact existence (not keyed
    by source/target), so a shared default dir would let a second run return the first run's
    output (@CodeX). A timestamp + random suffix keeps each run isolated; pass --out to resume."""
    return os.path.join("ovt_job", f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ovt", description="OpenVideoTrans local runner")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="translate / dub a local file or URL to marked mp4 + srt")
    run.add_argument("source", help="a local media file path, or an http(s):// URL")
    run.add_argument("-t", "--target-lang", required=True, help="target BCP-47 locale (zh-Hans)")
    run.add_argument("-o", "--out", default=None,
                     help="job output directory (default: a unique ovt_job/<timestamp> dir)")
    run.add_argument("--output-mode", default="both", choices=_OUTPUT_MODES)
    run.add_argument("--subtitle-lang", default="target", choices=("target", "bilingual"))
    run.add_argument("--subtitle-delivery", default="srt", choices=("srt", "burned", "both"))
    run.add_argument("--source-lang", default=None, help="source-language hint (else auto-detect)")
    run.add_argument("--asr", default=None, help="force an ASR provider (else the $0 auto ladder)")
    run.add_argument("--mt", default=None, help="force an MT provider (else the $0 auto ladder)")
    run.add_argument("--separate", action="store_true", help="split ambient audio with demucs")

    sub.add_parser("doctor", help="check provider availability + supply-chain pins")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "doctor":
        return run_doctor()
    try:
        result = run_job(
            source=args.source,
            target_lang=args.target_lang,
            out_dir=args.out or _default_out_dir(),
            output_mode=args.output_mode,
            subtitle_lang=args.subtitle_lang,
            subtitle_delivery=args.subtitle_delivery,
            source_hint=args.source_lang,
            asr=args.asr,
            mt=args.mt,
            separate=args.separate,
        )
    except (LanguageError, PaidProviderBlocked) as exc:  # fail-closed REFUSAL (policy), exit 2
        code = getattr(exc, "code", "paid_provider_blocked")
        print(f"refused [{code}]: {exc}", file=sys.stderr)
        return 2
    except (IngestError, ProviderUnavailable, SupplyChainError, FfmpegError,
            NotImplementedError, FileNotFoundError) as exc:  # expected operational ERROR, exit 1
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"done [{result.admission.tts_provider or 'subtitle-only'}]: {result.primary}")
    return 0


if __name__ == "__main__":  # `python -m local_runner.cli`
    raise SystemExit(main())
