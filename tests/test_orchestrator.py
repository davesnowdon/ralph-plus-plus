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
    def test_worktree_path_reported_on_failure(
        self, mock_wt, mock_prd, mock_sandbox, mock_post, mock_clean, tmp_path, capsys
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
    def test_no_worktree_message_when_worktree_not_created(self, mock_wt, tmp_path, capsys):
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
    def test_resume_skips_worktree_and_prd(self, mock_sandbox, mock_post, mock_clean, tmp_path):
        from ralph_pp.config import Config

        cfg = Config.__new__(Config)

        # Set up a fake worktree with prd.json
        wt = tmp_path / "worktree"
        wt.mkdir()
        (wt / "scripts" / "ralph").mkdir(parents=True)
        (wt / "scripts" / "ralph" / "prd.json").write_text(
            '{"userStories": [{"id": "US-001", "passes": false}]}'
        )

        # Init a git repo in the worktree so rev-parse works
        import subprocess

        subprocess.run(["git", "init"], cwd=wt, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=wt,
            check=True,
            capture_output=True,
        )

        orch = Orchestrator("test-feature", cfg, resume_worktree=wt)
        orch.run()

        assert orch.worktree_path == wt
        assert orch.branch is not None
        mock_sandbox.assert_called_once()

    def test_resume_fails_if_no_prd_json(self, tmp_path):
        from ralph_pp.config import Config

        cfg = Config.__new__(Config)
        wt = tmp_path / "worktree"
        wt.mkdir()

        orch = Orchestrator("test-feature", cfg, resume_worktree=wt)
        with pytest.raises(FileNotFoundError, match="prd.json"):
            orch.run()
