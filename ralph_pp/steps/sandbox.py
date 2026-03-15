"""Docker sandbox invocation for the Ralph loop."""

from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console

from ..config import Config

console = Console()


def run_sandbox(worktree_path: Path, config: Config) -> bool:
    """
    Run the Ralph loop inside the docker sandbox.

    Mounts:
      - worktree as /workspace
      - claude config dir (read-only)
      - codex config dir (read-only)

    Returns True if ralph completed successfully, False otherwise.
    """
    console.print("[bold cyan]\n── Step: Ralph Sandbox ──[/bold cyan]")

    inner = config.inner_review
    ralph_cfg = config.ralph

    # Build reviewer command string for injection into the shell script
    reviewer_tool = config.get_tool(inner.reviewer)
    reviewer_args = [a.replace("{prompt}", "$REVIEW_PROMPT") for a in reviewer_tool.args]
    reviewer_cmd = reviewer_tool.command + (" " + " ".join(reviewer_args) if reviewer_args else "")

    fixer_tool = config.get_tool(inner.fixer)
    if fixer_tool.stdin is not None:
        fixer_cmd = "echo \"$FIX_PROMPT\" | " + fixer_tool.command + " " + " ".join(fixer_tool.args)
    else:
        fixer_args = [a.replace("{prompt}", "$FIX_PROMPT") for a in fixer_tool.args]
        fixer_cmd = fixer_tool.command + (" " + " ".join(fixer_args) if fixer_args else "")

    docker_cmd = [
        "docker", "run", "--rm",
        "-v", str(worktree_path) + ":/workspace",
        "-v", str(config.claude_config_dir) + ":/home/ralph/.claude:ro",
        "-v", str(config.codex_config_dir) + ":/home/ralph/.codex:ro",
        "-e", "SKIP_REVIEW=" + ("0" if inner.enabled else "1"),
        "-e", "MAX_REVIEW_CYCLES=" + str(inner.max_cycles),
        "-e", "REVIEWER_CMD=" + reviewer_cmd,
        "-e", "FIXER_CMD=" + fixer_cmd,
        "-e", "REVIEW_PROMPT_TEMPLATE=" + inner.reviewer_prompt,
        "-e", "FIX_PROMPT_TEMPLATE=" + inner.fixer_prompt,
        ralph_cfg.sandbox_image,
        "--", str(ralph_cfg.max_iterations),
    ]

    console.print("[dim]$ docker run --rm -v " + str(worktree_path) + " ...[/dim]")

    result = subprocess.run(docker_cmd, text=True)

    if result.returncode == 0:
        console.print("[green]✓ Ralph completed successfully[/green]")
        return True
    else:
        console.print("[red]✗ Ralph exited with code " + str(result.returncode) + "[/red]")
        return False
