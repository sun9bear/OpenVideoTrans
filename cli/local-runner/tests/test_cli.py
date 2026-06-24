"""T1.4 — the ``ovt`` CLI: subcommand dispatch + fail-closed exit codes (refused jobs vs errors).
Test-first.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from local_runner.cli import main


def test_doctor_command_runs(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "doctor" in out and "supply-chain" in out


def test_run_unsupported_target_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    # fail-closed admission (LanguageError) -> exit 2 with the schema ErrorCode in the message.
    code = main(["run", "x.mp4", "-t", "tlh"])
    assert code == 2
    assert "unsupported_language_pair" in capsys.readouterr().err


def test_run_missing_source_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # a supported pair but a missing source file -> ingest error -> exit 1.
    code = main(["run", str(tmp_path / "nope.mp4"), "-t", "es",
                 "--output-mode", "subtitle_only", "-o", str(tmp_path / "job")])
    assert code == 1
    assert "error:" in capsys.readouterr().err


def test_missing_subcommand_errors() -> None:
    with pytest.raises(SystemExit):
        main([])
