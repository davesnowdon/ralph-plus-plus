"""Phase 1 tests for unattended mode (#163).

These cover only the foundation: flag plumbing, env-var equivalence,
conflict rejection, console replacement, and run-id round-trip into the
``Orchestrator`` constructor. Result-file, exit-code taxonomy, and signal
handling are exercised in later phases.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from ralph_pp import unattended
from ralph_pp.cli import main

ANSI_ESCAPE_RE = re.compile(r"\x1b\[")


def _stub_run(*_args: object, **_kwargs: object) -> None:
    """No-op stand-in for ``Orchestrator.run`` so tests stop at construction."""


class TestUnattendedFlag:
    def test_flag_is_propagated_to_orchestrator(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("ralph_pp.cli.Orchestrator") as mock_orch:
            mock_orch.return_value.run = _stub_run
            result = runner.invoke(
                main,
                [
                    "run",
                    "--feature",
                    "test-feat",
                    "--repo",
                    str(repo),
                    "--unattended",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        kwargs = mock_orch.call_args.kwargs
        assert kwargs["unattended"] is True
        # A ULID is 26 characters of Crockford base32.
        assert isinstance(kwargs["run_id"], str)
        assert len(kwargs["run_id"]) == 26

    def test_env_var_activates_mode(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner(env={"RALPH_UNATTENDED": "1"})
        with patch("ralph_pp.cli.Orchestrator") as mock_orch:
            mock_orch.return_value.run = _stub_run
            result = runner.invoke(
                main,
                [
                    "run",
                    "--feature",
                    "test-feat",
                    "--repo",
                    str(repo),
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        kwargs = mock_orch.call_args.kwargs
        assert kwargs["unattended"] is True

    def test_run_id_round_trips(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("ralph_pp.cli.Orchestrator") as mock_orch:
            mock_orch.return_value.run = _stub_run
            result = runner.invoke(
                main,
                [
                    "run",
                    "--feature",
                    "test-feat",
                    "--repo",
                    str(repo),
                    "--unattended",
                    "--run-id",
                    "custom-run-123",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        assert mock_orch.call_args.kwargs["run_id"] == "custom-run-123"

    def test_result_file_round_trips(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        result_path = tmp_path / "out.json"

        runner = CliRunner()
        with patch("ralph_pp.cli.Orchestrator") as mock_orch:
            mock_orch.return_value.run = _stub_run
            result = runner.invoke(
                main,
                [
                    "run",
                    "--feature",
                    "test-feat",
                    "--repo",
                    str(repo),
                    "--unattended",
                    "--result-file",
                    str(result_path),
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        assert mock_orch.call_args.kwargs["result_file"] == result_path

    def test_unattended_implies_non_interactive(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("ralph_pp.cli.Orchestrator") as mock_orch:
            mock_orch.return_value.run = _stub_run
            result = runner.invoke(
                main,
                [
                    "run",
                    "--feature",
                    "test-feat",
                    "--repo",
                    str(repo),
                    "--unattended",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        cfg = mock_orch.call_args.kwargs["config"]
        assert cfg.non_interactive.enabled is True


class TestConflictRejection:
    def test_unattended_with_manual_prd_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        # The cwd-fallback result file lands in the current directory; chdir
        # so it doesn't pollute the repo root when tests run.
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "run",
                "--feature",
                "test-feat",
                "--repo",
                str(repo),
                "--unattended",
                "--manual-prd",
                "--dry-run",
            ],
        )

        # Per FR-4, configuration / parse-time conflicts in unattended mode
        # exit 20 (user-fixable) — not Click's default 2.
        assert result.exit_code == 20, result.output


class TestPlainOutput:
    def test_no_ansi_escapes_in_dry_run(self, tmp_path: Path) -> None:
        """The most measurable consequence of unattended mode: no ANSI codes."""
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("ralph_pp.cli.Orchestrator") as mock_orch:
            mock_orch.return_value.run = _stub_run
            result = runner.invoke(
                main,
                [
                    "run",
                    "--feature",
                    "test-feat",
                    "--repo",
                    str(repo),
                    "--unattended",
                    "--dry-run",
                ],
                color=True,  # ask Click for ANSI; unattended must still suppress
            )

        assert result.exit_code == 0, result.output
        assert not ANSI_ESCAPE_RE.search(result.output), (
            f"unattended output must contain no ANSI escapes, got: {result.output!r}"
        )

    def test_reconfigure_swaps_module_consoles(self) -> None:
        from ralph_pp import cli as cli_mod
        from ralph_pp import orchestrator as orch_mod

        original_cli = cli_mod.console
        original_orch = orch_mod.console
        try:
            unattended.reconfigure_consoles_for_unattended()
            assert cli_mod.console is not original_cli
            assert orch_mod.console is not original_orch
            # The replacement must be a no-color console.
            assert cli_mod.console.no_color is True
        finally:
            cli_mod.console = original_cli
            orch_mod.console = original_orch


class TestEnvHelper:
    def test_truthy_values(self, monkeypatch) -> None:
        for value in ("1", "true", "TRUE", "yes", "YES"):
            monkeypatch.setenv("RALPH_UNATTENDED", value)
            assert unattended.is_unattended_env() is True

    def test_falsy_values(self, monkeypatch) -> None:
        for value in ("", "0", "false", "no"):
            monkeypatch.setenv("RALPH_UNATTENDED", value)
            assert unattended.is_unattended_env() is False

    def test_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("RALPH_UNATTENDED", raising=False)
        assert unattended.is_unattended_env() is False


class TestRunIdGeneration:
    def test_generates_distinct_ulids(self) -> None:
        a = unattended.generate_run_id()
        b = unattended.generate_run_id()
        assert a != b
        assert len(a) == len(b) == 26


class TestStartupAndShutdownLogs:
    """Phase 5: a single startup + shutdown log line on stderr, plain-text."""

    def test_logs_appear_with_expected_fields(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from unittest.mock import patch as _patch

        from ralph_pp.config import (
            Config,
            OrchestratedConfig,
            PostReviewConfig,
            RalphConfig,
        )
        from ralph_pp.orchestrator import Orchestrator

        cfg = Config.__new__(Config)
        cfg.repo_path = tmp_path
        cfg.ralph = RalphConfig(mode="delegated", max_iterations=1, sandbox_tool="claude")
        cfg.post_review = PostReviewConfig(max_cycles=1)
        cfg.hooks = {}
        cfg.orchestrated = OrchestratedConfig()

        orch = Orchestrator(
            "test-feat",
            cfg,
            unattended=True,
            run_id="01HXYZ" + "0" * 20,
            result_file=tmp_path / "r.json",
        )

        with (
            _patch.object(Orchestrator, "_step_cleanup"),
            _patch.object(Orchestrator, "_step_post_review"),
            _patch.object(Orchestrator, "_step_sandbox"),
            _patch.object(Orchestrator, "_step_prd"),
            _patch.object(Orchestrator, "_step_worktree"),
            _patch("ralph_pp.orchestrator.validate_sandbox_prerequisites"),
        ):
            orch.run()

        err = capsys.readouterr().err
        assert "ralph++ started" in err
        assert "ralph++ finished" in err
        # Stable fields surface in the log.
        assert "run_id=" in err
        assert "feature=test-feat" in err
        assert "status=succeeded" in err
        assert "duration_seconds=" in err
        # No ANSI escapes in the log lines specifically.
        assert not ANSI_ESCAPE_RE.search(err), err
