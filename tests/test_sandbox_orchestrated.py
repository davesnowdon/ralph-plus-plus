"""Tests for orchestrated sandbox mode — infra failure and test-failure handling."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ralph_pp.config import Config, OrchestratedConfig, RalphConfig, ToolConfig
from ralph_pp.steps.sandbox import _run_fixer_in_sandbox, _run_orchestrated
from ralph_pp.tools.base import ToolResult


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
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )
    return result


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
            return (True, "LGTM")

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
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
            return (True, "LGTM")

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
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
            return (False, "Issues found")

        def mock_fixer(findings, worktree_path, config):
            # Fixer fails
            return _fake_subprocess_run(returncode=1, stderr="fixer crashed")

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._run_fixer_in_sandbox", side_effect=mock_fixer),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
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
            return (True, "LGTM")

        def mock_test_commands(worktree_path, commands):
            return False  # tests fail

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._run_test_commands", side_effect=mock_test_commands),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
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
            return (False, "Issues found")

        def mock_fixer(findings, worktree_path, config):
            return _fake_subprocess_run(returncode=0, stdout="fixed")

        def mock_test_commands(worktree_path, commands):
            nonlocal test_call_count
            test_call_count += 1
            return False  # tests always fail

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._run_fixer_in_sandbox", side_effect=mock_fixer),
            patch("ralph_pp.steps.sandbox._run_test_commands", side_effect=mock_test_commands),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
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
                return (False, "Issues found")
            return (True, "LGTM")

        def mock_fixer(findings, worktree_path, config):
            return _fake_subprocess_run(returncode=0, stdout="fixed")

        def mock_test_commands(worktree_path, commands):
            nonlocal test_call_count
            test_call_count += 1
            if test_call_count == 1:
                return False  # initial tests fail
            return True  # tests pass after fix

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._run_fixer_in_sandbox", side_effect=mock_fixer),
            patch("ralph_pp.steps.sandbox._run_test_commands", side_effect=mock_test_commands),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
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
        from ralph_pp.steps.sandbox import _get_head_sha

        with patch("ralph_pp.steps.sandbox.subprocess.run") as mock_run:
            mock_run.return_value = _fake_subprocess_run(
                returncode=128, stderr="fatal: not a git repository"
            )
            with pytest.raises(RuntimeError, match="git rev-parse HEAD failed"):
                _get_head_sha(tmp_path)

    def test_get_diff_raises_on_failure(self, tmp_path):
        from ralph_pp.steps.sandbox import _get_diff

        with patch("ralph_pp.steps.sandbox.subprocess.run") as mock_run:
            mock_run.return_value = _fake_subprocess_run(
                returncode=128, stderr="fatal: bad revision"
            )
            with pytest.raises(RuntimeError, match="git diff failed"):
                _get_diff(tmp_path, "abc1234")


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
            return (True, "LGTM")

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
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
        assert "prd.json" in content


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
            return (False, "Major flaws found")

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            result = _run_orchestrated(worktree, config)

        assert result is False, "Should fail when retries exhausted"
        assert coder_call_count == 2, "Should run initial + 1 retry, then abort (not start iter 2)"

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
            return (False, "Major flaws found")

        with (
            patch("ralph_pp.steps.sandbox.subprocess.run", side_effect=mock_subprocess_run),
            patch("ralph_pp.steps.sandbox._review_iteration", side_effect=mock_review),
            patch("ralph_pp.steps.sandbox._commit_if_dirty", return_value=False),
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
            cwd=path, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=path, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=path, check=True, capture_output=True,
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
                return (False, "Issues found")
            return (True, "LGTM")

        def mock_fixer(findings, worktree_path, config):
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
            patch(
                "ralph_pp.steps.sandbox._session_runner_path",
                return_value=tmp_path / "scripts" / "ralph-single-step.sh",
            ),
        ):
            _run_orchestrated(worktree, config)

        # Should have committed after coder and after fixer
        assert any("coder" in m for m in commit_calls), "Should commit after coder"
        assert any("fixer" in m for m in commit_calls), "Should commit after fixer"
