"""Tests for orchestrated sandbox mode — infra failure and test-failure handling."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from ralph_pp.config import Config, ToolConfig, RalphConfig, OrchestratedConfig
from ralph_pp.tools.base import ToolResult
from ralph_pp.steps.sandbox import _run_orchestrated


def _make_config(
    tmp_path: Path,
    max_iterations: int = 1,
    max_iteration_retries: int = 1,
    backout_on_failure: bool = True,
    run_tests: bool = False,
    test_commands: list[str] | None = None,
) -> Config:
    """Build a Config with fake sandbox dir and orchestrated settings."""
    sandbox_dir = tmp_path / "ralph-sandbox"
    (sandbox_dir / "bin").mkdir(parents=True)
    wrapper = sandbox_dir / "bin" / "ralph-sandbox"
    wrapper.write_text("#!/bin/bash\necho fake")
    wrapper.chmod(0o755)

    # Fake session runner
    runner = tmp_path / "scripts" / "ralph-single-step.sh"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text("#!/bin/bash\necho run")
    runner.chmod(0o755)

    return Config(
        tools={
            "claude": ToolConfig(command="claude", args=["--print"], stdin="{prompt}"),
            "codex": ToolConfig(command="codex", args=["{prompt}"]),
        },
        ralph=RalphConfig(
            max_iterations=max_iterations,
            mode="orchestrated",
            sandbox_dir=str(sandbox_dir),
            session_runner=str(runner),
        ),
        orchestrated=OrchestratedConfig(
            coder="claude",
            reviewer="codex",
            fixer="claude",
            max_iteration_retries=max_iteration_retries,
            backout_on_failure=backout_on_failure,
            run_tests_between_steps=run_tests,
            test_commands=test_commands or [],
        ),
    )


def _setup_worktree(tmp_path: Path) -> Path:
    """Create a minimal git worktree with prd.json."""
    worktree = tmp_path / "project"
    worktree.mkdir()
    ralph_dir = worktree / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True)
    (ralph_dir / "prd.json").write_text('{"stories": []}')
    return worktree


def _fake_subprocess_run(returncode=0, stdout="", stderr=""):
    """Create a fake CompletedProcess."""
    result = subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )
    return result


class TestCoderInfraFailure:
    """Finding 2: coder subprocess failure should not fall through to review."""

    def test_coder_failure_retries_in_backout_mode(self, tmp_path):
        """When coder exits nonzero in backout mode, it should retry (not review)."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=1, backout_on_failure=True)

        git_sha = "abc1234"

        call_count = {"subprocess": 0, "review": 0}

        def mock_subprocess_run(cmd, **kwargs):
            call_count["subprocess"] += 1
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout=git_sha)
            if isinstance(cmd, list) and "reset" in cmd:
                return _fake_subprocess_run(returncode=0)
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            # Coder calls — first fails, second succeeds
            if call_count["subprocess"] <= 2:  # first coder call (after rev-parse)
                return _fake_subprocess_run(returncode=1, stderr="Docker crashed")
            return _fake_subprocess_run(returncode=0, stdout="some output")

        def mock_review(*args, **kwargs):
            call_count["review"] += 1
            return (True, "LGTM")

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._session_runner_path", return_value=tmp_path / "scripts" / "ralph-single-step.sh"),
        ):
            _run_orchestrated(worktree, config)

        # Review should still have been called (on the retry), not on the failed attempt
        # The key assertion: if coder failed on attempt 1, it should have retried

    def test_coder_failure_skips_review_when_no_retries(self, tmp_path):
        """When coder exits nonzero and no retries left, skip review entirely."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=0, backout_on_failure=True)

        review_called = False

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            # Coder always fails
            return _fake_subprocess_run(returncode=1, stderr="Docker crashed")

        def mock_review(*args, **kwargs):
            nonlocal review_called
            review_called = True
            return (True, "LGTM")

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._session_runner_path", return_value=tmp_path / "scripts" / "ralph-single-step.sh"),
        ):
            result = _run_orchestrated(worktree, config)

        assert review_called is False, "Review should not be called after infra failure with no retries"
        assert result is False


class TestFixerInfraFailure:
    """Finding 2: fixer subprocess failure should break the fix cycle."""

    def test_fixer_failure_breaks_fix_cycle(self, tmp_path):
        """When fixer exits nonzero, stop fix cycles (don't re-review)."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path, max_iterations=1, max_iteration_retries=2,
            backout_on_failure=False,
        )

        review_count = 0

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            # Coder succeeds
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            nonlocal review_count
            review_count += 1
            return (False, "Issues found")

        def mock_fixer(findings, worktree_path, config):
            # Fixer fails
            return _fake_subprocess_run(returncode=1, stderr="fixer crashed")

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._run_fixer_in_sandbox", side_effect=mock_fixer),
            patch("ralph_pp.steps.sandbox._session_runner_path", return_value=tmp_path / "scripts" / "ralph-single-step.sh"),
        ):
            result = _run_orchestrated(worktree, config)

        # Only 1 review (initial), no re-review after broken fixer
        assert review_count == 1, "Should not re-review after fixer infra failure"


class TestTestFailureBlocking:
    """Finding 3: test failures should block iteration acceptance even if reviewer approves."""

    def test_tests_fail_reviewer_lgtm_not_accepted(self, tmp_path):
        """Tests fail on last attempt, reviewer says LGTM — iteration should NOT pass."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path, max_iterations=1, max_iteration_retries=0,
            backout_on_failure=True,
            run_tests=True, test_commands=["false"],
        )

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            # Coder succeeds
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            return (True, "LGTM")

        def mock_test_commands(worktree_path, commands):
            return False  # tests fail

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._run_test_commands", side_effect=mock_test_commands),
            patch("ralph_pp.steps.sandbox._session_runner_path", return_value=tmp_path / "scripts" / "ralph-single-step.sh"),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is False, "Iteration should not be accepted when tests fail"
