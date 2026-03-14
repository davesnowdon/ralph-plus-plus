"""Abstract base class for AI tool wrappers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ToolResult:
    output: str
    exit_code: int
    success: bool

    @property
    def is_lgtm(self) -> bool:
        """True if the tool output signals no issues found."""
        return "LGTM" in self.output


class BaseTool(ABC):
    """Abstract interface for a runnable AI tool."""

    @abstractmethod
    def run(self, prompt: str, cwd: Path, extra_env: dict[str, str] | None = None) -> ToolResult:
        """Run the tool with the given prompt in the given working directory."""
        ...
