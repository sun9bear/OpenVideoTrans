"""End-to-end orchestration for one local job: admit -> ingest -> run_pipeline -> marked output.

This is the seam the kernel deliberately leaves to an orchestrator: the runner admits the job
(the T1.3f language gate + the commercial-safe dub choice), fetches the source (local file or
URL/yt-dlp), then drives autodub-core's ``run_pipeline`` with the provider-adapters ``Resolver``.
AIGC marking defaults ON (the kernel's ``run_pipeline`` applies ``default_marking`` when no
explicit marking/job is passed), so the deliverable is always marked.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autodub_core import JobPaths, run_pipeline
from provider_adapters import Resolver

from .admission import Admission, admit
from .ingest import fetch_source


@dataclass(frozen=True)
class RunResult:
    primary: Path  # the deliverable: dubbed mp4 (dub) or subtitles.srt (subtitle_only)
    admission: Admission


def run_job(
    *,
    source: str,
    target_lang: str,
    out_dir: str | Path,
    output_mode: str = "both",
    subtitle_lang: str = "target",
    subtitle_delivery: str = "srt",
    source_hint: str | None = None,
    asr: str | None = None,
    mt: str | None = None,
    separate: bool = False,
    resolver: object | None = None,
) -> RunResult:
    """Run one job end-to-end and return the primary deliverable path. Raises ``LanguageError``
    (fail-closed admission) / ``IngestError`` / ``ProviderUnavailable`` on failure."""
    resolver = resolver if resolver is not None else Resolver()
    # Normalize the source hint: a blank or literal "auto" means "no hint" — forwarding "auto"
    # would be taken by ASR as a language code and by MT over the detected language, disabling the
    # intended detection backfill (@CodeX CLI).
    hint = source_hint.strip() if source_hint else ""
    clean_hint = None if hint.lower() in ("", "auto") else hint
    # Admission BEFORE any fetch/transcode: fail closed on an unsupported pair, a forced paid/
    # unconfigured ASR/MT, or a dub with no commercial-safe voice — never download/encode for a
    # job that can't be delivered.
    adm = admit(
        target_lang=target_lang, output_mode=output_mode, resolver=resolver,
        source_hint=clean_hint, asr=asr, mt=mt,
    )
    paths = JobPaths(out_dir).ensure()
    local_source = fetch_source(source, paths.video)
    primary = run_pipeline(
        paths,
        resolver,  # type: ignore[arg-type] - structural Resolver (kernel depends on the Protocol)
        source=str(local_source),
        target_lang=target_lang,
        source_lang=clean_hint,  # normalized hint (None lets ASR detect + backfill)
        output_mode=output_mode,
        subtitle_lang=subtitle_lang,
        subtitle_delivery=subtitle_delivery,
        asr=asr,
        mt=mt,
        tts_provider=adm.tts_provider,  # admitted commercial-safe voice (None for subtitle-only)
        separate=separate,
    )
    return RunResult(primary=primary, admission=adm)
