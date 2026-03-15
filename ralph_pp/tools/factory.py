"""Tool factory — creates the right tool wrapper from config."""

from __future__ import annotations

from ..config import Config, ToolConfig
from .base import BaseTool
from .cli_tool import CliTool


def make_tool(name: str, config: Config) -> BaseTool:
    """Instantiate a tool by name using the config."""
    tool_cfg: ToolConfig = config.get_tool(name)
    return CliTool(name=name, config=tool_cfg)
