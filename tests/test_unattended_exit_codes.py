"""Phase 3 tests for unattended mode (#163): exit code taxonomy.

Covers FR-4 from docs/unattended-mode-mvp.md:

| Code | Meaning                              | status        |
|------|--------------------------------------|---------------|
| 0    | Run completed successfully           | succeeded     |
| 1    | Run failed (PRD, sandbox, review)    | failed        |
| 20   | Bad input / configuration error      | failed        |
| 30   | Interrupted by signal                | interrupted   |

These tests exercise the orchestrator's classification path; signal-driven
interruption is tested end-to-end in phase 4.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from ralph_pp.cli import main
from ralph_pp.orchestrator import Orchestrator
from ralph_pp.unattended import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    Classification,
    ErrorInfo,
    classify_error,
)

# ── classify_error unit tests ────────────────────────────────────────────


class TestClassifyError:
    def test_click_usage_error_is_config_20(self) -> None:
        import click

        c = classify_error(click.UsageError("bad flag"))
        assert isinstance(c, Classification)
        assert c.status == "failed"
        assert c.exit_code == EXIT_CONFIG_ERROR
        assert c.error.category == "config"
        assert c.error.retriable is False

    def test_value_error_is_config_20(self) -> None:
        c = classify_error(ValueError("config validation failed"))
        assert c.exit_code == EXIT_CONFIG_ERROR
        assert c.error.category == "config"

    def test_file_not_found_is_config_20(self) -> None:
        c = classify_error(FileNotFoundError("missing prd.json"))
        assert c.exit_code == EXIT_CONFIG_ERROR
        assert c.error.category == "config"

    def test_max_cycles_abort_is_review_1(self) -> None:
        from ralph_pp.steps.prd import MaxCyclesAbort

        c = classify_error(MaxCyclesAbort())
        assert c.exit_code == EXIT_FAILURE
        assert c.error.category == "review"

    def test_prd_parse_error_is_prd_1(self) -> None:
        from ralph_pp.steps.sandbox import PrdParseError

        c = classify_error(PrdParseError("bad json"))
        assert c.exit_code == EXIT_FAILURE
        assert c.error.category == "prd"

    def test_keyboard_interrupt_is_interrupted_30(self) -> None:
        c = classify_error(KeyboardInterrupt())
        assert c.status == "interrupted"
        assert c.exit_code == EXIT_INTERRUPTED

    def test_unknown_runtime_error_is_internal_1(self) -> None:
        c = classify_error(RuntimeError("something broke"))
        assert c.exit_code == EXIT_FAILURE
        assert c.error.category == "internal"

    def test_message_falls_back_to_class_name(self) -> None:
        # An exception whose str() is empty should still get a useful message.
        class _Anon(Exception):
            pass

        c = classify_error(_Anon())
        assert c.error.message == "_Anon"


# ── Orchestrator integration: result file reflects classification ────────


def _make_orchestrator(
    tmp_path: Path,
    *,
    result_file: Path | None = None,
) -> Orchestrator:
    from ralph_pp.config import (
        Config,
        OrchestratedConfig,
        PostReviewConfig,
        RalphConfig,
    )

    cfg = Config.__new__(Config)
    cfg.repo_path = tmp_path
    cfg.ralph = RalphConfig(mode="delegated", max_iterations=1, sandbox_tool="claude")
    cfg.post_review = PostReviewConfig(max_cycles=1)
    cfg.hooks = {}
    cfg.orchestrated = OrchestratedConfig()
    return Orchestrator(
        "test-feat",
        cfg,
        unattended=True,
        run_id="01HXYZ" + "0" * 20,
        result_file=result_file,
    )


@pytest.mark.parametrize(
    "step_exception, expected_exit, expected_category",
    [
        (FileNotFoundError("missing"), EXIT_CONFIG_ERROR, "config"),
        (ValueError("bad config"), EXIT_CONFIG_ERROR, "config"),
        (RuntimeError("sandbox crashed"), EXIT_FAILURE, "internal"),
    ],
)
class TestOrchestratorResultClassification:
    @patch.object(Orchestrator, "_step_cleanup")
    @patch.object(Orchestrator, "_step_prd")
    @patch.object(Orchestrator, "_step_worktree")
    @patch("ralph_pp.orchestrator.validate_sandbox_prerequisites")
    def test_categorisation_round_trips_to_result(
        self,
        _val: object,
        _wt: object,
        _prd: object,
        _clean: object,
        tmp_path: Path,
        step_exception: BaseException,
        expected_exit: int,
        expected_category: str,
    ) -> None:
        result_path = tmp_path / "r.json"
        orch = _make_orchestrator(tmp_path, result_file=result_path)
        with (
            patch.object(Orchestrator, "_step_sandbox", side_effect=step_exception),
            pytest.raises(type(step_exception)),
        ):
            orch.run()
        data = json.loads(result_path.read_text())
        assert data["exit_code"] == expected_exit
        assert data["error"]["category"] == expected_category


class TestKeyboardInterruptPathway:
    """KeyboardInterrupt is a BaseException; verify the orchestrator's
    finally captures it via sys.exc_info and writes status=interrupted."""

    @patch.object(Orchestrator, "_step_cleanup")
    @patch.object(Orchestrator, "_step_prd")
    @patch.object(Orchestrator, "_step_worktree")
    @patch("ralph_pp.orchestrator.validate_sandbox_prerequisites")
    def test_keyboard_interrupt_writes_interrupted_result(
        self,
        _val: object,
        _wt: object,
        _prd: object,
        _clean: object,
        tmp_path: Path,
    ) -> None:
        result_path = tmp_path / "r.json"
        orch = _make_orchestrator(tmp_path, result_file=result_path)
        with (
            patch.object(Orchestrator, "_step_sandbox", side_effect=KeyboardInterrupt()),
            pytest.raises(KeyboardInterrupt),
        ):
            orch.run()
        data = json.loads(result_path.read_text())
        assert data["status"] == "interrupted"
        assert data["exit_code"] == EXIT_INTERRUPTED


# ── CLI integration: process exits with the classified code ──────────────


class TestCliExitCodes:
    def _stub_orchestrator(self, monkeypatch: pytest.MonkeyPatch, side_effect: BaseException):
        """Patch Orchestrator so .run() raises the given exception."""
        from ralph_pp import cli as cli_mod

        class _StubOrch:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def run(self, **_kwargs: object) -> None:
                raise side_effect

        monkeypatch.setattr(cli_mod, "Orchestrator", _StubOrch)

    def test_runtime_error_exits_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        self._stub_orchestrator(monkeypatch, RuntimeError("boom"))

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
                "--result-file",
                str(tmp_path / "r.json"),
            ],
        )
        assert result.exit_code == EXIT_FAILURE, result.output

    def test_value_error_exits_20(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        self._stub_orchestrator(monkeypatch, ValueError("config bad"))

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
                "--result-file",
                str(tmp_path / "r.json"),
            ],
        )
        assert result.exit_code == EXIT_CONFIG_ERROR, result.output

    def test_keyboard_interrupt_exits_30(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        self._stub_orchestrator(monkeypatch, KeyboardInterrupt())

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
                "--result-file",
                str(tmp_path / "r.json"),
            ],
        )
        assert result.exit_code == EXIT_INTERRUPTED, result.output

    def test_non_unattended_runtime_error_propagates_normally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --unattended we don't translate exit codes — the existing
        Click behaviour (non-zero, with traceback printed) is preserved."""
        repo = tmp_path / "repo"
        repo.mkdir()
        self._stub_orchestrator(monkeypatch, RuntimeError("boom"))

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "run",
                "--feature",
                "test-feat",
                "--repo",
                str(repo),
            ],
        )
        # Click surfaces the exception via .exception; the exit code from
        # CliRunner matches whatever Click would have used.
        assert result.exception is not None
        # The classifier-driven 1/20/30 mapping must NOT apply outside
        # unattended mode.
        assert not isinstance(result.exception, SystemExit) or result.exit_code != 20


def _early_failure_helper(args: list[str], tmp_path: Path) -> tuple[int, Path]:
    """Run ``main`` with the given args; return exit code and the cwd-fallback
    result path that should have been written."""
    runner = CliRunner()
    result = runner.invoke(main, args, catch_exceptions=False)
    # When unattended falls into the cwd fallback we cannot predict the run id
    # without intercepting it; just glob for it.
    candidates = list(tmp_path.glob("ralph-result-*.json"))
    return result.exit_code, candidates[0] if candidates else Path("/nonexistent")


class TestEarlyFailureWritesResult:
    """A parse-time conflict in unattended mode must still produce a result
    file (FR-2 promises an always-written result whenever unattended is on)."""

    def test_manual_prd_conflict_writes_cwd_fallback_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(tmp_path)

        exit_code, result_path = _early_failure_helper(
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
            tmp_path,
        )

        assert exit_code == EXIT_CONFIG_ERROR
        assert result_path.is_file(), "expected a cwd-fallback result file"
        data = json.loads(result_path.read_text())
        assert data["status"] == "failed"
        assert data["exit_code"] == EXIT_CONFIG_ERROR
        assert data["error"]["category"] == "config"


class TestErrorInfoSerialisation:
    def test_error_info_round_trips(self) -> None:
        e = ErrorInfo(category="sandbox", message="m", retriable=True)
        from dataclasses import asdict

        assert asdict(e) == {"category": "sandbox", "message": "m", "retriable": True}
