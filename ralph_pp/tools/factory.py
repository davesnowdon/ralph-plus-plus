"""Tool factory — creates the right tool wrapper from config."""

from __future__ import annotations

import dataclasses

from ..config import Config, ToolConfig
from .base import BaseTool
from .cli_tool import CliTool
from .permissions import bash_permissions_from_commands


def make_tool(name: str, config: Config) -> BaseTool:
    """Instantiate a tool by name using the config."""
    tool_cfg: ToolConfig = config.get_tool(name)
    return CliTool(name=name, config=tool_cfg)


def make_tool_with_permissions(
    name: str,
    config: Config,
    extra_bash_commands: list[str],
) -> BaseTool:
    """Like :func:`make_tool` but augments ``allowed_tools`` with Bash patterns.

    Only augments tools that already have ``allowed_tools`` set (i.e. Claude
    tools that use ``--allowedTools``).  Codex tools are returned unchanged.
    """
    tool_cfg: ToolConfig = config.get_tool(name)
    if not tool_cfg.allowed_tools or not extra_bash_commands:
        return CliTool(name=name, config=tool_cfg)

    extra = bash_permissions_from_commands(extra_bash_commands)
    augmented = dataclasses.replace(
        tool_cfg,
        allowed_tools=list(tool_cfg.allowed_tools) + extra,
    )
    return CliTool(name=name, config=augmented)
