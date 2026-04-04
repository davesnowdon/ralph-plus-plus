"""Git worktree creation and branch management."""

from __future__ import annotations

import secrets
import subprocess
from pathlib import Path

from rich.console import Console
from slugify import slugify

from ..config import Config

console = Console()


def make_branch_name(feature: str, config: Config) -> str:
    """Derive a unique branch name from the feature description."""
    slug = slugify(feature, max_length=50, separator="-")
    suffix = secrets.token_hex(config.branch_suffix_length // 2)[: config.branch_suffix_length]
    return f"{config.branch_prefix}{slug}-{suffix}"


def create_worktree(feature: str, config: Config) -> tuple[Path, str]:
    """
    Create a git worktree for the feature.

    Returns:
        (worktree_path, branch_name)
    """
    # Try up to a few times in case the generated path already exists
    # (e.g. from a previous failed run with the same random suffix).
    branch = ""
    worktree_path = Path()
    for _attempt in range(5):
        branch = make_branch_name(feature, config)
        worktree_path = config.repo_path.parent / branch.replace("/", "-")
        if not worktree_path.exists():
            break
    else:
        raise RuntimeError(
            f"Could not find a free worktree path after 5 attempts (last tried: {worktree_path})"
        )

    console.print(f"[bold]Creating worktree:[/bold] {worktree_path}")
    console.print(f"[bold]Branch:[/bold] {branch}")

    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path)],
        cwd=config.repo_path,
        check=True,
        text=True,
    )

    console.print(f"[green]✓ Worktree created:[/green] {worktree_path}")
    return worktree_path, branch


def cleanup_git_config(worktree_path: Path) -> None:
    """Remove any locally set git user config from the worktree."""
    console.print("[bold]Cleaning up git config...[/bold]")
    for key in ("user.name", "user.email"):
        subprocess.run(
            ["git", "config", "--unset", key],
            cwd=worktree_path,
            # unset returns exit 5 if key not set — that is fine
            check=False,
            text=True,
        )
    # Verify
    result = subprocess.run(
        ["git", "config", "--list"],
        cwd=worktree_path,
        check=True,
        text=True,
        capture_output=True,
    )
    user_lines = [line for line in result.stdout.splitlines() if line.startswith("user.")]
    if user_lines:
        console.print(f"[yellow]Remaining user config:[/yellow] {user_lines}")
    else:
        console.print("[green]✓ Git user config clean[/green]")
