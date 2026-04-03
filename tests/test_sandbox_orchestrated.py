"""Tests for orchestrated sandbox mode — infra failure and test-failure handling."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ralph_pp.config import Config, OrchestratedConfig, RalphConfig, ToolConfig
from ralph_pp.steps.sandbox import (
    ReviewResult,
    _run_fixer_in_sandbox,
    _run_orchestrated,
    _wrap_retry_findings,
)
from ralph_pp.tools.base import ToolResult


def _make_config(
    tmp_path: Path,
    max_iterations: int = 1,
    max_iteration_retries: int = 1,
    backout_on_failure: bool = True,
    backout_severity_threshold: str = "major",
    run_tests: bool = False,
    test_commands: list[str] | None = None,
) -> Config:
    """Build a Config with fake sandbox dir and orchestrated settings."""
    sandbox_dir = tmp_path / "ralph-sandbox"
    (sandbox_dir / "bin").mkdir(parents=True)
    wrapper = sandbox_dir / "bin" / "ralph-sandbox"
    wrapper.write_text("#!/bin/bash\necho fake")
    wrapper.chmod(0o755)
    (sandbox_dir / "docker-compose.yml").write_text("version: '3'\n")

    # Fake session runner
    runner = tmp_path / "scripts" / "ralph-single-step.sh"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text("#!/bin/bash\necho run")
    runner.chmod(0o755)

    return Config(
        tools={
            "claude": ToolConfig(
                command="claude",
                args=["--print"],
                stdin="{prompt}",
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash(git:*)"],
            ),
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
            backout_severity_threshold=backout_severity_threshold,
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
    (ralph_dir / "prd.json").write_text('{"userStories": []}')
    return worktree


def _fake_subprocess_run(returncode=0, stdout="", stderr=""):
    """Create a fake CompletedProcess."""
    result = subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )
    return result


def _coder_succeeds(cmd, **kwargs):
    """Subprocess mock where git helpers and coder all succeed."""
    if isinstance(cmd, list) and "rev-parse" in cmd:
        return _fake_subprocess_run(returncode=0, stdout="abc1234")
    if isinstance(cmd, list) and "diff" in cmd:
        return _fake_subprocess_run(returncode=0, stdout="some diff")
    return _fake_subprocess_run(returncode=0, stdout="coder output")


def _incrementing_sha():
    """Return a _get_head_sha mock that returns a new SHA each call.

    This prevents the idle-detection logic from treating iterations as no-ops.
    """
    counter = {"n": 0}

    def _get_sha(path):
        counter["n"] += 1
        return f"sha{counter['n']:04d}"

    return _get_sha


class TestCoderInfraFailure:
    """Finding 2: coder subprocess failure should not fall through to review."""

    def test_coder_failure_retries_in_backout_mode(self, tmp_path):
        """When coder exits nonzero in backout mode, retries and reviews only the success."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path, max_iterations=1, max_iteration_retries=1, backout_on_failure=True
        )

        git_sha = "abc1234"
        coder_call_count = 0
        review_call_count = 0
        backout_called = False

        def mock_subprocess_run(cmd, **kwargs):
            nonlocal coder_call_count, backout_called
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout=git_sha)
            if isinstance(cmd, list) and "reset" in cmd:
                backout_called = True
                return _fake_subprocess_run(returncode=0)
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            # Coder calls — first fails, second succeeds
            coder_call_count += 1
            if coder_call_count == 1:
                return _fake_subprocess_run(returncode=1, stderr="Docker crashed")
            return _fake_subprocess_run(returncode=0, stdout="some output")

        def mock_review(*args, **kwargs):
            nonlocal review_call_count
            review_call_count += 1
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        assert coder_call_count == 2, "Coder should have been called twice (initial + retry)"
        assert backout_called, "Backout should have been called after first failure"
        assert review_call_count == 1, "Review should only be called once (on successful retry)"

    def test_coder_failure_skips_review_when_no_retries(self, tmp_path):
        """When coder exits nonzero and no retries left, skip review entirely."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path, max_iterations=1, max_iteration_retries=0, backout_on_failure=True
        )

        review_called = False

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            # Coder always fails
            return _fake_subprocess_run(returncode=1, stderr="Docker crashed")

        def mock_review(*args, **kwargs):
            nonlocal review_called
            review_called = True
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert review_called is False, (
            "Review should not be called after infra failure with no retries"
        )
        assert result is False


class TestFixerInfraFailure:
    """Finding 2: fixer subprocess failure should break the fix cycle."""

    def test_fixer_failure_breaks_fix_cycle(self, tmp_path):
        """When fixer exits nonzero, stop fix cycles (don't re-review)."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path,
            max_iterations=1,
            max_iteration_retries=2,
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
            return ReviewResult(
                passed=False, findings="Issues found", max_severity=None, minor_only=False
            )

        def mock_fixer(findings, worktree_path, config, stories=""):
            # Fixer fails
            return _fake_subprocess_run(returncode=1, stderr="fixer crashed")

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._run_fixer_in_sandbox", side_effect=mock_fixer),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        # Only 1 review (initial), no re-review after broken fixer
        assert review_count == 1, "Should not re-review after fixer infra failure"


class TestTestFailureBlocking:
    """Finding 3: test failures should block iteration acceptance even if reviewer approves."""

    def test_tests_fail_reviewer_lgtm_not_accepted(self, tmp_path):
        """Tests fail on last attempt, reviewer says LGTM — iteration should NOT pass."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path,
            max_iterations=1,
            max_iteration_retries=0,
            backout_on_failure=True,
            run_tests=True,
            test_commands=["false"],
        )

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            # Coder succeeds
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        def mock_test_commands(worktree_path, commands):
            return False, "tests failed output"  # tests fail

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch(
                "ralph_pp.steps.sandbox.run_test_commands_with_output",
                side_effect=mock_test_commands,
            ),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is False, "Iteration should not be accepted when tests fail"


class TestFixInPlaceTestRerun:
    """Finding 1 (round 2): tests must be re-run after fixer in fix-in-place mode."""

    def test_fixer_passes_but_tests_still_fail_not_accepted(self, tmp_path):
        """Fixer succeeds, but tests still fail — iteration should not be accepted."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path,
            max_iterations=1,
            max_iteration_retries=2,
            backout_on_failure=False,
            run_tests=True,
            test_commands=["pytest"],
        )

        test_call_count = 0

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            return ReviewResult(
                passed=False, findings="Issues found", max_severity=None, minor_only=False
            )

        def mock_fixer(findings, worktree_path, config, stories=""):
            return _fake_subprocess_run(returncode=0, stdout="fixed")

        def mock_test_commands(worktree_path, commands):
            nonlocal test_call_count
            test_call_count += 1
            return False, "tests failed output"  # tests always fail

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._run_fixer_in_sandbox", side_effect=mock_fixer),
            patch(
                "ralph_pp.steps.sandbox.run_test_commands_with_output",
                side_effect=mock_test_commands,
            ),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is False
        # Tests should have been called: once before initial review + once per fix cycle
        assert test_call_count >= 2, "Tests should be re-run after fixer"

    def test_fixer_passes_tests_pass_reviewer_accepts(self, tmp_path):
        """Fixer succeeds, tests pass after fix, reviewer LGTM — iteration marked as passed.

        Note: _run_orchestrated returns False (no COMPLETE signal), but the iteration
        itself should be marked as passed in progress.txt.
        """
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path,
            max_iterations=1,
            max_iteration_retries=2,
            backout_on_failure=False,
            run_tests=True,
            test_commands=["pytest"],
        )

        test_call_count = 0
        review_call_count = 0

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            nonlocal review_call_count
            review_call_count += 1
            if review_call_count == 1:
                return ReviewResult(
                    passed=False, findings="Issues found", max_severity=None, minor_only=False
                )
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        def mock_fixer(findings, worktree_path, config, stories=""):
            return _fake_subprocess_run(returncode=0, stdout="fixed")

        def mock_test_commands(worktree_path, commands):
            nonlocal test_call_count
            test_call_count += 1
            if test_call_count == 1:
                return False, "tests failed output"  # initial tests fail
            return True, "all tests passed"  # tests pass after fix

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._run_fixer_in_sandbox", side_effect=mock_fixer),
            patch(
                "ralph_pp.steps.sandbox.run_test_commands_with_output",
                side_effect=mock_test_commands,
            ),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        # No COMPLETE signal was emitted, so the overall workflow returns False
        assert result is False, "No COMPLETE signal — workflow returns False"
        # But the iteration itself should be marked as passed in progress.txt
        progress = (worktree / "scripts" / "ralph" / "progress.txt").read_text()
        assert "Iteration 1 — passed" in progress, (
            "Iteration should be marked passed in progress.txt"
        )
        assert test_call_count == 2, "Tests should be called initially and after fix"
        assert review_call_count == 2, "Reviewer should be called after tests pass"


class TestReviewerInfraFailure:
    """Round 3: _review_iteration should raise on reviewer CLI failure."""

    def test_reviewer_crash_raises_runtime_error(self, tmp_path):
        """Reviewer exits nonzero — workflow should abort with RuntimeError."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=0)

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            # Coder succeeds
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        reviewer_result = ToolResult(output="segfault", exit_code=139, success=False)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox.make_tool") as mock_make_tool,
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            mock_tool = MagicMock()
            mock_tool.run.return_value = reviewer_result
            mock_make_tool.return_value = mock_tool

            with pytest.raises(RuntimeError, match="Iteration reviewer failed"):
                _run_orchestrated(worktree, config)


class TestGitHelperFailures:
    """Round 3: git helpers should raise on failure instead of returning garbage."""

    def test_get_head_sha_raises_on_failure(self, tmp_path):
        from ralph_pp.steps._git import get_head_sha

        with patch("ralph_pp.steps._git.subprocess.run") as mock_run:
            mock_run.return_value = _fake_subprocess_run(
                returncode=128, stderr="fatal: not a git repository"
            )
            with pytest.raises(RuntimeError, match="git rev-parse HEAD failed"):
                get_head_sha(tmp_path)

    def test_get_diff_raises_on_failure(self, tmp_path):
        from ralph_pp.steps._git import get_diff

        with patch("ralph_pp.steps._git.subprocess.run") as mock_run:
            mock_run.return_value = _fake_subprocess_run(
                returncode=128, stderr="fatal: bad revision"
            )
            with pytest.raises(RuntimeError, match="git diff failed"):
                get_diff(tmp_path, "abc1234")


class TestPromptPropagation:
    """Verify that prompt templates are rendered and written correctly."""

    def test_iteration_prompt_contains_expected_placeholders(self, tmp_path):
        """Orchestrated mode writes .iteration-prompt.md with iteration, progress, and findings."""
        worktree = _setup_worktree(tmp_path)
        # Seed progress.txt so the template has progress content
        progress_file = worktree / "scripts" / "ralph" / "progress.txt"
        progress_file.write_text("## Iteration 0 — seed\n")

        template = (
            "Iteration: {iteration}\n"
            "PRD: {prd_file}\n"
            "Progress:\n{progress}\n"
            "Previous findings:\n{review_findings}\n"
        )
        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=0)
        config.orchestrated.prompt_template = template

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            # Coder succeeds
            return _fake_subprocess_run(returncode=0, stdout="output")

        def mock_review(*args, **kwargs):
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        iter_prompt = worktree / "scripts" / "ralph" / ".iteration-prompt.md"
        assert iter_prompt.exists(), ".iteration-prompt.md should be written"
        content = iter_prompt.read_text()
        assert "Iteration: 1" in content
        assert "prd.json" in content
        assert "## Iteration 0 — seed" in content  # progress content
        # First iteration has no prior findings
        assert "Previous findings:\n\n" in content

    def test_fix_prompt_contains_findings(self, tmp_path):
        """Fixer writes .fix-prompt.md containing the review findings."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=1)
        findings_text = "Line 42: missing null check in parser.py"

        with (
            patch(
                "ralph_pp.steps.sandbox._sandbox_wrapper",
                return_value=tmp_path / "ralph-sandbox" / "bin" / "ralph-sandbox",
            ),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
            patch(
                "ralph_pp.steps.sandbox.subprocess.run",
                return_value=_fake_subprocess_run(returncode=0),
            ),
        ):
            _run_fixer_in_sandbox(findings_text, worktree, config)

        fix_prompt = worktree / "scripts" / "ralph" / ".fix-prompt.md"
        assert fix_prompt.exists(), ".fix-prompt.md should be written"
        content = fix_prompt.read_text()
        assert findings_text in content
        assert "Stories under review" in content


class TestRetriesExhaustedAborts:
    """When all retries are exhausted the task must fail, not continue to the next iteration."""

    def test_backout_retries_exhausted_returns_false(self, tmp_path):
        """Backout mode: reviewer rejects every attempt → return False, no iteration 2."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path, max_iterations=3, max_iteration_retries=1, backout_on_failure=True
        )

        coder_call_count = 0

        def mock_subprocess_run(cmd, **kwargs):
            nonlocal coder_call_count
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "reset" in cmd:
                return _fake_subprocess_run(returncode=0)
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            coder_call_count += 1
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            return ReviewResult(
                passed=False, findings="Major flaws found", max_severity="major", minor_only=False
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is False, "Should fail when retries exhausted"
        assert coder_call_count == 2, "Should run initial + 1 retry, then abort (not start iter 2)"

    def test_backout_restores_current_prd_not_initial(self, tmp_path):
        """Backout must restore prd.json to its state at iteration start, not the initial state.

        Regression: saved_prd_json was captured once before the loop, so backout in
        iteration N restored the initial prd.json (all stories passes=false) instead of
        the state after iterations 1..N-1. This caused the review diff to show spurious
        regressions for previously-completed stories.
        """
        worktree = _setup_worktree(tmp_path)
        prd_json = worktree / "scripts" / "ralph" / "prd.json"

        # Two stories: US-001 (already done) and US-002 (in progress)
        import json

        prd_data = {
            "userStories": [
                {"id": "US-001", "title": "Done", "passes": True},
                {"id": "US-002", "title": "Todo", "passes": False},
            ]
        }
        prd_json.write_text(json.dumps(prd_data))

        config = _make_config(
            tmp_path, max_iterations=1, max_iteration_retries=1, backout_on_failure=True
        )

        reset_target = None

        def mock_subprocess_run(cmd, **kwargs):
            nonlocal reset_target
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "reset" in cmd:
                # Capture what we're resetting to, but don't actually reset
                reset_target = cmd[-1] if cmd else None
                return _fake_subprocess_run(returncode=0)
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            return ReviewResult(
                passed=False,
                findings="Major issues",
                max_severity="major",
                minor_only=False,
            )

        with (
            patch(
                "ralph_pp.steps.sandbox.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch(
                "ralph_pp.steps.sandbox._review_iteration",
                side_effect=mock_review,
            ),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch(
                "ralph_pp.steps.sandbox._get_head_sha",
                side_effect=_incrementing_sha(),
            ),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        # After backout + restore, prd.json should still show US-001 as passes=true
        restored = json.loads(prd_json.read_text())
        us001 = next(s for s in restored["userStories"] if s["id"] == "US-001")
        assert us001["passes"] is True, (
            "Backout should restore prd.json with previously-completed stories intact"
        )

    def test_fix_in_place_exhausted_returns_false(self, tmp_path):
        """Fix-in-place mode: fixer can't resolve issues → return False."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path, max_iterations=3, max_iteration_retries=1, backout_on_failure=False
        )

        coder_call_count = 0

        def mock_subprocess_run(cmd, **kwargs):
            nonlocal coder_call_count
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            coder_call_count += 1
            return _fake_subprocess_run(returncode=0, stdout="coder/fixer output")

        def mock_review(*args, **kwargs):
            return ReviewResult(
                passed=False, findings="Major flaws found", max_severity="major", minor_only=False
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is False, "Should fail when fix cycles exhausted"
        # 1 coder + 1 fixer = 2 subprocess calls (not counting rev-parse/diff)
        assert coder_call_count == 2, "Should run coder once + fixer once, then abort"


class TestCommitIfDirty:
    """_commit_if_dirty stages and commits uncommitted work."""

    @staticmethod
    def _init_repo(path):
        """Create a git repo with user config (needed in CI where no global config exists)."""
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=path,
            check=True,
            capture_output=True,
        )

    def test_creates_commit_when_dirty(self, tmp_path):
        """Dirty working tree → commit created, returns True."""
        from ralph_pp.steps.sandbox import _commit_if_dirty

        self._init_repo(tmp_path)
        old_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Create an uncommitted file
        (tmp_path / "new_file.py").write_text("print('hello')")

        result = _commit_if_dirty(tmp_path, "test commit")

        assert result is True
        new_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert old_sha != new_sha, "HEAD should have moved"

    def test_noop_when_clean(self, tmp_path):
        """Clean working tree → no commit, returns False."""
        from ralph_pp.steps.sandbox import _commit_if_dirty

        self._init_repo(tmp_path)

        result = _commit_if_dirty(tmp_path, "should not appear")

        assert result is False


class TestPreviousFindings:
    """Reviewer receives previous findings context in fix cycles."""

    def test_previous_findings_passed_to_reviewer(self, tmp_path):
        """After a fix cycle, the reviewer prompt includes previous findings."""
        from ralph_pp.steps.sandbox import _review_iteration

        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=1)

        captured_prompt = None

        def mock_tool_run(prompt, cwd):
            nonlocal captured_prompt
            captured_prompt = prompt
            return ToolResult(output="LGTM", exit_code=0, success=True)

        with patch("ralph_pp.steps.sandbox.make_tool") as mock_make_tool:
            mock_tool = MagicMock()
            mock_tool.run.side_effect = mock_tool_run
            mock_make_tool.return_value = mock_tool

            _review_iteration(
                iteration=1,
                diff="some diff",
                worktree_path=worktree,
                config=config,
                previous_findings="Line 42: missing null check",
            )

        assert captured_prompt is not None
        assert "previous review cycle found these issues" in captured_prompt
        assert "Line 42: missing null check" in captured_prompt

    def test_no_previous_findings_on_first_review(self, tmp_path):
        """First review has no previous findings context."""
        from ralph_pp.steps.sandbox import _review_iteration

        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=1)

        captured_prompt = None

        def mock_tool_run(prompt, cwd):
            nonlocal captured_prompt
            captured_prompt = prompt
            return ToolResult(output="LGTM", exit_code=0, success=True)

        with patch("ralph_pp.steps.sandbox.make_tool") as mock_make_tool:
            mock_tool = MagicMock()
            mock_tool.run.side_effect = mock_tool_run
            mock_make_tool.return_value = mock_tool

            _review_iteration(
                iteration=1,
                diff="some diff",
                worktree_path=worktree,
                config=config,
            )

        assert captured_prompt is not None
        assert "previous review cycle" not in captured_prompt

    def test_test_results_passed_to_reviewer(self, tmp_path):
        """When test_results is provided, the reviewer prompt includes them."""
        from ralph_pp.steps.sandbox import _review_iteration

        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=1)

        captured_prompt = None

        def mock_tool_run(prompt, cwd):
            nonlocal captured_prompt
            captured_prompt = prompt
            return ToolResult(output="LGTM", exit_code=0, success=True)

        with patch("ralph_pp.steps.sandbox.make_tool") as mock_make_tool:
            mock_tool = MagicMock()
            mock_tool.run.side_effect = mock_tool_run
            mock_make_tool.return_value = mock_tool

            _review_iteration(
                iteration=1,
                diff="some diff",
                worktree_path=worktree,
                config=config,
                test_results="test/CI results were obtained before this review (PASSED)",
            )

        assert captured_prompt is not None
        assert "test/CI results were obtained before this review" in captured_prompt

    def test_no_test_results_when_empty(self, tmp_path):
        """When test_results is empty, no test results block appears in prompt."""
        from ralph_pp.steps.sandbox import _review_iteration

        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=1)

        captured_prompt = None

        def mock_tool_run(prompt, cwd):
            nonlocal captured_prompt
            captured_prompt = prompt
            return ToolResult(output="LGTM", exit_code=0, success=True)

        with patch("ralph_pp.steps.sandbox.make_tool") as mock_make_tool:
            mock_tool = MagicMock()
            mock_tool.run.side_effect = mock_tool_run
            mock_make_tool.return_value = mock_tool

            _review_iteration(
                iteration=1,
                diff="some diff",
                worktree_path=worktree,
                config=config,
            )

        assert captured_prompt is not None
        assert "test/CI results" not in captured_prompt

    def test_fixer_changes_committed(self, tmp_path):
        """In fix-in-place mode, _commit_if_dirty is called after fixer runs."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path, max_iterations=1, max_iteration_retries=1, backout_on_failure=False
        )

        review_count = 0

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="output")

        def mock_review(*args, **kwargs):
            nonlocal review_count
            review_count += 1
            if review_count == 1:
                return ReviewResult(
                    passed=False, findings="Issues found", max_severity=None, minor_only=False
                )
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        def mock_fixer(findings, worktree_path, config, stories=""):
            return _fake_subprocess_run(returncode=0, stdout="fixed")

        commit_calls = []

        def mock_commit(path, message):
            commit_calls.append(message)
            return False

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._run_fixer_in_sandbox", side_effect=mock_fixer),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", side_effect=mock_commit),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        # Should have committed after coder and after fixer
        assert any("coder" in m for m in commit_calls), "Should commit after coder"
        assert any("fixer" in m for m in commit_calls), "Should commit after fixer"

    def test_backout_retry_gets_review_findings(self, tmp_path):
        """In backout mode, the coder prompt on retry includes previous review findings."""
        worktree = _setup_worktree(tmp_path)
        progress_file = worktree / "scripts" / "ralph" / "progress.txt"
        progress_file.write_text("# Progress\n")

        template = (
            "Iteration: {iteration}\n"
            "PRD: {prd_file}\n"
            "Progress:\n{progress}\n"
            "Previous findings:\n{review_findings}\n"
        )
        config = _make_config(
            tmp_path, max_iterations=1, max_iteration_retries=1, backout_on_failure=True
        )
        config.orchestrated.prompt_template = template

        review_count = 0
        findings_text = "MAJOR: query() not removed from InMemoryStore"

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "reset" in cmd:
                return _fake_subprocess_run(returncode=0)
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            nonlocal review_count
            review_count += 1
            if review_count == 1:
                return ReviewResult(
                    passed=False, findings=findings_text, max_severity="major", minor_only=False
                )
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        # The prompt file should contain the review findings from the first attempt
        iter_prompt = worktree / "scripts" / "ralph" / ".iteration-prompt.md"
        assert iter_prompt.exists()
        content = iter_prompt.read_text()
        assert findings_text in content, "Retry prompt should include previous review findings"

    def test_backout_retry_appends_findings_to_claude_md(self, tmp_path):
        """Default flow (no custom template): retry appends findings to CLAUDE.md."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path, max_iterations=1, max_iteration_retries=1, backout_on_failure=True
        )
        # No custom prompt_template — uses default CLAUDE.md flow

        review_count = 0
        findings_text = "MAJOR: update_last_access missing timezone validation"

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "reset" in cmd:
                return _fake_subprocess_run(returncode=0)
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            nonlocal review_count
            review_count += 1
            if review_count == 1:
                return ReviewResult(
                    passed=False,
                    findings=findings_text,
                    max_severity="major",
                    minor_only=False,
                )
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch(
                "ralph_pp.steps.sandbox.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch(
                "ralph_pp.steps.sandbox._review_iteration",
                side_effect=mock_review,
            ),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch(
                "ralph_pp.steps.sandbox._get_head_sha",
                side_effect=_incrementing_sha(),
            ),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        # CLAUDE.md should contain the review findings
        claude_md = worktree / "scripts" / "ralph" / "CLAUDE.md"
        content = claude_md.read_text()
        assert findings_text in content, "Default CLAUDE.md should include review findings on retry"
        assert "RETRY" in content, "Should include retry header"

    def test_first_attempt_claude_md_has_no_findings(self, tmp_path):
        """On the first attempt, CLAUDE.md is the clean prompt without findings."""
        from ralph_pp.steps.sandbox import _ORCHESTRATED_CODER_PROMPT

        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path, max_iterations=1, max_iteration_retries=0, backout_on_failure=True
        )

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        with (
            patch(
                "ralph_pp.steps.sandbox.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch(
                "ralph_pp.steps.sandbox._review_iteration",
                return_value=ReviewResult(
                    passed=True, findings="LGTM", max_severity=None, minor_only=True
                ),
            ),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch(
                "ralph_pp.steps.sandbox._get_head_sha",
                side_effect=_incrementing_sha(),
            ),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        claude_md = worktree / "scripts" / "ralph" / "CLAUDE.md"
        content = claude_md.read_text()
        assert content == _ORCHESTRATED_CODER_PROMPT, (
            "First attempt should use clean prompt without findings"
        )


class TestSeverityGatedBackout:
    """Backout should only trigger when findings meet the severity threshold."""

    def test_minor_only_findings_do_not_trigger_backout(self, tmp_path):
        """When all findings are minor and threshold is 'major', iteration passes."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path, max_iterations=1, max_iteration_retries=1, backout_on_failure=True
        )

        minor_findings = "1. severity: minor\nfile: foo.py\nproblem: missing docstring"

        def mock_review(*args, **kwargs):
            # Return minor-only findings — should pass due to severity gating
            return ReviewResult(
                passed=True, findings=minor_findings, max_severity="minor", minor_only=True
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=_coder_succeeds),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            # Should not abort — minor findings don't trigger backout
            result = _run_orchestrated(worktree, config)

        # Iteration accepted (but didn't get COMPLETE signal, so returns False
        # after reaching max iterations). The key thing is it didn't abort early.
        # With max_iterations=1 it will print "Reached max iterations" and return False
        # but importantly it did NOT return False from "All retries exhausted".
        assert result is False  # max iterations reached, no COMPLETE signal

    def test_major_findings_trigger_backout(self, tmp_path):
        """When findings include major severity, backout and retry happen."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path, max_iterations=1, max_iteration_retries=1, backout_on_failure=True
        )
        review_count = 0

        def mock_review(*args, **kwargs):
            nonlocal review_count
            review_count += 1
            return ReviewResult(
                passed=False,
                findings="severity: major\nproblem: broken",
                max_severity="major",
                minor_only=False,
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=_coder_succeeds),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch("ralph_pp.steps.sandbox._backout_to") as mock_backout,
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is False
        # With max_iteration_retries=1, we get 2 attempts. After first fails,
        # backout should be called once.
        assert mock_backout.call_count == 1

    def test_unparseable_severity_triggers_backout(self, tmp_path):
        """When reviewer output has no severity labels, treat as blocking."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path, max_iterations=1, max_iteration_retries=0, backout_on_failure=True
        )

        def mock_review(*args, **kwargs):
            # No severity labels — max_severity=None, passed=False (conservative)
            return ReviewResult(
                passed=False, findings="Something is wrong", max_severity=None, minor_only=False
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=_coder_succeeds),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is False  # Should abort

    def test_minor_findings_carried_forward(self, tmp_path):
        """Minor findings that pass gating are still available in last_findings."""
        worktree = _setup_worktree(tmp_path)
        minor_text = "1. severity: minor\nproblem: missing test"
        config = _make_config(
            tmp_path, max_iterations=2, max_iteration_retries=0, backout_on_failure=True
        )
        review_count = 0

        def mock_review(*args, **kwargs):
            nonlocal review_count
            review_count += 1
            if review_count == 1:
                return ReviewResult(
                    passed=True, findings=minor_text, max_severity="minor", minor_only=True
                )
            # Second iteration should see the minor findings
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=_coder_succeeds),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        # Both iterations ran (minor findings didn't block iteration 1)
        assert review_count == 2

    def test_custom_threshold_critical_only(self, tmp_path):
        """With threshold='critical', major findings pass gating."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path,
            max_iterations=1,
            max_iteration_retries=0,
            backout_on_failure=True,
            backout_severity_threshold="critical",
        )

        def mock_review(*args, **kwargs):
            # Major findings but threshold is critical — should pass
            return ReviewResult(
                passed=True,
                findings="severity: major\nproblem: not great",
                max_severity="major",
                minor_only=True,  # below threshold
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=_coder_succeeds),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        # Should not abort early — max_iterations reached without COMPLETE
        assert result is False  # but no "retries exhausted" abort


class TestRetryPromptWrapping:
    """_wrap_retry_findings prepends a structured header on retries."""

    def test_first_attempt_noop(self):
        assert _wrap_retry_findings("some findings", 1, 3) == "some findings"

    def test_empty_findings_noop(self):
        assert _wrap_retry_findings("", 2, 3) == ""

    def test_retry_adds_header(self):
        result = _wrap_retry_findings("problem: broken", 2, 3)
        assert "RETRY 2/3" in result
        assert "REJECTED" in result
        assert "problem: broken" in result

    def test_retry_header_preserves_findings(self):
        findings = "1. severity: major\nfile: foo.py\nproblem: bad"
        result = _wrap_retry_findings(findings, 3, 4)
        assert result.endswith(findings)
        assert "RETRY 3/4" in result

    def test_backout_retry_prompt_contains_header(self, tmp_path):
        """Integration: .iteration-prompt.md contains RETRY header on attempt 2."""
        worktree = _setup_worktree(tmp_path)
        findings_text = "1. severity: major\nproblem: broken code"
        config = _make_config(
            tmp_path, max_iterations=1, max_iteration_retries=1, backout_on_failure=True
        )
        config.orchestrated.prompt_template = (
            "Iteration {iteration}\nprd: {prd_file}\n"
            "progress: {progress}\nfindings: {review_findings}"
        )

        review_count = 0

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "reset" in cmd:
                return _fake_subprocess_run(returncode=0)
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            nonlocal review_count
            review_count += 1
            if review_count == 1:
                return ReviewResult(
                    passed=False, findings=findings_text, max_severity="major", minor_only=False
                )
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        iter_prompt = worktree / "scripts" / "ralph" / ".iteration-prompt.md"
        assert iter_prompt.exists()
        content = iter_prompt.read_text()
        assert "RETRY 2/2" in content, "Retry prompt should have RETRY header"
        assert findings_text in content


class TestCompleteSignalValidation:
    """COMPLETE signal must be validated against prd.json story status."""

    def test_complete_accepted_when_all_stories_pass(self, tmp_path):
        """COMPLETE signal with all passes=true → returns True."""
        worktree = _setup_worktree(tmp_path)
        prd_json = worktree / "scripts" / "ralph" / "prd.json"

        import json

        prd_data = {
            "userStories": [
                {"id": "US-001", "title": "Done", "passes": True},
            ]
        }
        prd_json.write_text(json.dumps(prd_data))

        config = _make_config(tmp_path, max_iterations=3, max_iteration_retries=0)

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            # Coder output contains COMPLETE signal
            return _fake_subprocess_run(returncode=0, stdout="done\n<promise>COMPLETE</promise>\n")

        with (
            patch(
                "ralph_pp.steps.sandbox.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch(
                "ralph_pp.steps.sandbox._get_head_sha",
                side_effect=_incrementing_sha(),
            ),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is True

    def test_complete_rejected_when_stories_incomplete(self, tmp_path):
        """COMPLETE signal with passes=false → continues iterations."""
        worktree = _setup_worktree(tmp_path)
        prd_json = worktree / "scripts" / "ralph" / "prd.json"

        import json

        prd_data = {
            "userStories": [
                {"id": "US-001", "title": "Done", "passes": True},
                {"id": "US-002", "title": "Not done", "passes": False},
            ]
        }
        prd_json.write_text(json.dumps(prd_data))

        config = _make_config(tmp_path, max_iterations=2, max_iteration_retries=0)

        coder_call_count = 0

        def mock_subprocess_run(cmd, **kwargs):
            nonlocal coder_call_count
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            coder_call_count += 1
            # Coder always claims COMPLETE
            return _fake_subprocess_run(returncode=0, stdout="done\n<promise>COMPLETE</promise>\n")

        with (
            patch(
                "ralph_pp.steps.sandbox.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch(
                "ralph_pp.steps.sandbox._review_iteration",
                return_value=ReviewResult(
                    passed=True,
                    findings="LGTM",
                    max_severity=None,
                    minor_only=True,
                ),
            ),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch(
                "ralph_pp.steps.sandbox._get_head_sha",
                side_effect=_incrementing_sha(),
            ),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        # Should have run multiple iterations, not stopped at 1
        assert coder_call_count == 2, (
            "Should continue iterating when COMPLETE signal but stories incomplete"
        )


class TestIdleDetection:
    """Orchestrated mode should terminate early when no changes are made."""

    def test_idle_detection_returns_true_after_threshold(self, tmp_path):
        """When coder makes no changes for max_idle_iterations, treat as complete."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=5, max_iteration_retries=0)
        config.orchestrated.max_idle_iterations = 2

        # _get_head_sha always returns the same value → no changes detected
        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="same_sha")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is True, "Should return True (treat as complete) after idle threshold"

    def test_idle_counter_resets_on_changes(self, tmp_path):
        """When the coder makes changes, the idle counter resets."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=4, max_iteration_retries=0)
        config.orchestrated.max_idle_iterations = 2

        sha_sequence = iter(
            [
                "sha1",
                "sha1",  # iter 1: idle (pre==post)
                "sha2",
                "sha3",  # iter 2: changed (pre!=post) — resets counter
                "sha4",
                "sha4",  # iter 3: idle again
                "sha5",
                "sha5",  # iter 4: idle — now hits threshold
            ]
        )

        def mock_sha(path):
            return next(sha_sequence)

        def mock_review(*args, **kwargs):
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=_coder_succeeds),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox._get_head_sha", side_effect=mock_sha),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is True, "Should complete after 2 consecutive idle iterations"


class TestFixerDiffInReview:
    """Reviewer should see the fixer's diff when re-reviewing after a fix cycle."""

    def test_fixer_diff_passed_to_reviewer(self, tmp_path):
        from ralph_pp.steps.sandbox import _review_iteration

        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=1)

        captured_prompt = None

        def mock_tool_run(prompt, cwd):
            nonlocal captured_prompt
            captured_prompt = prompt
            return ToolResult(output="LGTM", exit_code=0, success=True)

        with patch("ralph_pp.steps.sandbox.make_tool") as mock_make_tool:
            mock_tool = MagicMock()
            mock_tool.run.side_effect = mock_tool_run
            mock_make_tool.return_value = mock_tool

            _review_iteration(
                iteration=1,
                diff="full diff",
                worktree_path=worktree,
                config=config,
                previous_findings="Line 42: missing null check",
                fixer_diff="--- a/foo.py\n+++ b/foo.py\n@@ ...",
            )

        assert captured_prompt is not None
        assert "fixer made the following changes" in captured_prompt
        assert "--- a/foo.py" in captured_prompt

    def test_no_fixer_diff_on_first_review(self, tmp_path):
        from ralph_pp.steps.sandbox import _review_iteration

        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=1)

        captured_prompt = None

        def mock_tool_run(prompt, cwd):
            nonlocal captured_prompt
            captured_prompt = prompt
            return ToolResult(output="LGTM", exit_code=0, success=True)

        with patch("ralph_pp.steps.sandbox.make_tool") as mock_make_tool:
            mock_tool = MagicMock()
            mock_tool.run.side_effect = mock_tool_run
            mock_make_tool.return_value = mock_tool

            _review_iteration(
                iteration=1,
                diff="full diff",
                worktree_path=worktree,
                config=config,
            )

        assert captured_prompt is not None
        assert "fixer made" not in captured_prompt


class TestOrchestratedCoderPrompt:
    """The orchestrated coder prompt should instruct Claude to update prd.json."""

    def test_fallback_prompt_mentions_prd_update(self, tmp_path):
        from ralph_pp.steps.sandbox import _ORCHESTRATED_CODER_PROMPT

        assert "passes" in _ORCHESTRATED_CODER_PROMPT
        assert "progress.txt" in _ORCHESTRATED_CODER_PROMPT
        assert "COMPLETE" in _ORCHESTRATED_CODER_PROMPT

    def test_setup_worktree_writes_orchestrated_prompt(self, tmp_path):
        from ralph_pp.steps.sandbox import _setup_worktree_files

        worktree = tmp_path / "project"
        worktree.mkdir()

        _setup_worktree_files(worktree)

        claude_md = worktree / "scripts" / "ralph" / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text()
        assert "passes" in content
        assert "progress.txt" in content


class TestTestCommandsGuidance:
    """Reviewer prompts include test command guidance when configured."""

    def test_review_prompt_includes_test_commands_when_configured(self, tmp_path):
        from ralph_pp.steps.sandbox import _review_iteration

        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, test_commands=["hatch run ci"])

        captured_prompt = None

        def mock_tool_run(prompt, cwd):
            nonlocal captured_prompt
            captured_prompt = prompt
            return ToolResult(output="LGTM", exit_code=0, success=True)

        with patch("ralph_pp.steps.sandbox.make_tool") as mock_make_tool:
            mock_tool = MagicMock()
            mock_tool.run.side_effect = mock_tool_run
            mock_make_tool.return_value = mock_tool

            _review_iteration(iteration=1, diff="diff", worktree_path=worktree, config=config)

        assert captured_prompt is not None
        assert "hatch run ci" in captured_prompt
        assert "Do NOT run bare pytest" in captured_prompt

    def test_review_prompt_no_guidance_when_no_test_commands(self, tmp_path):
        from ralph_pp.steps.sandbox import _review_iteration

        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, test_commands=[])

        captured_prompt = None

        def mock_tool_run(prompt, cwd):
            nonlocal captured_prompt
            captured_prompt = prompt
            return ToolResult(output="LGTM", exit_code=0, success=True)

        with patch("ralph_pp.steps.sandbox.make_tool") as mock_make_tool:
            mock_tool = MagicMock()
            mock_tool.run.side_effect = mock_tool_run
            mock_make_tool.return_value = mock_tool

            _review_iteration(iteration=1, diff="diff", worktree_path=worktree, config=config)

        assert captured_prompt is not None
        assert "Do NOT run bare pytest" not in captured_prompt


class TestStoryScopedReview:
    """Reviewer should only see stories relevant to the current iteration."""

    def test_review_prompt_contains_story_text(self, tmp_path):
        from ralph_pp.steps.sandbox import _review_iteration

        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=1)

        captured_prompt = None

        def mock_tool_run(prompt, cwd):
            nonlocal captured_prompt
            captured_prompt = prompt
            return ToolResult(output="LGTM", exit_code=0, success=True)

        with patch("ralph_pp.steps.sandbox.make_tool") as mock_make_tool:
            mock_tool = MagicMock()
            mock_tool.run.side_effect = mock_tool_run
            mock_make_tool.return_value = mock_tool

            _review_iteration(
                iteration=1,
                diff="diff",
                worktree_path=worktree,
                config=config,
                stories_under_review="### US-001: Add feature\nAs a user...",
            )

        assert captured_prompt is not None
        assert "US-001: Add feature" in captured_prompt
        assert "Do NOT evaluate against stories that are not listed" in captured_prompt

    def test_prd_helpers_parse_stories(self, tmp_path):
        from ralph_pp.steps.sandbox import format_stories, read_story_status

        prd_json = tmp_path / "prd.json"
        prd_json.write_text(
            json.dumps(
                {
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Add field",
                            "description": "As a dev...",
                            "acceptanceCriteria": ["Field exists", "Tests pass"],
                            "passes": True,
                        },
                        {
                            "id": "US-002",
                            "title": "Remove query",
                            "description": "As a dev...",
                            "acceptanceCriteria": ["Method removed"],
                            "passes": False,
                        },
                    ]
                }
            )
        )

        status = read_story_status(prd_json)
        assert status == {"US-001": True, "US-002": False}

        text = format_stories(prd_json, {"US-001"})
        assert "US-001: Add field" in text
        assert "Field exists" in text
        assert "US-002" not in text

    def test_prd_parse_error_on_bad_json(self, tmp_path):
        from ralph_pp.steps.sandbox import PrdParseError, read_story_status

        prd_json = tmp_path / "prd.json"
        prd_json.write_text("not json")

        with pytest.raises(PrdParseError, match="Failed to parse"):
            read_story_status(prd_json)

    def test_prd_parse_error_on_missing_key(self, tmp_path):
        from ralph_pp.steps.sandbox import PrdParseError, read_story_status

        prd_json = tmp_path / "prd.json"
        prd_json.write_text('{"project": "test"}')

        with pytest.raises(PrdParseError, match="missing 'userStories' key"):
            read_story_status(prd_json)
