"""Tests for CLI helpers."""

from pathlib import Path

from ralph_pp.cli import _build_overrides


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
