"""Tests for PRD generation and conversion artifact validation."""

from unittest.mock import MagicMock, patch

import pytest

from ralph_pp.steps.prd import convert_prd_to_json, generate_prd
from ralph_pp.tools.base import ToolResult


def _make_config():
    """Minimal config with default tools."""
    from ralph_pp.config import Config, ToolConfig

    return Config(
        tools={
            "claude": ToolConfig(command="claude", args=["--print"], stdin="{prompt}"),
        }
    )


class TestGeneratePrd:
    def test_raises_when_file_missing_after_success(self, tmp_path):
        """Exit 0 but tasks/prd.md not created should raise."""
        config = _make_config()
        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            with pytest.raises(RuntimeError, match="was not created"):
                generate_prd("test feature", tmp_path, config)

    def test_succeeds_when_file_exists(self, tmp_path):
        """Exit 0 with tasks/prd.md present should succeed."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
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
