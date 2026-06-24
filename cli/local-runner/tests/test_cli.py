"""T1.4 — the ``ovt`` CLI: subcommand dispatch + fail-closed exit codes (refused jobs vs errors).
Test-first.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from local_runner import cli as climod
from local_runner.cli import _default_out_dir, main


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


def test_forced_paid_provider_is_clean_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # @CodeX review P2: --asr openai (paid) must be a clean coded REFUSAL (exit 2), not a traceback.
    # The red line still holds (select blocks it); this checks the CLI surfaces it legibly.
    from provider_adapters import PaidProviderBlocked

    def _blocked(**_kw: object) -> object:
        raise PaidProviderBlocked("'openai' is a paid provider; never invoked automatically")

    monkeypatch.setattr(climod, "run_job", _blocked)
    assert main(["run", "x.mp4", "-t", "es", "--asr", "openai"]) == 2
    assert "paid_provider_blocked" in capsys.readouterr().err


def test_ffmpeg_error_is_clean_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # @CodeX CLI P2: an ffmpeg validation failure must surface as the CLI's `error:` exit 1,
    # not an uncaught traceback.
    from autodub_core.ffmpeg_utils import FfmpegError

    def _boom(**_kw: object) -> object:
        raise FfmpegError("ffprobe refused the input format")

    monkeypatch.setattr(climod, "run_job", _boom)
    assert main(["run", "x.mp4", "-t", "es"]) == 1
    assert "error:" in capsys.readouterr().err


def test_default_out_dir_is_unique() -> None:
    # @CodeX CLI P1: each run gets an isolated job dir so the kernel's existence-cache can't
    # serve a previous run's output.
    assert _default_out_dir() != _default_out_dir()
    assert _default_out_dir().startswith("ovt_job")
