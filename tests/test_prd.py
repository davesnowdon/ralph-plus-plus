"""Tests for PRD generation and conversion artifact validation."""

from unittest.mock import MagicMock, patch

import pytest

from ralph_pp.steps.prd import convert_prd_to_json, feature_to_slug, generate_prd
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

    def test_prd_prompt_used_when_provided(self, tmp_path):
        """When prd_prompt is given, it should appear in the prompt instead of feature."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd-short-name.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            generate_prd(
                "short-name",
                tmp_path,
                config,
                prd_prompt="Unify the dual memory systems behind a single canonical contract",
            )

            call_kwargs = mock_tool.run.call_args[1]
            assert "Unify the dual memory systems" in call_kwargs["prompt"]
            # The short feature name should NOT be in the prompt body
            # (it's only used for the filename)
            assert "Create a PRD for the following feature: short-name" not in call_kwargs["prompt"]

    def test_feature_used_when_prd_prompt_absent(self, tmp_path):
        """When prd_prompt is None, feature is used as the prompt."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd-my-feature.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            generate_prd("my-feature", tmp_path, config)

            call_kwargs = mock_tool.run.call_args[1]
            assert "Create a PRD for the following feature: my-feature" in call_kwargs["prompt"]

    def test_prd_prompt_does_not_affect_filename(self, tmp_path):
        """Filename should be derived from feature, not prd_prompt."""
        config = _make_config()
        # The file that should be created uses the feature slug
        prd_file = tmp_path / "tasks" / "prd-short-name.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            result = generate_prd(
                "short-name",
                tmp_path,
                config,
                prd_prompt="A very long description that would make a terrible filename",
            )

            assert result.name == "prd-short-name.md"


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
