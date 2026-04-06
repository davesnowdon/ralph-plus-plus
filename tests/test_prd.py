"""Tests for PRD generation, conversion, and prd.json review."""

from unittest.mock import MagicMock, patch

import pytest

from ralph_pp.steps.prd import (
    convert_prd_to_json,
    feature_to_slug,
    generate_prd,
    review_prd_json_loop,
)
from ralph_pp.tools.base import ToolResult


def _make_config():
    """Minimal config with default tools."""
    from ralph_pp.config import Config, ToolConfig

    return Config(
        tools={
            "claude-interactive": ToolConfig(
                command="claude",
                args=["{prompt}"],
                interactive=True,
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash(git:*)"],
            ),
            "codex": ToolConfig(command="codex", args=["{prompt}"]),
            "claude": ToolConfig(
                command="claude",
                args=["--print"],
                stdin="{prompt}",
            ),
        }
    )


class TestFeatureToSlug:
    def test_simple(self):
        assert feature_to_slug("test feature") == "test-feature"

    def test_mixed_case_and_punctuation(self):
        result = feature_to_slug("Freeze Canonical Memory Contracts")
        assert result == "freeze-canonical-memory-contracts"

    def test_special_characters(self):
        assert feature_to_slug("add foo/bar support!") == "add-foobar-support"

    def test_multiple_spaces_and_dashes(self):
        assert feature_to_slug("  lots   of   spaces  ") == "lots-of-spaces"


class TestGeneratePrd:
    def test_raises_when_file_missing_after_success(self, tmp_path):
        """Exit 0 but tasks/prd-*.md not created should raise."""
        config = _make_config()
        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            with pytest.raises(RuntimeError, match="was not created"):
                generate_prd("test feature", tmp_path, config)

    def test_succeeds_when_file_exists(self, tmp_path):
        """Exit 0 with tasks/prd-test-feature.md present should succeed."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd-test-feature.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD\nSome content")

        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            result = generate_prd("test feature", tmp_path, config)
            assert result == prd_file

    def test_raises_on_tool_failure(self, tmp_path):
        """Non-zero exit should raise before file check."""
        config = _make_config()
        fake_result = ToolResult(output="Error", exit_code=1, success=False)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            with pytest.raises(RuntimeError, match="PRD generation failed"):
                generate_prd("test feature", tmp_path, config)


class TestConvertPrdToJson:
    def test_raises_when_json_missing_after_success(self, tmp_path):
        """Exit 0 but scripts/ralph/prd.json not created should raise."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            with pytest.raises(RuntimeError, match="was not created"):
                convert_prd_to_json(prd_file, tmp_path, config)

    def test_succeeds_when_json_exists(self, tmp_path):
        """Exit 0 with prd.json present should succeed."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"stories": []}')

        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            result = convert_prd_to_json(prd_file, tmp_path, config)
            assert result == prd_json

    def test_raises_when_json_invalid(self, tmp_path):
        """Exit 0 with malformed prd.json should raise."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text("this is not json {{{")

        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            with pytest.raises(RuntimeError, match="not valid JSON"):
                convert_prd_to_json(prd_file, tmp_path, config)

    def test_raises_when_json_empty(self, tmp_path):
        """Exit 0 with empty prd.json should raise."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text("")

        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            with pytest.raises(RuntimeError, match="not valid JSON"):
                convert_prd_to_json(prd_file, tmp_path, config)


class TestReviewPrdJsonLoop:
    def test_lgtm_on_first_cycle(self, tmp_path):
        """LGTM from reviewer on first cycle returns immediately."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        lgtm_result = ToolResult(output="LGTM", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_reviewer = MagicMock()
            mock_reviewer.run.return_value = lgtm_result
            mock_fixer = MagicMock()
            mock_make.side_effect = [mock_reviewer, mock_fixer]

            review_prd_json_loop(prd_file, prd_json, tmp_path, config)

            assert mock_reviewer.run.call_count == 1
            mock_fixer.run.assert_not_called()

    def test_issues_trigger_fixer_then_re_review(self, tmp_path):
        """Non-LGTM triggers fixer, then re-reviews."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        issue_result = ToolResult(
            output="1. severity: major\n   problem: criterion infeasible",
            exit_code=0,
            success=True,
        )
        lgtm_result = ToolResult(output="LGTM", exit_code=0, success=True)
        fix_result = ToolResult(output="Fixed", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_reviewer = MagicMock()
            mock_reviewer.run.side_effect = [issue_result, lgtm_result]
            mock_fixer = MagicMock()
            mock_fixer.run.return_value = fix_result
            mock_make.side_effect = [mock_reviewer, mock_fixer]

            review_prd_json_loop(prd_file, prd_json, tmp_path, config)

            assert mock_reviewer.run.call_count == 2
            assert mock_fixer.run.call_count == 1

    def test_max_cycles_exhaustion_continues(self, tmp_path):
        """Max cycles reached without LGTM warns but continues."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        issue_result = ToolResult(
            output="1. severity: major\n   problem: still broken",
            exit_code=0,
            success=True,
        )
        fix_result = ToolResult(output="Attempted fix", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_reviewer = MagicMock()
            mock_reviewer.run.return_value = issue_result
            mock_fixer = MagicMock()
            mock_fixer.run.return_value = fix_result
            mock_make.side_effect = [mock_reviewer, mock_fixer]

            # Should not raise — just warns and continues
            review_prd_json_loop(prd_file, prd_json, tmp_path, config)

            # Default max_cycles is 2
            assert mock_reviewer.run.call_count == 2
            assert mock_fixer.run.call_count == 2

    def test_disabled_skips_review(self, tmp_path):
        """Disabled config skips the review entirely."""
        from ralph_pp.config import PrdJsonReviewConfig

        config = _make_config()
        config.prd_json_review = PrdJsonReviewConfig(enabled=False)

        prd_file = tmp_path / "tasks" / "prd.md"
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            review_prd_json_loop(prd_file, prd_json, tmp_path, config)
            mock_make.assert_not_called()

    def test_reviewer_prompt_includes_repo_path(self, tmp_path):
        """Reviewer prompt should include the codebase path."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        lgtm_result = ToolResult(output="LGTM", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_reviewer = MagicMock()
            mock_reviewer.run.return_value = lgtm_result
            mock_fixer = MagicMock()
            mock_make.side_effect = [mock_reviewer, mock_fixer]

            review_prd_json_loop(prd_file, prd_json, tmp_path, config)

            call_kwargs = mock_reviewer.run.call_args[1]
            assert str(tmp_path) in call_kwargs["prompt"]
