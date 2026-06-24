"""T1.4 — runner orchestration: the source-hint normalization that protects the ASR detection
backfill. Test-first (no ffmpeg needed: run_pipeline / fetch_source are stubbed).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from local_runner import runner as rmod
from provider_adapters import ProviderUnavailable


class _NoTtsResolver:
    """subtitle_only needs no TTS; any select() call would be a bug, so it raises."""

    def select(self, kind: str, requested: str | None, allow_paid: bool):  # noqa: ANN201, ARG002
        raise ProviderUnavailable("no provider")


@pytest.mark.parametrize("hint,expected", [("auto", None), ("  AUTO ", None), ("", None),
                                           (None, None), ("pt-BR", "pt-BR")])
def test_run_job_normalizes_auto_or_blank_source_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hint: str | None, expected: str | None
) -> None:
    # @CodeX CLI P2: "auto"/blank must reach run_pipeline as None (else ASR takes it as a code and
    # MT prefers it over the detected language, disabling the detection backfill).
    captured: dict[str, object] = {}
    monkeypatch.setattr(rmod, "fetch_source", lambda src, wd: tmp_path / "src.mp4")  # noqa: ARG005
    monkeypatch.setattr(rmod, "run_pipeline",
                        lambda *a, **k: captured.update(k) or (tmp_path / "out.srt"))  # noqa: ARG005
    rmod.run_job(source="src.mp4", target_lang="es", out_dir=str(tmp_path / "job"),
                 output_mode="subtitle_only", source_hint=hint, resolver=_NoTtsResolver())
    assert captured["source_lang"] == expected
