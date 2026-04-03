"""Shared git helpers for step modules."""

from __future__ import annotations

import subprocess
from pathlib import Path


def get_head_sha(worktree_path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git rev-parse HEAD failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def get_diff(worktree_path: Path, from_sha: str) -> str:
    result = subprocess.run(
        ["git", "diff", from_sha],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed (exit {result.returncode}): {result.stderr.strip()}")
    return result.stdout or "(no diff)"


def commit_if_dirty(worktree_path: Path, message: str) -> bool:
    """Stage and commit all changes if the working tree has uncommitted work.

    Returns True if a commit was created, False if tree was clean.
    """
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        raise RuntimeError(f"git status failed: {status.stderr.strip()}")
    if not status.stdout.strip():
        return False
    add = subprocess.run(
        ["git", "add", "-A"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        raise RuntimeError(f"git add failed: {add.stderr.strip()}")
    commit = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"git commit failed: {commit.stderr.strip()}")
    return True


def format_test_results(test_output: str, passed: bool) -> str:
    """Format pre-run test output into a block for reviewer prompts."""
    status_str = "PASSED" if passed else "FAILED"
    return (
        f"\nThe following test/CI results were obtained before this review "
        f"({status_str}):\n\n{test_output}\n\n"
        "Use these results as a starting point. You may re-run the configured "
        "CI commands if you need to verify specific fixes, but do NOT run bare "
        "pytest or other tools.\n"
    )


def run_test_commands_with_output(worktree_path: Path, commands: list[str]) -> tuple[bool, str]:
    """Run test/lint commands, capturing output.

    Returns ``(all_passed, combined_output)``.
    """
    output_parts: list[str] = []
    for cmd in commands:
        output_parts.append(f"$ {cmd}")
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        combined = (result.stdout or "") + (result.stderr or "")
        output_parts.append(combined)
        if result.returncode != 0:
            output_parts.append(f"✗ Command failed (exit {result.returncode})")
            return False, "\n".join(output_parts)
    return True, "\n".join(output_parts)
