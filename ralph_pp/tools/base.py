"""Abstract base class for AI tool wrappers."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

_SEVERITY_RE = re.compile(r"severity:\s*(critical|major|minor)", re.IGNORECASE)
_SEVERITY_ORDER = {"minor": 0, "major": 1, "critical": 2}


def parse_max_severity(text: str) -> str | None:
    """Return the highest severity label found in *text*, or ``None``."""
    matches = _SEVERITY_RE.findall(text)
    if not matches:
        return None
    return max((m.lower() for m in matches), key=lambda s: _SEVERITY_ORDER[s])


def severity_at_or_above(severity: str, threshold: str) -> bool:
    """True if *severity* meets or exceeds *threshold*."""
    return _SEVERITY_ORDER[severity.lower()] >= _SEVERITY_ORDER[threshold.lower()]


@dataclass
class ToolResult:
    output: str
    exit_code: int
    success: bool

    @property
    def is_lgtm(self) -> bool:
        """True if the tool output signals no issues found."""
        stripped = self.output.strip()
        return stripped == "LGTM" or stripped.startswith("LGTM\n")


class BaseTool(ABC):
    """Abstract interface for a runnable AI tool."""

    @abstractmethod
    def run(self, prompt: str, cwd: Path, extra_env: dict[str, str] | None = None) -> ToolResult:
        """Run the tool with the given prompt in the given working directory."""
        ...
