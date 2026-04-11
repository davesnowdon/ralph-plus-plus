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


def _resolve_worktree_base(config: Config) -> Path:
    """Return the directory under which new worktrees will be created.

    When ``config.worktree_root`` is unset, fall back to ``repo_path.parent``
    (the original back-compatible behavior). Relative ``worktree_root``
    values are resolved against ``config.repo_path`` — *not* CWD — so a
    checked-in ``.ralph/ralph++.yaml`` can use paths like ``../worktrees``
    without hardcoding environment-specific locations.
    """
    root = config.worktree_root
    if root is None:
        return config.repo_path.parent
    if not root.is_absolute():
        root = config.repo_path / root
    return root.resolve()


def create_worktree(feature: str, config: Config) -> tuple[Path, str]:
    """
    Create a git worktree for the feature.

    Returns:
        (worktree_path, branch_name)
    """
    # #151 / #153: honor config.worktree_root if set; otherwise fall back to
    # sibling-of-repo for back-compat. Relative worktree_root values resolve
    # against repo_path so a checked-in .ralph/ralph++.yaml can use
    # "../worktrees" without hardcoding environment-specific paths. mkdir
    # is a no-op when the parent already exists.
    base = _resolve_worktree_base(config)
    base.mkdir(parents=True, exist_ok=True)

    # Try up to a few times in case the generated path already exists
    # (e.g. from a previous failed run with the same random suffix).
    branch = ""
    worktree_path = Path()
    for _attempt in range(5):
        branch = make_branch_name(feature, config)
        worktree_path = base / branch.replace("/", "-")
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


# Internal orchestration files that should not appear in the final commit.
_ORCHESTRATION_ARTIFACTS = [
    "scripts/ralph/.base-sha",
    "scripts/ralph/.fix-prompt.md",
]


def cleanup_orchestration_artifacts(worktree_path: Path) -> None:
    """Remove internal orchestration files from the worktree and stage the deletions."""
    removed = 0
    for rel_path in _ORCHESTRATION_ARTIFACTS:
        artifact = worktree_path / rel_path
        if artifact.exists():
            artifact.unlink()
            removed += 1

    if removed:
        # Stage the deletions so the next commit (or amend) picks them up
        subprocess.run(
            ["git", "add", "-A", "scripts/ralph/"],
            cwd=worktree_path,
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "ralph: remove orchestration artifacts"],
            cwd=worktree_path,
            check=False,
            capture_output=True,
        )
        console.print(f"[green]✓ Removed {removed} orchestration artifact(s)[/green]")
    else:
        console.print("[green]✓ No orchestration artifacts to clean up[/green]")
