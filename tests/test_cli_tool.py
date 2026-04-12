"""Tests for CliTool, including the large-prompt stdin fallback."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from ralph_pp.config import ToolConfig
from ralph_pp.tools.cli_tool import CliTool


@pytest.fixture
def tool() -> CliTool:
    cfg = ToolConfig(command="echo", args=["{prompt}"])
    return CliTool("test-tool", cfg)


@pytest.fixture
def tool_with_stdin() -> CliTool:
    cfg = ToolConfig(command="echo", args=["--flag"], stdin="{prompt}")
    return CliTool("test-tool-stdin", cfg)


def test_small_prompt_passed_via_args(tool: CliTool, tmp_path: Path) -> None:
    """Normal-sized prompts are embedded in the command-line args."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "ok"
        mock_run.return_value.stderr = ""
        tool.run("hello world", cwd=tmp_path)
        args_used = mock_run.call_args[0][0]
        assert "hello world" in args_used
        assert mock_run.call_args[1].get("input") is None


def test_large_prompt_redirected_to_stdin(tool: CliTool, tmp_path: Path) -> None:
    """Prompts exceeding _ARG_MAX_SAFE are redirected to stdin."""
    big_prompt = "x" * (CliTool._ARG_MAX_SAFE + 1)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "ok"
        mock_run.return_value.stderr = ""
        tool.run(big_prompt, cwd=tmp_path)
        args_used = mock_run.call_args[0][0]
        # Prompt must NOT appear in args
        assert big_prompt not in args_used
        # Prompt must be sent via stdin
        assert mock_run.call_args[1].get("input") == big_prompt


def test_large_prompt_with_explicit_stdin_config(tool_with_stdin: CliTool, tmp_path: Path) -> None:
    """When stdin is already configured, it takes precedence even for large prompts."""
    big_prompt = "x" * (CliTool._ARG_MAX_SAFE + 1)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "ok"
        mock_run.return_value.stderr = ""
        tool_with_stdin.run(big_prompt, cwd=tmp_path)
        # stdin should use the configured template (which includes the prompt)
        assert mock_run.call_args[1].get("input") == big_prompt


def test_prompt_just_under_limit_stays_in_args(tool: CliTool, tmp_path: Path) -> None:
    """Prompts exactly at the threshold remain in args."""
    prompt = "x" * CliTool._ARG_MAX_SAFE
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "ok"
        mock_run.return_value.stderr = ""
        tool.run(prompt, cwd=tmp_path)
        args_used = mock_run.call_args[0][0]
        assert prompt in args_used
