"""Tests for CLI helpers."""

import subprocess
from pathlib import Path

from click.testing import CliRunner

from ralph_pp.cli import _build_overrides, main


class TestBuildOverrides:
    def test_repo_explicit(self, tmp_path: Path):
        overrides = _build_overrides(
            repo=tmp_path, claude_config=None, codex_config=None, sandbox_dir=None
        )
        assert overrides["repo_path"] == tmp_path

    def test_repo_none_does_not_override(self):
        """When --repo is not passed, repo_path should NOT be in overrides
        so that config-file values are preserved (#33)."""
        overrides = _build_overrides(
            repo=None, claude_config=None, codex_config=None, sandbox_dir=None
        )
        assert "repo_path" not in overrides


def _init_repo(path: Path) -> None:
    """Create a minimal git repo with one commit."""
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
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
        cwd=path,
        check=True,
        capture_output=True,
    )


class TestWorktreesList:
    def test_no_worktrees(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        runner = CliRunner()
        result = runner.invoke(main, ["worktrees", "list", "--repo", str(repo)])
        assert result.exit_code == 0
        assert "No ralph++ worktrees found" in result.output

    def test_lists_ralph_worktrees(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        # Create a ralph worktree
        wt = tmp_path / "ralph-test-wt"
        subprocess.run(
            ["git", "worktree", "add", "-b", "ralph/test-feat", str(wt)],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        runner = CliRunner()
        result = runner.invoke(main, ["worktrees", "list", "--repo", str(repo)])
        assert result.exit_code == 0
        assert "ralph/test-feat" in result.output
        assert "1 worktree(s)" in result.output

        # Cleanup
        subprocess.run(
            ["git", "worktree", "remove", str(wt)], cwd=repo, check=True, capture_output=True
        )


class TestWorktreesClean:
    def test_clean_removes_worktrees(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        wt = tmp_path / "ralph-clean-test"
        subprocess.run(
            ["git", "worktree", "add", "-b", "ralph/clean-test", str(wt)],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        runner = CliRunner()
        # --yes to skip confirmation prompt
        result = runner.invoke(main, ["worktrees", "clean", "--repo", str(repo), "--yes"])
        assert result.exit_code == 0
        assert "Removed 1 worktree" in result.output
        assert not wt.exists()
