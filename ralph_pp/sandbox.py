"""Sandbox discovery and validation for ralph++."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import Config


def _is_sandbox_root(path: Path) -> bool:
    """Check whether *path* looks like a ralph-sandbox checkout.

    Requires both ``bin/ralph-sandbox`` (the wrapper script) and
    ``docker-compose.yml`` (the container definition) to exist.  This
    prevents false positives when ``ralph-sandbox`` is a standalone
    executable installed in e.g. ``/usr/local/bin``.
    """
    return (path / "bin" / "ralph-sandbox").is_file() and (path / "docker-compose.yml").is_file()


def _check_sandbox(path: Path) -> None:
    """Verify that *path* is a valid ralph-sandbox checkout root."""
    if not _is_sandbox_root(path):
        raise FileNotFoundError(
            f"ralph-sandbox checkout not found at {path}. "
            f"Expected bin/ralph-sandbox and docker-compose.yml to exist."
        )


def resolve_sandbox_dir(config: Config) -> Path:
    """Resolve the ralph-sandbox checkout directory.

    Resolution order:
    1. ``config.ralph.sandbox_dir`` (explicit config / CLI)
    2. ``RALPH_SANDBOX_DIR`` environment variable
    3. ``ralph-sandbox`` on ``PATH`` (via ``shutil.which``)
    4. Sibling checkout relative to ``config.repo_path``
    """
    # 1. Explicit config value
    if config.ralph.sandbox_dir:
        resolved = Path(config.ralph.sandbox_dir).expanduser().resolve()
        _check_sandbox(resolved)
        return resolved

    # 2. Environment variable
    env_dir = os.environ.get("RALPH_SANDBOX_DIR")
    if env_dir:
        resolved = Path(env_dir).expanduser().resolve()
        _check_sandbox(resolved)
        return resolved

    # 3. PATH lookup — only if the found executable lives inside a real checkout
    which_result = shutil.which("ralph-sandbox")
    if which_result:
        # In a checkout layout the wrapper is at <sandbox_root>/bin/ralph-sandbox
        candidate = Path(which_result).resolve().parent.parent
        if _is_sandbox_root(candidate):
            return candidate

    # 4. Sibling checkout (dev superproject layout)
    sibling = (config.repo_path / ".." / "ralph-sandbox").resolve()
    if _is_sandbox_root(sibling):
        return sibling

    raise FileNotFoundError(
        "Could not find ralph-sandbox. Set one of:\n"
        "  - ralph.sandbox_dir in config\n"
        "  - RALPH_SANDBOX_DIR environment variable\n"
        "  - Add ralph-sandbox/bin to PATH\n"
        "  - Place ralph-sandbox as a sibling of the repo"
    )
