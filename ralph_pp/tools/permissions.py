"""Derive Bash permission patterns from test/CI commands."""

from __future__ import annotations


def bash_permissions_from_commands(commands: list[str]) -> list[str]:
    """Convert test commands to ``Bash(...)`` permission patterns.

    Each command's first token becomes a wildcard prefix, e.g.
    ``"hatch run ci"`` → ``"Bash(hatch:*)"`` and ``"pytest"`` → ``"Bash(pytest:*)"``.

    Duplicates are suppressed (two ``make`` commands produce one ``Bash(make:*)``).
    """
    permissions: list[str] = []
    seen: set[str] = set()
    for cmd in commands:
        parts = cmd.strip().split()
        if not parts:
            continue
        pattern = f"Bash({parts[0]}:*)"
        if pattern not in seen:
            seen.add(pattern)
            permissions.append(pattern)
    return permissions
