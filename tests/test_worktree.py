"""Tests for branch name generation and worktree creation."""

from pathlib import Path
from unittest.mock import patch

from ralph_pp.config import load_config
from ralph_pp.steps.worktree import create_worktree, make_branch_name


def test_branch_name_slugified():
    cfg = load_config(None)
    name = make_branch_name("Add User Authentication", cfg)
    assert name.startswith("ralph/")
    assert "add-user-authentication" in name
    # suffix appended
    parts = name.split("-")
    assert len(parts[-1]) == cfg.branch_suffix_length


def test_branch_name_unique():
    cfg = load_config(None)
    names = {make_branch_name("same feature", cfg) for _ in range(5)}
    # With random suffixes, all 5 should be unique
    assert len(names) == 5


def test_branch_name_special_chars():
    cfg = load_config(None)
    name = make_branch_name("Fix: user@email.com validation!", cfg)
    # Should not contain special chars
    assert "@" not in name
    assert "!" not in name
    assert ":" not in name


class TestWorktreeConflictCheck:
    def test_retries_when_path_exists(self, tmp_path: Path):
        """If the generated worktree path already exists, a new suffix is tried."""
        cfg = load_config(None)
        cfg.repo_path = tmp_path / "repo"
        cfg.repo_path.mkdir()

        call_count = 0
        original_make = make_branch_name

        def mock_make(feature, config):
            nonlocal call_count
            call_count += 1
            branch = original_make(feature, config)
            if call_count == 1:
                # Pre-create the directory to simulate a conflict
                conflict = config.repo_path.parent / branch.replace("/", "-")
                conflict.mkdir(parents=True, exist_ok=True)
            return branch

        with (
            patch("ralph_pp.steps.worktree.make_branch_name", side_effect=mock_make),
            patch("ralph_pp.steps.worktree.subprocess.run"),
        ):
            path, branch = create_worktree("test feature", cfg)

        assert call_count >= 2, "Should have retried after conflict"
        assert not path.exists() or call_count > 1


class TestWorktreeRoot:
    """#151 / #153: configurable worktree_root."""

    def test_default_places_worktree_as_repo_sibling(self, tmp_path: Path):
        """Regression guard: with worktree_root unset, worktrees are placed as
        flat siblings of repo_path (existing behavior)."""
        cfg = load_config(None)
        cfg.repo_path = tmp_path / "repo"
        cfg.repo_path.mkdir()
        assert cfg.worktree_root is None

        with patch("ralph_pp.steps.worktree.subprocess.run"):
            path, branch = create_worktree("test feature", cfg)

        assert path.parent == cfg.repo_path.parent
        assert path.name == branch.replace("/", "-")

    def test_custom_worktree_root_honored(self, tmp_path: Path):
        """When worktree_root is set, worktrees are created under that root."""
        cfg = load_config(None)
        cfg.repo_path = tmp_path / "repo"
        cfg.repo_path.mkdir()
        custom_root = tmp_path / "some" / "nested" / "worktrees"
        # Intentionally do NOT pre-create the root — create_worktree must mkdir.
        cfg.worktree_root = custom_root

        with patch("ralph_pp.steps.worktree.subprocess.run"):
            path, branch = create_worktree("test feature", cfg)

        assert custom_root.is_dir(), "worktree_root should be created on demand"
        assert path.parent == custom_root
        assert path.name == branch.replace("/", "-")
        # And crucially, NOT under the repo's parent.
        assert path.parent != cfg.repo_path.parent
