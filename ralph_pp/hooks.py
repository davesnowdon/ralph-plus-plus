"""Lifecycle hook runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console
from rich.markup import escape

console = Console()


def run_hooks(hook_name: str, hooks: dict[str, list[str]], cwd: Path) -> None:
    """Run all commands registered for a lifecycle hook."""
    commands = hooks.get(hook_name, [])
    if not commands:
        return

    console.print(f"[bold cyan]→ hooks:[/bold cyan] {escape(hook_name)}")
    for cmd in commands:
        # User-configured shell commands may contain brackets (e.g.
        # `pytest -k 'test_x[1]'`) that Rich would parse as markup. #125
        console.print(f"  [dim]$ {escape(cmd)}[/dim]")
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            text=True,
            capture_output=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Hook command failed (exit {result.returncode}): {cmd}")
