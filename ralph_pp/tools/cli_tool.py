"""Generic CLI tool wrapper (covers codex, claude, and custom commands)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from rich.console import Console

from ..config import ToolConfig
from .base import BaseTool, ToolResult

console = Console()


class CliTool(BaseTool):
    """Runs any CLI tool defined in config, with optional stdin prompt."""

    def __init__(self, name: str, config: ToolConfig) -> None:
        self.name = name
        self.config = config

    def run(
        self,
        prompt: str,
        cwd: Path,
        extra_env: dict[str, str] | None = None,
    ) -> ToolResult:
        env = os.environ.copy()
        for k, v in self.config.env.items():
            env[k] = v
        if extra_env:
            env.update(extra_env)

        # Build args list, substituting {prompt} placeholder
        args = [self.config.command] + [a.replace("{prompt}", prompt) for a in self.config.args]

        # Determine stdin
        stdin_data: str | None = None
        if self.config.stdin is not None:
            stdin_data = self.config.stdin.replace("{prompt}", prompt)

        preview = " ".join(args[:2]) + (" ..." if len(args) > 2 else "")
        console.print("[bold green]→ " + self.name + ":[/bold green] " + preview)

        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            input=stdin_data,
            text=True,
            capture_output=True,
        )

        if result.stdout:
            console.print(result.stdout)
        if result.stderr:
            console.print("[dim]" + result.stderr + "[/dim]")

        return ToolResult(
            output=result.stdout + result.stderr,
            exit_code=result.returncode,
            success=result.returncode == 0,
        )
