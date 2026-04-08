"""Tests for CLI helpers."""

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from ralph_pp.cli import (
    WorktreeInfo,
    _build_overrides,
    _filter_worktrees_for_clean,
    _format_age,
    _parse_duration,
    main,
)


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

    def test_list_shows_dirty_marker(self, tmp_path: Path):
        """#95: dirty worktrees should be marked in list output."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        wt = tmp_path / "ralph-dirty-list"
        subprocess.run(
            ["git", "worktree", "add", "-b", "ralph/dirty-list", str(wt)],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        (wt / "untracked.txt").write_text("dirty")

        runner = CliRunner()
        result = runner.invoke(main, ["worktrees", "list", "--repo", str(repo)])
        assert result.exit_code == 0
        assert "ralph/dirty-list" in result.output
        assert "[dirty]" in result.output

        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt)],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    def test_list_shows_age(self, tmp_path: Path):
        """#99: list output should include a relative age."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        wt = tmp_path / "ralph-age-test"
        subprocess.run(
            ["git", "worktree", "add", "-b", "ralph/age-test", str(wt)],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        runner = CliRunner()
        result = runner.invoke(main, ["worktrees", "list", "--repo", str(repo)])
        assert result.exit_code == 0
        # Just-created worktree should show seconds-ago or 0m
        assert "ago" in result.output

        subprocess.run(
            ["git", "worktree", "remove", str(wt)],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    def test_clean_dry_run(self, tmp_path: Path):
        """#98: --dry-run reports what would be removed without acting."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        wt = tmp_path / "ralph-dryrun-test"
        subprocess.run(
            ["git", "worktree", "add", "-b", "ralph/dryrun-test", str(wt)],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        runner = CliRunner()
        result = runner.invoke(
            main, ["worktrees", "clean", "--repo", str(repo), "--dry-run", "--yes"]
        )
        assert result.exit_code == 0
        assert "Would remove" in result.output
        assert "Dry run" in result.output
        # Worktree must still exist
        assert wt.exists()

        # Cleanup
        subprocess.run(
            ["git", "worktree", "remove", str(wt)],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    def test_clean_branch_filter(self, tmp_path: Path):
        """#100: --branch glob limits which worktrees are considered."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        wt_a = tmp_path / "ralph-keep-me"
        wt_b = tmp_path / "ralph-trash-me"
        for wt, branch in ((wt_a, "ralph/keep-me"), (wt_b, "ralph/trash-me")):
            subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(wt)],
                cwd=repo,
                check=True,
                capture_output=True,
            )

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "worktrees",
                "clean",
                "--repo",
                str(repo),
                "--yes",
                "--branch",
                "ralph/trash-*",
            ],
        )
        assert result.exit_code == 0
        assert wt_a.exists(), "non-matching worktree must be preserved"
        assert not wt_b.exists(), "matching worktree must be removed"

        # Cleanup
        subprocess.run(
            ["git", "worktree", "remove", str(wt_a)],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    def test_clean_keep_branches(self, tmp_path: Path):
        """#97: --keep-branches preserves the underlying branch ref."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        wt = tmp_path / "ralph-keep-branch"
        subprocess.run(
            ["git", "worktree", "add", "-b", "ralph/keep-branch", str(wt)],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["worktrees", "clean", "--repo", str(repo), "--yes", "--keep-branches"],
        )
        assert result.exit_code == 0
        assert not wt.exists()
        # Branch should still exist
        branches = subprocess.run(
            ["git", "branch", "--list", "ralph/keep-branch"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        )
        assert "ralph/keep-branch" in branches.stdout

    def test_clean_default_deletes_branches(self, tmp_path: Path):
        """Default behavior (#97 status quo): branches are deleted."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        wt = tmp_path / "ralph-del-branch"
        subprocess.run(
            ["git", "worktree", "add", "-b", "ralph/del-branch", str(wt)],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        runner = CliRunner()
        result = runner.invoke(main, ["worktrees", "clean", "--repo", str(repo), "--yes"])
        assert result.exit_code == 0

        branches = subprocess.run(
            ["git", "branch", "--list", "ralph/del-branch"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        )
        assert "ralph/del-branch" not in branches.stdout

    def test_clean_skips_dirty_worktrees_without_force(self, tmp_path: Path):
        """Dirty worktrees are skipped (not failed) unless --force is given.

        Replaces the legacy assertion that exit code was 1 — that was the
        old fail-hard behavior. The new contract (#95): dirty worktrees
        are reported as skipped, exit code stays 0 unless an actual
        removal failed.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        wt = tmp_path / "ralph-dirty-test"
        subprocess.run(
            ["git", "worktree", "add", "-b", "ralph/dirty-test", str(wt)],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        # Make the worktree dirty
        (wt / "untracked.txt").write_text("dirty")

        runner = CliRunner()
        result = runner.invoke(main, ["worktrees", "clean", "--repo", str(repo), "--yes"])
        assert result.exit_code == 0
        assert "Skip (dirty)" in result.output or "Skipped" in result.output
        assert wt.exists(), "dirty worktree should still exist"

        # --force should remove it
        result = runner.invoke(
            main, ["worktrees", "clean", "--repo", str(repo), "--yes", "--force"]
        )
        assert result.exit_code == 0
        assert not wt.exists()


class TestParseDuration:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("30s", 30),
            ("5m", 300),
            ("2h", 7200),
            ("7d", 7 * 86400),
            ("1w", 7 * 86400),
            ("90s", 90),
        ],
    )
    def test_valid(self, value, expected):
        assert _parse_duration(value) == expected

    @pytest.mark.parametrize(
        "value",
        ["", "abc", "5", "5x", "-3d", "1.5h", " "],
    )
    def test_invalid_raises(self, value):
        import click as _click

        with pytest.raises(_click.BadParameter):
            _parse_duration(value)


class TestFormatAge:
    def test_unknown(self):
        assert _format_age(None) == "?"

    @pytest.mark.parametrize(
        "secs,expected_substr",
        [
            (5, "5s ago"),
            (90, "1m ago"),
            (3600, "1h ago"),
            (86400, "1d ago"),
            (86400 * 30, "4w ago"),
        ],
    )
    def test_format(self, secs, expected_substr):
        assert _format_age(secs) == expected_substr


class TestFilterWorktreesForClean:
    def _make(self, branch, age=100, dirty=False):
        return WorktreeInfo(
            path=f"/tmp/{branch.replace('/', '-')}",
            branch=branch,
            dirty=dirty,
            last_commit_age_seconds=age,
        )

    def test_no_filters_returns_all(self):
        entries = [self._make("ralph/a"), self._make("ralph/b")]
        assert _filter_worktrees_for_clean(entries, older_than=None, branch_pattern=None) == entries

    def test_branch_pattern(self):
        entries = [
            self._make("ralph/keep-a"),
            self._make("ralph/trash-1"),
            self._make("ralph/trash-2"),
        ]
        result = _filter_worktrees_for_clean(
            entries, older_than=None, branch_pattern="ralph/trash-*"
        )
        assert {e.branch for e in result} == {"ralph/trash-1", "ralph/trash-2"}

    def test_older_than(self):
        entries = [
            self._make("ralph/recent", age=60),
            self._make("ralph/old", age=86400 * 10),
        ]
        result = _filter_worktrees_for_clean(entries, older_than=86400 * 7, branch_pattern=None)
        assert {e.branch for e in result} == {"ralph/old"}

    def test_older_than_skips_unknown_age(self):
        """Worktrees with unknown age (missing dir) are NOT removed."""
        entries = [self._make("ralph/unknown", age=None)]
        result = _filter_worktrees_for_clean(entries, older_than=86400, branch_pattern=None)
        assert result == []

    def test_combined_filters(self):
        entries = [
            self._make("ralph/keep-recent", age=60),
            self._make("ralph/keep-old", age=86400 * 10),
            self._make("ralph/trash-recent", age=60),
            self._make("ralph/trash-old", age=86400 * 10),
        ]
        result = _filter_worktrees_for_clean(
            entries, older_than=86400 * 7, branch_pattern="ralph/trash-*"
        )
        assert {e.branch for e in result} == {"ralph/trash-old"}
