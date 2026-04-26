"""Unattended mode support for ralph++.

Unattended mode is the contract used when an external orchestrator drives
``ralph++`` as a subprocess (issue #163). It activates plain output, a
caller-supplied run id, and (in later phases) an atomic JSON result file,
stable exit codes, and graceful signal handling.

This module currently exposes only the Phase 1 surface:

- :func:`is_unattended_env` — read ``RALPH_UNATTENDED`` from the env.
- :func:`generate_run_id` — produce a ULID when the caller did not supply one.
- :func:`reconfigure_consoles_for_unattended` — swap the module-level Rich
  consoles in ``ralph_pp.cli`` and ``ralph_pp.orchestrator`` for instances that
  emit no ANSI escape codes.
"""

from __future__ import annotations

import os

from rich.console import Console
from ulid import ULID

ENV_VAR = "RALPH_UNATTENDED"


def is_unattended_env() -> bool:
    """Return True when ``RALPH_UNATTENDED`` is set to a truthy value."""
    raw = os.environ.get(ENV_VAR, "").strip().lower()
    return bool(raw) and raw not in ("0", "false", "no")


def generate_run_id() -> str:
    """Return a freshly minted ULID as a 26-char Crockford base32 string."""
    return str(ULID())


def make_plain_console() -> Console:
    """Build a Rich :class:`Console` that emits no ANSI escape codes.

    Rich still draws box characters for ``Panel`` and ``Rule``; that is
    acceptable for the MVP whose acceptance criterion is "no ANSI escape
    codes". Tightening to fully unstructured output is deferred.
    """
    return Console(no_color=True, force_terminal=False, highlight=False)


def reconfigure_consoles_for_unattended() -> None:
    """Replace the module-level consoles used across ralph++ with plain ones.

    Called after the CLI has decided unattended mode is active and *before*
    the orchestrator does any printing. Idempotent.
    """
    plain = make_plain_console()

    # Late imports: avoid pulling these in unless unattended mode is active,
    # and keep the dependency direction one-way (these modules may eventually
    # import this one).
    from . import cli, hooks, orchestrator, skills
    from .steps import post_review, prd, sandbox, worktree
    from .tools import cli_tool

    # Each module declares its own ``console = Console()`` at import time;
    # vars(module) rebinds that name without tripping pyright's strict
    # attribute-access check on ModuleType, and without ruff's B010 on setattr.
    for module in (cli, hooks, orchestrator, skills, post_review, prd, sandbox, worktree, cli_tool):
        vars(module)["console"] = plain
