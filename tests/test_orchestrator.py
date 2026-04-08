"""Tests for Orchestrator failure handling."""

from pathlib import Path
from unittest.mock import patch

import pytest

from ralph_pp.orchestrator import Orchestrator


class TestWorktreePreservedOnFailure:
    """When the workflow fails after worktree creation, the worktree path and
    branch should be reported but NOT deleted (to support future --resume)."""

    def _make_orchestrator(self, tmp_path: Path) -> Orchestrator:
        from ralph_pp.config import Config

        cfg = Config.__new__(Config)
        orch = Orchestrator("test-feature", cfg)
        orch.worktree_path = tmp_path / "worktree"
        orch.branch = "ralph/test-feature-abc123"
        return orch

    @patch.object(Orchestrator, "_step_cleanup")
    @patch.object(Orchestrator, "_step_post_review")
    @patch.object(Orchestrator, "_step_sandbox", side_effect=RuntimeError("docker crashed"))
    @patch.object(Orchestrator, "_step_prd")
    @patch.object(Orchestrator, "_step_worktree")
    @patch("ralph_pp.orchestrator.validate_sandbox_prerequisites")
    def test_worktree_path_reported_on_failure(
        self,
        mock_validate,
        mock_wt,
        mock_prd,
        mock_sandbox,
        mock_post,
        mock_clean,
        tmp_path,
        capsys,
    ):
        orch = self._make_orchestrator(tmp_path)

        # _step_worktree sets worktree_path; simulate that
        def set_worktree():
            orch.worktree_path = tmp_path / "worktree"
            orch.branch = "ralph/test-feature-abc123"

        mock_wt.side_effect = set_worktree

        with pytest.raises(RuntimeError, match="docker crashed"):
            orch.run()

        captured = capsys.readouterr().out
        assert "Worktree preserved at:" in captured or str(tmp_path / "worktree") in captured

    @patch.object(Orchestrator, "_step_worktree", side_effect=RuntimeError("git failed"))
    @patch("ralph_pp.orchestrator.validate_sandbox_prerequisites")
    def test_no_worktree_message_when_worktree_not_created(
        self, mock_validate, mock_wt, tmp_path, capsys
    ):
        orch = self._make_orchestrator(tmp_path)
        orch.worktree_path = None  # not yet created

        with pytest.raises(RuntimeError, match="git failed"):
            orch.run()

        captured = capsys.readouterr().out
        assert "Worktree preserved" not in captured


class TestResumeWorktree:
    """--resume-worktree should skip worktree creation and PRD, go straight to sandbox."""

    @patch.object(Orchestrator, "_step_cleanup")
    @patch.object(Orchestrator, "_step_post_review")
    @patch.object(Orchestrator, "_step_sandbox")
    @patch("ralph_pp.orchestrator.validate_sandbox_prerequisites")
    def test_resume_skips_worktree_and_prd(
        self, mock_validate, mock_sandbox, mock_post, mock_clean, tmp_path
    ):
        from ralph_pp.config import Config

        cfg = Config.__new__(Config)

        # Set up a fake worktree with prd.json
        wt = tmp_path / "worktree"
        wt.mkdir()
        (wt / "scripts" / "ralph").mkdir(parents=True)
        (wt / "scripts" / "ralph" / "prd.json").write_text(
            '{"userStories": [{"id": "US-001", "passes": false}]}'
        )

        # Create a real linked git worktree so the #84 .git-file check
        # passes. We init a parent repo, make a commit, then `git worktree
        # add` to create wt as a linked worktree.
        import subprocess

        parent = tmp_path / "parent"
        parent.mkdir()
        subprocess.run(["git", "init"], cwd=parent, check=True, capture_output=True)
        for k, v in (("user.email", "test@test.com"), ("user.name", "Test")):
            subprocess.run(["git", "config", k, v], cwd=parent, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=parent,
            check=True,
            capture_output=True,
        )
        # Remove the prepared wt directory so `git worktree add` can create it.
        # We need to preserve the prd.json scaffold, so move it aside first.
        prd_text = (wt / "scripts" / "ralph" / "prd.json").read_text()
        import shutil

        shutil.rmtree(wt)
        subprocess.run(
            ["git", "worktree", "add", "-b", "test-branch", str(wt)],
            cwd=parent,
            check=True,
            capture_output=True,
        )
        # Restore the prd.json scaffold inside the linked worktree.
        (wt / "scripts" / "ralph").mkdir(parents=True)
        (wt / "scripts" / "ralph" / "prd.json").write_text(prd_text)

        orch = Orchestrator("test-feature", cfg, resume_worktree=wt)
        orch.run()

        assert orch.worktree_path == wt
        assert orch.branch is not None
        mock_sandbox.assert_called_once()

    @patch("ralph_pp.orchestrator.validate_sandbox_prerequisites")
    def test_resume_fails_if_no_prd_json(self, mock_validate, tmp_path):
        from ralph_pp.config import Config

        cfg = Config.__new__(Config)
        # Create a real linked git worktree so the #84 check passes,
        # leaving prd.json absent so the test exercises the prd.json
        # validation path.
        import subprocess

        parent = tmp_path / "parent"
        parent.mkdir()
        subprocess.run(["git", "init"], cwd=parent, check=True, capture_output=True)
        for k, v in (("user.email", "test@test.com"), ("user.name", "Test")):
            subprocess.run(["git", "config", k, v], cwd=parent, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=parent,
            check=True,
            capture_output=True,
        )
        wt = tmp_path / "worktree"
        subprocess.run(
            ["git", "worktree", "add", "-b", "test-branch", str(wt)],
            cwd=parent,
            check=True,
            capture_output=True,
        )

        orch = Orchestrator("test-feature", cfg, resume_worktree=wt)
        with pytest.raises(FileNotFoundError, match="prd.json"):
            orch.run()

    @patch("ralph_pp.orchestrator.validate_sandbox_prerequisites")
    def test_resume_rejects_primary_working_tree(self, mock_validate, tmp_path):
        """#84: --resume-worktree must reject a primary working tree."""
        from ralph_pp.config import Config

        cfg = Config.__new__(Config)
        # A regular git init creates a primary working tree (.git is a dir)
        import subprocess

        wt = tmp_path / "primary"
        wt.mkdir()
        subprocess.run(["git", "init"], cwd=wt, check=True, capture_output=True)
        # Add a fake prd.json so we know we get past that check
        (wt / "scripts" / "ralph").mkdir(parents=True)
        (wt / "scripts" / "ralph" / "prd.json").write_text('{"userStories": []}')

        orch = Orchestrator("test-feature", cfg, resume_worktree=wt)
        with pytest.raises(ValueError, match="not a linked git worktree"):
            orch.run()

    @patch("ralph_pp.orchestrator.validate_sandbox_prerequisites")
    def test_resume_rejects_non_git_directory(self, mock_validate, tmp_path):
        """#84: --resume-worktree must reject a directory with no .git."""
        from ralph_pp.config import Config

        cfg = Config.__new__(Config)
        wt = tmp_path / "not-a-repo"
        wt.mkdir()
        (wt / "scripts" / "ralph").mkdir(parents=True)
        (wt / "scripts" / "ralph" / "prd.json").write_text('{"userStories": []}')

        orch = Orchestrator("test-feature", cfg, resume_worktree=wt)
        with pytest.raises(ValueError, match="not a git directory"):
            orch.run()
