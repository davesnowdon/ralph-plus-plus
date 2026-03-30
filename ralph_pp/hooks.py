"""Lifecycle hook runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console

console = Console()


def run_hooks(hook_name: str, hooks: dict[str, list[str]], cwd: Path) -> None:
    """Run all commands registered for a lifecycle hook."""
    commands = hooks.get(hook_name, [])
    if not commands:
        return

    console.print(f"[bold cyan]→ hooks:[/bold cyan] {hook_name}")
    for cmd in commands:
        console.print(f"  [dim]$ {cmd}[/dim]")
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            text=True,
            capture_output=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Hook command failed (exit {result.returncode}): {cmd}")
