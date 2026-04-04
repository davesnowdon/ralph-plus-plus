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
    slug = slugify(feature, max_length=50, separator="-") or "feature"
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


def snapshot_local_config(worktree_path: Path) -> set[str]:
    """Return the set of local git config keys present in the worktree."""
    result = subprocess.run(
        ["git", "config", "--local", "--list"],
        cwd=worktree_path,
        text=True,
        capture_output=True,
        check=False,
    )
    keys: set[str] = set()
    for line in result.stdout.splitlines():
        key, _, _ = line.partition("=")
        if key:
            keys.add(key)
    return keys


def cleanup_git_config(worktree_path: Path, baseline_keys: set[str] | None = None) -> None:
    """Remove locally set git config keys that were added after worktree creation.

    If *baseline_keys* is provided, unsets any local key not in the
    baseline. Otherwise falls back to unsetting user.name and user.email.
    """
    console.print("[bold]Cleaning up git config...[/bold]")

    current_keys = snapshot_local_config(worktree_path)
    keys_to_remove: set[str] = set()
    if baseline_keys is not None:
        keys_to_remove = current_keys - baseline_keys
    else:
        # Fallback when no baseline: only remove identity keys
        keys_to_remove = current_keys & {"user.name", "user.email"}

    removed = 0
    for key in sorted(keys_to_remove):
        result = subprocess.run(
            ["git", "config", "--local", "--unset", key],
            cwd=worktree_path,
            check=False,
            text=True,
        )
        if result.returncode == 5:
            # Multi-value key: --unset fails with rc 5, use --unset-all
            result = subprocess.run(
                ["git", "config", "--local", "--unset-all", key],
                cwd=worktree_path,
                check=False,
                text=True,
            )
        if result.returncode == 0:
            removed += 1

    if removed:
        console.print(f"[green]✓ Removed {removed} local git config key(s)[/green]")
    else:
        console.print("[green]✓ No local git config to clean up[/green]")
