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
    on_retry_exhaustion: str = "abort",
) -> Config:
    """Build a Config with fake sandbox dir and orchestrated settings.

    Note: the production default for ``on_retry_exhaustion`` is
    ``"skip-story"`` (see #127). Tests default to ``"abort"`` for
    backwards compatibility with pre-#127 assertions; tests that exercise
    the skip path pass ``on_retry_exhaustion="skip-story"`` explicitly.
    """
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
            on_retry_exhaustion=on_retry_exhaustion,  # type: ignore[arg-type]
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
            patch("ralph_pp.steps.sandbox.CliTool") as mock_make_tool,
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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


class TestCountersWrittenOnException:
    """Counters dict must be updated even when _run_orchestrated raises."""

    def test_counters_updated_when_reviewer_raises(self, tmp_path):
        """If reviewer crashes at iteration 1, counters should show iterations=1."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=3, max_iteration_retries=0)

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        reviewer_result = ToolResult(output="segfault", exit_code=139, success=False)
        counters: dict[str, int] = {"iterations": 0, "retries": 0}

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox.CliTool") as mock_make_tool,
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            mock_tool = MagicMock()
            mock_tool.run.return_value = reviewer_result
            mock_make_tool.return_value = mock_tool

            with pytest.raises(RuntimeError):
                _run_orchestrated(worktree, config, counters)

        assert counters["iterations"] == 1, "Should record that iteration 1 was reached"
        assert counters["retries"] == 0


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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch(
                "ralph_pp.steps.sandbox.get_head_sha",
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
        from ralph_pp.steps.sandbox import commit_if_dirty

        self._init_repo(tmp_path)
        old_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Create an uncommitted file
        (tmp_path / "new_file.py").write_text("print('hello')")

        result = commit_if_dirty(tmp_path, "test commit")

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
        from ralph_pp.steps.sandbox import commit_if_dirty

        self._init_repo(tmp_path)

        result = commit_if_dirty(tmp_path, "should not appear")

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

        with patch("ralph_pp.steps.sandbox.CliTool") as mock_make_tool:
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

        with patch("ralph_pp.steps.sandbox.CliTool") as mock_make_tool:
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

        with patch("ralph_pp.steps.sandbox.CliTool") as mock_make_tool:
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

        with patch("ralph_pp.steps.sandbox.CliTool") as mock_make_tool:
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", side_effect=mock_commit),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch(
                "ralph_pp.steps.sandbox.get_head_sha",
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch(
                "ralph_pp.steps.sandbox.get_head_sha",
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
        expected = _ORCHESTRATED_CODER_PROMPT.replace("{story_filter_instruction}", "")
        assert content == expected, "First attempt should use clean prompt without findings"


class TestFindingsNotLeakedAcrossIterations:
    """Findings from iteration N must not contaminate the review of iteration N+1."""

    def test_iteration2_reviewer_gets_no_previous_findings(self, tmp_path):
        """After iteration 1 has findings, iteration 2's first review should
        have no previous_findings context."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path, max_iterations=2, max_iteration_retries=0, backout_on_failure=False
        )

        review_calls: list[dict] = []

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            review_calls.append(kwargs)
            # Accept every iteration so we progress to iteration 2
            return ReviewResult(
                passed=True, findings="Minor: style nits", max_severity="minor", minor_only=True
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        assert len(review_calls) == 2, "Should have reviewed both iterations"
        # Iteration 2's review should have empty previous_findings
        iter2_findings = review_calls[1].get("previous_findings", "")
        assert iter2_findings == "", (
            f"Iteration 2 should have no previous_findings, got: {iter2_findings!r}"
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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

    def test_repeat_count_escalates_header(self):
        """When repeat_count >= 1, retry header must escalate the language (#126)."""
        result = _wrap_retry_findings("problem: broken", 3, 4, repeat_count=1)
        assert "REPEATED FAILURE" in result
        assert "FUNDAMENTALLY different" in result
        assert "problem: broken" in result

    def test_escalation_includes_repeat_count(self):
        result = _wrap_retry_findings("problem: broken", 4, 6, repeat_count=2)
        assert "REPEATED FAILURE (3x)" in result  # repeat_count+1 displayed

    def test_repeat_count_zero_uses_normal_header(self):
        """repeat_count=0 keeps the original polite retry header."""
        result = _wrap_retry_findings("problem", 2, 3, repeat_count=0)
        assert "RETRY 2/3" in result
        assert "REPEATED FAILURE" not in result

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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch(
                "ralph_pp.steps.sandbox.get_head_sha",
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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch(
                "ralph_pp.steps.sandbox.get_head_sha",
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

    def test_complete_with_story_filter_checks_only_filtered_stories(self, tmp_path):
        """COMPLETE signal with story filter should only check filtered story status (#86)."""
        worktree = _setup_worktree(tmp_path)
        prd_json = worktree / "scripts" / "ralph" / "prd.json"

        import json

        # US-001 is done, US-002 is not — but we only filter on US-001
        prd_data = {
            "userStories": [
                {"id": "US-001", "title": "Done", "passes": True},
                {"id": "US-002", "title": "Not targeted", "passes": False},
            ]
        }
        prd_json.write_text(json.dumps(prd_data))

        config = _make_config(tmp_path, max_iterations=3, max_iteration_retries=0)
        config.orchestrated.story_filter = ["US-001"]

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="done\n<promise>COMPLETE</promise>\n")

        with (
            patch(
                "ralph_pp.steps.sandbox.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch(
                "ralph_pp.steps.sandbox.get_head_sha",
                side_effect=_incrementing_sha(),
            ),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        # Should succeed — US-001 passes and US-002 is outside the filter
        assert result is True

    def test_unknown_story_filter_ids_raise_error(self, tmp_path):
        """Unknown story IDs in --story filter should raise ValueError (#85)."""
        worktree = _setup_worktree(tmp_path)
        prd_json = worktree / "scripts" / "ralph" / "prd.json"

        import json

        prd_data = {"userStories": [{"id": "US-001", "title": "Story", "passes": False}]}
        prd_json.write_text(json.dumps(prd_data))

        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=0)
        config.orchestrated.story_filter = ["US-999"]

        with (
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
            pytest.raises(ValueError, match="Unknown story IDs"),
        ):
            _run_orchestrated(worktree, config)


class TestIdleDetection:
    """Orchestrated mode should terminate early when no changes are made."""

    def test_idle_detection_returns_true_after_threshold(self, tmp_path):
        """Idle detection with completed stories returns True."""
        worktree = _setup_worktree(tmp_path)
        prd_json = worktree / "scripts" / "ralph" / "prd.json"

        import json

        prd_data = {"userStories": [{"id": "US-001", "title": "Done", "passes": True}]}
        prd_json.write_text(json.dumps(prd_data))

        config = _make_config(tmp_path, max_iterations=5, max_iteration_retries=0)
        config.orchestrated.max_idle_iterations = 2

        # _get_head_sha always returns the same value → no changes detected
        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="same_sha")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is True, "Should return True (treat as complete) after idle threshold"

    def test_idle_detection_returns_false_when_no_stories_complete(self, tmp_path):
        """When coder makes no changes but no stories passed, return False (#87)."""
        worktree = _setup_worktree(tmp_path)
        prd_json = worktree / "scripts" / "ralph" / "prd.json"

        import json

        prd_data = {"userStories": [{"id": "US-001", "title": "Not done", "passes": False}]}
        prd_json.write_text(json.dumps(prd_data))

        config = _make_config(tmp_path, max_iterations=5, max_iteration_retries=0)
        config.orchestrated.max_idle_iterations = 2

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="same_sha")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is False, "Should return False when idle but no stories complete"

    def test_idle_counter_resets_on_changes(self, tmp_path):
        """When the coder makes changes, the idle counter resets."""
        worktree = _setup_worktree(tmp_path)
        prd_json = worktree / "scripts" / "ralph" / "prd.json"

        import json

        prd_data = {"userStories": [{"id": "US-001", "title": "Done", "passes": True}]}
        prd_json.write_text(json.dumps(prd_data))

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
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=mock_sha),
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

        with patch("ralph_pp.steps.sandbox.CliTool") as mock_make_tool:
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

        with patch("ralph_pp.steps.sandbox.CliTool") as mock_make_tool:
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

        with patch("ralph_pp.steps.sandbox.CliTool") as mock_make_tool:
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

        with patch("ralph_pp.steps.sandbox.CliTool") as mock_make_tool:
            mock_tool = MagicMock()
            mock_tool.run.side_effect = mock_tool_run
            mock_make_tool.return_value = mock_tool

            _review_iteration(iteration=1, diff="diff", worktree_path=worktree, config=config)

        assert captured_prompt is not None
        assert "Do NOT run bare pytest" not in captured_prompt


class TestReviewerPermissions:
    """Orchestrated reviewer should augment Bash permissions (#89)."""

    def test_reviewer_gets_bash_permissions_for_test_commands(self, tmp_path):
        from ralph_pp.steps.sandbox import _review_iteration

        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, test_commands=["hatch run ci"])
        config.orchestrated.auto_allow_test_commands = True
        # Use claude (which has allowed_tools) as reviewer so permissions can be augmented
        config.orchestrated.reviewer = "claude"

        captured_config = None

        def mock_cli_tool_init(self_tool, name, config):
            nonlocal captured_config
            captured_config = config

        mock_tool = MagicMock()
        mock_tool.run.return_value = ToolResult(output="LGTM", exit_code=0, success=True)

        with patch("ralph_pp.steps.sandbox.CliTool") as mock_cls:
            mock_cls.return_value = mock_tool
            mock_cls.side_effect = None

            # Capture the config passed to CliTool
            def capture_init(name, config):
                nonlocal captured_config
                captured_config = config
                return mock_tool

            mock_cls.side_effect = capture_init

            _review_iteration(iteration=1, diff="diff", worktree_path=worktree, config=config)

        assert captured_config is not None
        assert captured_config.allowed_tools is not None
        # Should include a Bash(...) permission for hatch
        bash_perms = [t for t in captured_config.allowed_tools if t.startswith("Bash(")]
        assert len(bash_perms) > 0, "Reviewer should have Bash permissions for test commands"

    def test_reviewer_no_extra_permissions_when_auto_allow_disabled(self, tmp_path):
        from ralph_pp.steps.sandbox import _review_iteration

        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, test_commands=["hatch run ci"])
        config.orchestrated.auto_allow_test_commands = False

        captured_config = None

        mock_tool = MagicMock()
        mock_tool.run.return_value = ToolResult(output="LGTM", exit_code=0, success=True)

        with patch("ralph_pp.steps.sandbox.CliTool") as mock_cls:

            def capture_init(name, config):
                nonlocal captured_config
                captured_config = config
                return mock_tool

            mock_cls.side_effect = capture_init

            _review_iteration(iteration=1, diff="diff", worktree_path=worktree, config=config)

        assert captured_config is not None
        # Should NOT have extra Bash permissions
        original_tools = config.get_tool(config.orchestrated.reviewer).allowed_tools
        assert captured_config.allowed_tools == original_tools


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

        with patch("ralph_pp.steps.sandbox.CliTool") as mock_make_tool:
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

    def test_no_story_completed_uses_fallback_scope(self, tmp_path):
        """When coder makes changes but marks no story complete, the reviewer
        should get a fallback note instead of all incomplete stories."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path, max_iterations=1, max_iteration_retries=0, backout_on_failure=False
        )

        captured_stories_arg = None

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            # Coder runs but does NOT update prd.json — no story marked complete
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            nonlocal captured_stories_arg
            captured_stories_arg = kwargs.get("stories_under_review", "")
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        assert captured_stories_arg is not None
        assert "did not mark any story" in captured_stories_arg


# ── Issue #114: circuit-breaker for consecutive infra failures ─────────


class TestConsecutiveInfraFailureCircuitBreaker:
    """Issue #114: abort the run after N consecutive coder infra failures."""

    def test_aborts_after_threshold(self, tmp_path):
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path,
            max_iterations=10,
            max_iteration_retries=0,
            backout_on_failure=True,
        )
        config.orchestrated.max_consecutive_infra_failures = 3
        coder_calls = {"n": 0}

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            coder_calls["n"] += 1
            return _fake_subprocess_run(
                returncode=1,
                stderr='API Error: 401 {"error":"OAuth token expired"}',
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch(
                "ralph_pp.steps.sandbox._review_iteration",
                side_effect=AssertionError("review should not run after infra failure"),
            ),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is False
        # Circuit breaker trips on the 3rd failure — we stop there.
        assert coder_calls["n"] == 3, f"expected circuit-breaker at 3 calls, got {coder_calls['n']}"

    def test_counter_resets_on_successful_coder(self, tmp_path):
        """Intermittent failures should not accumulate across a successful iteration."""
        worktree = _setup_worktree(tmp_path)
        (worktree / "scripts" / "ralph" / "prd.json").write_text(
            json.dumps(
                {
                    "userStories": [
                        {"id": "US-001", "title": "A", "passes": False},
                        {"id": "US-002", "title": "B", "passes": False},
                        {"id": "US-003", "title": "C", "passes": False},
                        {"id": "US-004", "title": "D", "passes": False},
                    ]
                }
            )
        )
        config = _make_config(
            tmp_path,
            max_iterations=10,
            max_iteration_retries=0,
            backout_on_failure=True,
        )
        config.orchestrated.max_consecutive_infra_failures = 3
        # Pattern: fail, fail, success, fail, fail — should NOT trip because of reset
        results_iter = iter([1, 1, 0, 1, 1, 0])
        coder_calls = {"n": 0}

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            coder_calls["n"] += 1
            rc = next(results_iter, 0)
            return _fake_subprocess_run(returncode=rc, stderr="fail" if rc else "")

        def mock_review(*args, **kwargs):
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        # Must have gotten past the second pair of failures (>= 5 calls), proving reset worked.
        assert coder_calls["n"] >= 5, (
            f"expected reset-after-success behavior, got {coder_calls['n']} calls"
        )

    def test_disabled_when_zero(self, tmp_path):
        """max_consecutive_infra_failures=0 disables the breaker."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path,
            max_iterations=5,
            max_iteration_retries=0,
            backout_on_failure=True,
        )
        config.orchestrated.max_consecutive_infra_failures = 0
        coder_calls = {"n": 0}

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            coder_calls["n"] += 1
            return _fake_subprocess_run(returncode=1, stderr="fail")

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch(
                "ralph_pp.steps.sandbox._review_iteration",
                side_effect=AssertionError("review should not run"),
            ),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        # Without the breaker, all 5 iterations run.
        assert coder_calls["n"] == 5


# ── Issue #129: reviewer owns the passes field ─────────────────────────


class TestEnforcePassesBaseline:
    """Unit tests for the passes-baseline helper."""

    def _write_prd(self, worktree, stories):
        prd = worktree / "scripts" / "ralph" / "prd.json"
        prd.write_text(
            json.dumps({"userStories": [{"id": sid, "passes": p} for sid, p in stories]})
        )
        return prd

    def test_reverts_unauthorized_flip(self, tmp_path):
        from ralph_pp.steps.sandbox import enforce_passes_baseline

        worktree = _setup_worktree(tmp_path)
        prd = self._write_prd(
            worktree,
            [("US-001", False), ("US-002", False), ("US-003", False)],
        )
        # Coder flipped US-002 without reviewer approval.
        data = json.loads(prd.read_text())
        data["userStories"][1]["passes"] = True
        prd.write_text(json.dumps(data))

        reverted = enforce_passes_baseline(prd, {"US-001": False, "US-002": False, "US-003": False})
        assert reverted == {"US-002"}
        state = json.loads(prd.read_text())
        assert all(not s["passes"] for s in state["userStories"])

    def test_preserves_approved_stories(self, tmp_path):
        from ralph_pp.steps.sandbox import enforce_passes_baseline

        worktree = _setup_worktree(tmp_path)
        prd = self._write_prd(worktree, [("US-001", False), ("US-002", False)])
        data = json.loads(prd.read_text())
        data["userStories"][0]["passes"] = True
        data["userStories"][1]["passes"] = True
        prd.write_text(json.dumps(data))

        reverted = enforce_passes_baseline(
            prd,
            {"US-001": False, "US-002": False},
            approved={"US-001"},
        )
        assert reverted == {"US-002"}
        state = json.loads(prd.read_text())
        assert state["userStories"][0]["passes"] is True
        assert state["userStories"][1]["passes"] is False

    def test_no_change_when_coder_respected_baseline(self, tmp_path):
        from ralph_pp.steps.sandbox import enforce_passes_baseline

        worktree = _setup_worktree(tmp_path)
        prd = self._write_prd(worktree, [("US-001", True), ("US-002", False)])
        before = prd.read_text()
        reverted = enforce_passes_baseline(prd, {"US-001": True, "US-002": False})
        assert reverted == set()
        assert prd.read_text() == before

    def test_preserves_existing_baseline_true(self, tmp_path):
        """Previously-passed stories must stay passed even if approved is empty."""
        from ralph_pp.steps.sandbox import enforce_passes_baseline

        worktree = _setup_worktree(tmp_path)
        prd = self._write_prd(worktree, [("US-001", True), ("US-002", False)])
        # Coder flips US-002
        data = json.loads(prd.read_text())
        data["userStories"][1]["passes"] = True
        prd.write_text(json.dumps(data))

        reverted = enforce_passes_baseline(prd, {"US-001": True, "US-002": False})
        assert reverted == {"US-002"}
        state = json.loads(prd.read_text())
        assert state["userStories"][0]["passes"] is True
        assert state["userStories"][1]["passes"] is False


class TestPassesBaselineEnforcementInOrchestrator:
    """Integration: orchestrator must not accept coder's unauthorized passes flips."""

    def test_rejection_rolls_back_unauthorized_flip(self, tmp_path):
        """Coder flips US-002 to true, reviewer rejects → US-002 restored to false."""
        worktree = _setup_worktree(tmp_path)
        prd = worktree / "scripts" / "ralph" / "prd.json"
        prd.write_text(
            json.dumps(
                {
                    "userStories": [
                        {"id": "US-001", "title": "A", "passes": False},
                        {"id": "US-002", "title": "B", "passes": False},
                    ]
                }
            )
        )
        config = _make_config(
            tmp_path,
            max_iterations=1,
            max_iteration_retries=0,
            backout_on_failure=False,  # fix-in-place so we exercise the non-backout path
        )

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            # Coder flips US-002 to true without authority
            data = json.loads(prd.read_text())
            for s in data["userStories"]:
                if s["id"] == "US-002":
                    s["passes"] = True
            prd.write_text(json.dumps(data))
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            return ReviewResult(
                passed=False,
                findings="1. severity: critical\nproblem: incomplete",
                max_severity="critical",
                minor_only=False,
            )

        def mock_fixer(*args, **kwargs):
            return _fake_subprocess_run(returncode=0, stdout="fixed")

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._run_fixer_in_sandbox", side_effect=mock_fixer),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        # After the (failed) iteration, US-002 must be back to false.
        state = json.loads(prd.read_text())
        us002 = next(s for s in state["userStories"] if s["id"] == "US-002")
        assert us002["passes"] is False, (
            "Coder's unauthorized flip of US-002 must be rolled back after reviewer rejection"
        )

    def test_acceptance_keeps_only_newly_completed(self, tmp_path):
        """After LGTM, only stories in ``newly_completed`` (diffed from baseline) remain passed.

        The reviewer evaluates the diff scope, which is ``newly_completed`` — the
        set of stories the coder flipped this iteration. Stories not touched by
        the coder stay at their previous (baseline) value. This is the
        structural defense for issue #129: future-state prd.json can never drift
        from the reviewer-approved baseline.
        """
        worktree = _setup_worktree(tmp_path)
        prd = worktree / "scripts" / "ralph" / "prd.json"
        prd.write_text(
            json.dumps(
                {
                    "userStories": [
                        {"id": "US-001", "title": "A", "passes": False},
                        {"id": "US-002", "title": "B", "passes": False},
                    ]
                }
            )
        )
        config = _make_config(
            tmp_path,
            max_iterations=1,
            max_iteration_retries=0,
            backout_on_failure=True,
        )

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            # Coder flips US-001 legitimately
            data = json.loads(prd.read_text())
            for s in data["userStories"]:
                if s["id"] == "US-001":
                    s["passes"] = True
            prd.write_text(json.dumps(data))
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        state = json.loads(prd.read_text())
        by_id = {s["id"]: s["passes"] for s in state["userStories"]}
        assert by_id["US-001"] is True
        assert by_id["US-002"] is False


# ── Issue #127: skip-and-advance on retry exhaustion ───────────────────


class TestNextTargetStory:
    """Unit tests for the ``next_target_story`` helper."""

    def _make_prd(self, tmp_path, stories):
        prd = tmp_path / "prd.json"
        prd.write_text(json.dumps({"userStories": stories}))
        return prd

    def test_returns_highest_priority_unfinished(self, tmp_path):
        from ralph_pp.steps.sandbox import next_target_story

        prd = self._make_prd(
            tmp_path,
            [
                {"id": "US-001", "passes": True, "priority": 1},
                {"id": "US-002", "passes": False, "priority": 3},
                {"id": "US-003", "passes": False, "priority": 2},
            ],
        )
        assert next_target_story(prd) == "US-003"

    def test_excludes_skipped_ids(self, tmp_path):
        from ralph_pp.steps.sandbox import next_target_story

        prd = self._make_prd(
            tmp_path,
            [
                {"id": "US-001", "passes": False, "priority": 1},
                {"id": "US-002", "passes": False, "priority": 2},
            ],
        )
        assert next_target_story(prd, excluded_ids={"US-001"}) == "US-002"

    def test_returns_none_when_all_done(self, tmp_path):
        from ralph_pp.steps.sandbox import next_target_story

        prd = self._make_prd(
            tmp_path,
            [
                {"id": "US-001", "passes": True, "priority": 1},
                {"id": "US-002", "passes": True, "priority": 2},
            ],
        )
        assert next_target_story(prd) is None

    def test_returns_none_when_all_skipped(self, tmp_path):
        from ralph_pp.steps.sandbox import next_target_story

        prd = self._make_prd(
            tmp_path,
            [
                {"id": "US-001", "passes": False, "priority": 1},
                {"id": "US-002", "passes": False, "priority": 2},
            ],
        )
        assert next_target_story(prd, excluded_ids={"US-001", "US-002"}) is None

    def test_honors_story_filter(self, tmp_path):
        from ralph_pp.steps.sandbox import next_target_story

        prd = self._make_prd(
            tmp_path,
            [
                {"id": "US-001", "passes": False, "priority": 1},
                {"id": "US-002", "passes": False, "priority": 2},
                {"id": "US-003", "passes": False, "priority": 3},
            ],
        )
        assert next_target_story(prd, story_filter={"US-002", "US-003"}) == "US-002"

    def test_missing_priority_sorts_last(self, tmp_path):
        from ralph_pp.steps.sandbox import next_target_story

        prd = self._make_prd(
            tmp_path,
            [
                {"id": "US-001", "passes": False},
                {"id": "US-002", "passes": False, "priority": 5},
            ],
        )
        assert next_target_story(prd) == "US-002"


class TestSkipAndAdvance:
    """Integration tests for the skip-story retry-exhaustion policy (#127)."""

    def _make_prd(self, worktree, stories):
        prd = worktree / "scripts" / "ralph" / "prd.json"
        prd.write_text(json.dumps({"userStories": stories}))
        return prd

    def _parse_skipped_from_claude_md(self, worktree):
        """Return the set of story IDs CLAUDE.md currently tells the coder to skip."""
        import re

        claude_md = worktree / "scripts" / "ralph" / "CLAUDE.md"
        if not claude_md.exists():
            return set()
        text = claude_md.read_text()
        m = re.search(
            r"Do NOT work on these story IDs \(they have been skipped "
            r"after exhausting retries\): ([A-Z0-9,\- ]+)\.",
            text,
        )
        if not m:
            return set()
        return {s.strip() for s in m.group(1).split(",") if s.strip()}

    def test_skip_advances_to_next_story(self, tmp_path):
        """When US-001 exhausts retries, orchestrator should start US-002 on next iteration."""
        worktree = _setup_worktree(tmp_path)
        prd = self._make_prd(
            worktree,
            [
                {"id": "US-001", "title": "A", "priority": 1, "passes": False},
                {"id": "US-002", "title": "B", "priority": 2, "passes": False},
            ],
        )
        config = _make_config(
            tmp_path,
            max_iterations=4,
            max_iteration_retries=1,
            backout_on_failure=True,
            on_retry_exhaustion="skip-story",
        )
        iteration_seen: list[int] = []
        review_calls = {"n": 0}

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "reset" in cmd:
                return _fake_subprocess_run(returncode=0)
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            # Coder honors the skip list in CLAUDE.md when choosing a story
            skipped = self._parse_skipped_from_claude_md(worktree)
            data = json.loads(prd.read_text())
            for s in data["userStories"]:
                if not s["passes"] and s["id"] not in skipped:
                    s["passes"] = True
                    break
            prd.write_text(json.dumps(data))
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(iteration, *args, **kwargs):
            iteration_seen.append(iteration)
            review_calls["n"] += 1
            # US-001 gets rejected both times → exhaust → skip
            # US-002 gets accepted the first time → pass
            data = json.loads(prd.read_text())
            us001 = next(s for s in data["userStories"] if s["id"] == "US-001")
            if us001["passes"]:
                return ReviewResult(
                    passed=False,
                    findings="major flaws",
                    max_severity="major",
                    minor_only=False,
                )
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        # Should have completed (partial success) and visited iteration 2 for US-002
        assert result is True
        assert 2 in iteration_seen, (
            f"Expected iteration 2 to run after US-001 skip; saw {iteration_seen}"
        )

        # progress.txt should record US-001 as skipped
        progress = (worktree / "scripts" / "ralph" / "progress.txt").read_text()
        assert "US-001 SKIPPED" in progress

    def test_skip_injects_exclusion_into_claude_md(self, tmp_path):
        """Second iteration's CLAUDE.md should tell the coder to skip US-001."""
        worktree = _setup_worktree(tmp_path)
        self._make_prd(
            worktree,
            [
                {"id": "US-001", "title": "A", "priority": 1, "passes": False},
                {"id": "US-002", "title": "B", "priority": 2, "passes": False},
            ],
        )
        config = _make_config(
            tmp_path,
            max_iterations=3,
            max_iteration_retries=1,
            backout_on_failure=True,
            on_retry_exhaustion="skip-story",
        )
        claude_md_snapshots: list[str] = []

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "reset" in cmd:
                return _fake_subprocess_run(returncode=0)
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            # Snapshot CLAUDE.md at the moment of each coder invocation
            claude_md = worktree / "scripts" / "ralph" / "CLAUDE.md"
            claude_md_snapshots.append(claude_md.read_text() if claude_md.exists() else "")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            return ReviewResult(
                passed=False, findings="fail", max_severity="critical", minor_only=False
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        # After the first iteration skipped US-001, subsequent iterations
        # must tell the coder not to touch it.
        # Iteration 1 took 2 attempts (initial + 1 retry), so index 2 is iteration 2's prompt.
        assert len(claude_md_snapshots) >= 3
        later_snapshot = claude_md_snapshots[2]
        assert "US-001" in later_snapshot
        assert "skipped after exhausting retries" in later_snapshot

    def test_abort_policy_still_aborts(self, tmp_path):
        """When on_retry_exhaustion='abort', legacy behavior: return False, no advance."""
        worktree = _setup_worktree(tmp_path)
        self._make_prd(
            worktree,
            [
                {"id": "US-001", "title": "A", "priority": 1, "passes": False},
                {"id": "US-002", "title": "B", "priority": 2, "passes": False},
            ],
        )
        config = _make_config(
            tmp_path,
            max_iterations=3,
            max_iteration_retries=1,
            backout_on_failure=True,
            on_retry_exhaustion="abort",
        )
        iteration_seen: list[int] = []

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "reset" in cmd:
                return _fake_subprocess_run(returncode=0)
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(iteration, *args, **kwargs):
            iteration_seen.append(iteration)
            return ReviewResult(
                passed=False, findings="fail", max_severity="critical", minor_only=False
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is False
        # Only iteration 1 should run — no advance after abort
        assert iteration_seen == [1, 1]  # 2 reviews in iteration 1 (initial + 1 retry)

    def test_fix_in_place_mode_skip_advances(self, tmp_path):
        """Skip-story also works in fix-in-place mode.

        Note: this test asserts the interaction between #127 (skip-and-advance)
        and #129 (passes baseline enforcement). The coder mock honors the skip
        list, and the reviewer rejects iteration 1 outright (driving exhaustion
        and skip), then accepts iteration 2.
        """
        worktree = _setup_worktree(tmp_path)
        prd = self._make_prd(
            worktree,
            [
                {"id": "US-001", "title": "A", "priority": 1, "passes": False},
                {"id": "US-002", "title": "B", "priority": 2, "passes": False},
            ],
        )
        config = _make_config(
            tmp_path,
            max_iterations=4,
            max_iteration_retries=1,
            backout_on_failure=False,
            on_retry_exhaustion="skip-story",
        )
        iteration_seen: list[int] = []

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            env = kwargs.get("env") or {}
            is_fixer = "RALPH_PROMPT_FILE" in env and ".fix-prompt.md" in env.get(
                "RALPH_PROMPT_FILE", ""
            )
            if is_fixer:
                return _fake_subprocess_run(returncode=0, stdout="fixer output")
            skipped = self._parse_skipped_from_claude_md(worktree)
            data = json.loads(prd.read_text())
            for s in data["userStories"]:
                if not s["passes"] and s["id"] not in skipped:
                    s["passes"] = True
                    break
            prd.write_text(json.dumps(data))
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(iteration, *args, **kwargs):
            iteration_seen.append(iteration)
            # Iteration 1: reject everything to drive exhaustion → skip US-001.
            # Iteration 2+: accept (US-002 progresses).
            if iteration == 1:
                return ReviewResult(
                    passed=False, findings="fail", max_severity="major", minor_only=False
                )
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is True
        assert 2 in iteration_seen

        progress = (worktree / "scripts" / "ralph" / "progress.txt").read_text()
        assert "US-001 SKIPPED" in progress

    def test_exits_when_all_stories_skipped(self, tmp_path):
        """Loop terminates cleanly when every story has been skipped."""
        worktree = _setup_worktree(tmp_path)
        self._make_prd(
            worktree,
            [
                {"id": "US-001", "title": "A", "priority": 1, "passes": False},
                {"id": "US-002", "title": "B", "priority": 2, "passes": False},
            ],
        )
        config = _make_config(
            tmp_path,
            max_iterations=10,
            max_iteration_retries=1,
            backout_on_failure=True,
            on_retry_exhaustion="skip-story",
        )

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "reset" in cmd:
                return _fake_subprocess_run(returncode=0)
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            return ReviewResult(
                passed=False, findings="fail", max_severity="critical", minor_only=False
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        # No stories were actually completed — return value depends on any_done
        # which is False when no story got flipped. That's the correct signal.
        assert result is False
        progress = (worktree / "scripts" / "ralph" / "progress.txt").read_text()
        assert "US-001 SKIPPED" in progress
        assert "US-002 SKIPPED" in progress


# ── Issue #126: same-finding convergence detection ─────────────────────


class TestFindingsSimilarity:
    """Unit tests for the ``findings_similarity`` helper."""

    def test_identical_returns_one(self):
        from ralph_pp.steps.sandbox import findings_similarity

        assert findings_similarity("foo bar baz", "foo bar baz") == 1.0

    def test_disjoint_returns_zero(self):
        from ralph_pp.steps.sandbox import findings_similarity

        assert findings_similarity("alpha beta gamma", "delta epsilon zeta") == 0.0

    def test_empty_inputs_return_zero(self):
        from ralph_pp.steps.sandbox import findings_similarity

        assert findings_similarity("", "anything") == 0.0
        assert findings_similarity("anything", "") == 0.0

    def test_case_insensitive(self):
        from ralph_pp.steps.sandbox import findings_similarity

        assert findings_similarity("Foo Bar", "foo bar") == 1.0

    def test_ignores_punctuation_and_ordering(self):
        from ralph_pp.steps.sandbox import findings_similarity

        assert findings_similarity("foo, bar: baz!", "baz bar foo") == 1.0

    def test_short_tokens_filtered(self):
        """Tokens <3 chars are dropped to reduce noise."""
        from ralph_pp.steps.sandbox import findings_similarity

        # "is" and "a" are filtered — only "problem" matches
        sim = findings_similarity("is a problem", "a problem is")
        assert sim == 1.0

    def test_partial_overlap_returns_jaccard(self):
        from ralph_pp.steps.sandbox import findings_similarity

        # {"alpha", "beta"} vs {"beta", "gamma"} → 1/3
        sim = findings_similarity("alpha beta", "beta gamma")
        assert abs(sim - (1 / 3)) < 1e-9

    def test_verbatim_repeat_scores_high(self):
        """Realistic #126 case: reviewer produces essentially the same block.

        The observed bug was that the reviewer produced verbatim-same
        findings across retries (the coder kept making cosmetic edits that
        didn't change what the reviewer saw). Against verbatim input the
        Jaccard score should be well above the default threshold (0.75).
        """
        from ralph_pp.steps.sandbox import findings_similarity

        a = (
            "1. severity: critical\nfile: ralph_pp/agent.py\n"
            "problem: Agent.__init__ does not wire MemoryFacade\n"
            "evidence: line 42 imports MemoryFacade but never instantiates it"
        )
        # Simulate the coder making a cosmetic change that doesn't address
        # the finding: reviewer repeats the same block with trivial edits.
        b = (
            "1. severity: critical\nfile: ralph_pp/agent.py\n"
            "problem: Agent.__init__ still does not wire MemoryFacade\n"
            "evidence: line 42 imports MemoryFacade but never instantiates it"
        )
        assert findings_similarity(a, b) >= 0.75

    def test_rephrased_same_issue_has_meaningful_overlap(self):
        """Different phrasing of the same issue should show non-trivial overlap
        even if it does not cross the default threshold."""
        from ralph_pp.steps.sandbox import findings_similarity

        a = (
            "1. severity: critical\nfile: ralph_pp/agent.py\n"
            "problem: Agent.__init__ does not wire MemoryFacade\n"
        )
        b = (
            "CRITICAL: ralph_pp/agent.py still missing MemoryFacade wiring.\n"
            "At Agent.__init__ the MemoryFacade is never instantiated."
        )
        sim = findings_similarity(a, b)
        assert sim > 0.25, f"expected meaningful overlap, got {sim}"


class TestSameFindingConvergence:
    """Integration: same-finding convergence should abort the retry loop."""

    def test_backout_mode_aborts_on_same_finding_convergence(self, tmp_path):
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path,
            max_iterations=1,
            max_iteration_retries=5,  # would normally allow 6 attempts
            backout_on_failure=True,
        )
        config.orchestrated.max_same_finding_retries = 2  # stop after 2 repeats
        coder_calls = {"n": 0}

        # Reviewer always returns the same findings
        same_findings = (
            "1. severity: critical\nfile: ralph_pp/agent.py\n"
            "problem: Agent.__init__ does not wire MemoryFacade\n"
            "evidence: line 42 imports MemoryFacade but never instantiates it"
        )

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "reset" in cmd:
                return _fake_subprocess_run(returncode=0)
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            coder_calls["n"] += 1
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            return ReviewResult(
                passed=False,
                findings=same_findings,
                max_severity="critical",
                minor_only=False,
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is False
        # Initial attempt + 2 retries that each triggered a same-finding
        # rejection → 3 total coder calls, then abort. Without the
        # convergence detection this would have run all 6 attempts.
        assert coder_calls["n"] == 3, (
            f"Expected convergence detection to stop at 3 calls, got {coder_calls['n']}"
        )

    def test_disabled_when_zero(self, tmp_path):
        """max_same_finding_retries=0 disables convergence detection."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path,
            max_iterations=1,
            max_iteration_retries=2,
            backout_on_failure=True,
        )
        config.orchestrated.max_same_finding_retries = 0
        coder_calls = {"n": 0}
        same_findings = "problem: broken"

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "reset" in cmd:
                return _fake_subprocess_run(returncode=0)
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            coder_calls["n"] += 1
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            return ReviewResult(
                passed=False, findings=same_findings, max_severity="major", minor_only=False
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        # All 3 attempts should run (initial + 2 retries) because
        # convergence detection is disabled.
        assert coder_calls["n"] == 3

    def test_differing_findings_do_not_trip_breaker(self, tmp_path):
        """When findings change between retries, convergence detection stays quiet."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path,
            max_iterations=1,
            max_iteration_retries=2,
            backout_on_failure=True,
        )
        config.orchestrated.max_same_finding_retries = 2
        coder_calls = {"n": 0}

        # Each retry gets different findings (different files, different problems)
        findings_seq = iter(
            [
                "problem: foo.py line 10 missing import",
                "problem: bar.py line 42 wrong type annotation",
                "problem: baz.py line 7 unused variable",
            ]
        )

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "reset" in cmd:
                return _fake_subprocess_run(returncode=0)
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            coder_calls["n"] += 1
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            return ReviewResult(
                passed=False,
                findings=next(findings_seq, "done"),
                max_severity="major",
                minor_only=False,
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        # All 3 attempts should run because findings are different each time.
        assert coder_calls["n"] == 3

    def test_escalation_header_injected_on_repeat(self, tmp_path):
        """When the retry prompt is regenerated for a repeat, it uses the escalation header."""
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path,
            max_iterations=1,
            max_iteration_retries=3,
            backout_on_failure=True,
        )
        config.orchestrated.prompt_template = (
            "iter {iteration} prd {prd_file} progress {progress} findings {review_findings}"
        )
        config.orchestrated.max_same_finding_retries = 10  # don't abort early
        same_findings = "problem: broken specific thing at line 42"

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "reset" in cmd:
                return _fake_subprocess_run(returncode=0)
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_review(*args, **kwargs):
            return ReviewResult(
                passed=False, findings=same_findings, max_severity="major", minor_only=False
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        # After the 3rd attempt's prompt write, the iteration-prompt file
        # should contain the escalation header.
        iter_prompt = worktree / "scripts" / "ralph" / ".iteration-prompt.md"
        assert iter_prompt.exists()
        content = iter_prompt.read_text()
        assert "REPEATED FAILURE" in content
        assert "FUNDAMENTALLY different" in content


# ── Issue #124: emit final Progress line on COMPLETE signal ────────────


class TestFinalProgressOnComplete:
    def test_final_progress_emitted_on_complete_signal(self, tmp_path, capsys):
        """When the coder signals COMPLETE, a Progress line should print before
        handing off to post-run review (#124)."""
        worktree = _setup_worktree(tmp_path)
        prd_json = worktree / "scripts" / "ralph" / "prd.json"
        prd_json.write_text(
            json.dumps(
                {
                    "userStories": [
                        {"id": "US-001", "title": "A", "passes": True},
                        {"id": "US-002", "title": "B", "passes": True},
                        {"id": "US-003", "title": "C", "passes": True},
                    ]
                }
            )
        )
        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=0)

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(
                returncode=0,
                stdout="All stories complete.\n<promise>COMPLETE</promise>",
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is True
        captured = capsys.readouterr()
        assert "Progress: 3/3 stories done" in captured.out
        assert "Ralph signaled COMPLETE" in captured.out

    def test_partial_complete_signal_does_not_emit_final_progress(self, tmp_path, capsys):
        """If the coder signals COMPLETE while stories are still false,
        the function continues and the final-progress line MUST NOT fire."""
        worktree = _setup_worktree(tmp_path)
        prd_json = worktree / "scripts" / "ralph" / "prd.json"
        prd_json.write_text(
            json.dumps(
                {
                    "userStories": [
                        {"id": "US-001", "title": "A", "passes": True},
                        {"id": "US-002", "title": "B", "passes": False},
                    ]
                }
            )
        )
        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=0)

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(
                returncode=0,
                stdout="kind of done\n<promise>COMPLETE</promise>",
            )

        def mock_review(*args, **kwargs):
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        captured = capsys.readouterr()
        assert "still have passes=false" in captured.out


# ── Issue #82: log when reviewer_timeout is shadowed by tool timeout ───


class TestReviewerTimeoutPrecedenceLogging:
    def test_logs_debug_when_reviewer_timeout_shadowed(self, tmp_path, caplog):
        """When tool timeout is set, orchestrated.reviewer_timeout is silently
        ignored. #82 asks for a debug log so users can debug this surprise."""
        import logging

        from ralph_pp.steps.sandbox import _review_iteration

        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path)
        # Tool already has its own timeout — should win
        config.tools["codex"] = ToolConfig(command="codex", args=["{prompt}"], timeout=42)
        config.orchestrated.reviewer_timeout = 999

        with (
            patch("ralph_pp.steps.sandbox.CliTool") as mock_tool_cls,
            caplog.at_level(logging.DEBUG, logger="ralph_pp.steps.sandbox"),
        ):
            mock_tool = MagicMock()
            mock_tool.run.return_value = ToolResult(success=True, output="LGTM", exit_code=0)
            mock_tool_cls.return_value = mock_tool
            _review_iteration(1, "diff", worktree, config)

        # Verify the precedence log fired
        assert any(
            "reviewer_timeout=999 ignored" in r.message and "timeout=42" in r.message
            for r in caplog.records
        ), f"expected precedence debug log, got {[r.message for r in caplog.records]}"

    def test_no_log_when_only_orchestrated_timeout_set(self, tmp_path, caplog):
        """When only orchestrated.reviewer_timeout is set, it applies cleanly
        and there should be no precedence-warning log."""
        import logging

        from ralph_pp.steps.sandbox import _review_iteration

        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path)
        config.tools["codex"] = ToolConfig(command="codex", args=["{prompt}"], timeout=0)
        config.orchestrated.reviewer_timeout = 999

        with (
            patch("ralph_pp.steps.sandbox.CliTool") as mock_tool_cls,
            caplog.at_level(logging.DEBUG, logger="ralph_pp.steps.sandbox"),
        ):
            mock_tool = MagicMock()
            mock_tool.run.return_value = ToolResult(success=True, output="LGTM", exit_code=0)
            mock_tool_cls.return_value = mock_tool
            _review_iteration(1, "diff", worktree, config)

        assert not any("ignored" in r.message for r in caplog.records)


# ── Issue #125: subprocess output with brackets must not crash Rich ────


class TestSubprocessOutputMarkupSafe:
    """Issue #125: raw subprocess output containing [path/to/file.py] style
    text must not crash Rich's markup parser."""

    def test_coder_output_with_brackets_does_not_crash(self, tmp_path):
        worktree = _setup_worktree(tmp_path)
        config = _make_config(tmp_path, max_iterations=1, max_iteration_retries=0)

        # Output contains a closing bracket that looks like a tag — this is
        # the exact pattern that crashed Run 1 of the 2026-04-07
        # memory-unification-v4 run.
        bracket_output = (
            "Found 3 issues in [/home/dns/src/ralph_pp/sqlite_memory_store.py]:\n"
            "  [WARN] Unused import\n"
            "  [/CLOSED] mismatched tag\n"
        )

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout=bracket_output)

        def mock_review(*args, **kwargs):
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            # Should NOT raise rich.errors.MarkupError
            _run_orchestrated(worktree, config)

    def test_test_output_with_brackets_does_not_crash(self, tmp_path):
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path,
            max_iterations=1,
            max_iteration_retries=0,
            run_tests=True,
            test_commands=["pytest"],
        )

        bracket_output = (
            "FAILED tests/test_foo.py::test_bar[case-1]\n"
            "  AssertionError at [/home/dns/src/foo.py:42]\n"
        )

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="coder output")

        def mock_run_tests(*args, **kwargs):
            return (True, bracket_output)

        def mock_review(*args, **kwargs):
            return ReviewResult(passed=True, findings="LGTM", max_severity=None, minor_only=True)

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch(
                "ralph_pp.steps.sandbox.run_test_commands_with_output",
                side_effect=mock_run_tests,
            ),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            # Should NOT raise rich.errors.MarkupError
            _run_orchestrated(worktree, config)

    def test_fix_test_output_with_brackets_does_not_crash(self, tmp_path):
        worktree = _setup_worktree(tmp_path)
        config = _make_config(
            tmp_path,
            max_iterations=1,
            max_iteration_retries=1,
            backout_on_failure=False,
            run_tests=True,
            test_commands=["pytest"],
        )

        bracket_output = "FAILED [tests/test_x.py::test_y]\n"

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rev-parse" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="abc1234")
            if isinstance(cmd, list) and "diff" in cmd:
                return _fake_subprocess_run(returncode=0, stdout="some diff")
            return _fake_subprocess_run(returncode=0, stdout="output")

        def mock_run_tests(*args, **kwargs):
            return (False, bracket_output)  # tests fail to drive fixer path

        def mock_review(*args, **kwargs):
            return ReviewResult(
                passed=False, findings="bad", max_severity="major", minor_only=False
            )

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch(
                "ralph_pp.steps.sandbox.run_test_commands_with_output",
                side_effect=mock_run_tests,
            ),
            patch(
                "ralph_pp.steps.sandbox._run_fixer_in_sandbox",
                return_value=_fake_subprocess_run(returncode=0),
            ),
            patch("ralph_pp.steps.sandbox.commit_if_dirty", return_value=False),
            patch("ralph_pp.steps.sandbox.get_head_sha", side_effect=_incrementing_sha()),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            # Should NOT raise rich.errors.MarkupError
            _run_orchestrated(worktree, config)


class TestHooksMarkupSafe:
    def test_user_command_with_brackets_does_not_crash(self, tmp_path):
        """User-configured shell commands with brackets must not crash hooks."""
        from ralph_pp.hooks import run_hooks

        with patch("ralph_pp.hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            # Should NOT raise rich.errors.MarkupError when echoing the command
            run_hooks(
                "post_worktree_create",
                {"post_worktree_create": ["pytest -k 'test_foo[case-1]'"]},
                tmp_path,
            )
