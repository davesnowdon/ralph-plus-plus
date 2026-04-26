"""Unattended mode support for ralph++.

Unattended mode is the contract used when an external orchestrator drives
``ralph++`` as a subprocess (issue #163). It activates plain output, a
caller-supplied run id, an atomic JSON result file, and (in later phases)
stable exit codes and graceful signal handling.

The MVP schema and contract are defined in
``docs/unattended-mode-mvp.md``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from rich.console import Console
from ulid import ULID

ENV_VAR = "RALPH_UNATTENDED"
SCHEMA_VERSION = 1

# Stable exit-code table from docs/unattended-mode-mvp.md (FR-4):
#   0  — succeeded
#   1  — generic failure (PRD, sandbox, review, internal)
#   20 — bad input / config error (don't retry)
#   30 — interrupted (signal-driven shutdown; full handling in phase 4)
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 20
EXIT_INTERRUPTED = 30

ResultStatus = Literal["succeeded", "failed", "interrupted"]
ErrorCategory = Literal["config", "environment", "prd", "sandbox", "review", "internal"]

_log = logging.getLogger(__name__)


# ── Environment / identity helpers ───────────────────────────────────────


def is_unattended_env() -> bool:
    """Return True when ``RALPH_UNATTENDED`` is set to a truthy value."""
    raw = os.environ.get(ENV_VAR, "").strip().lower()
    return bool(raw) and raw not in ("0", "false", "no")


def generate_run_id() -> str:
    """Return a freshly minted ULID as a 26-char Crockford base32 string."""
    return str(ULID())


def now_iso8601_ms() -> str:
    """Return the current UTC time as ISO-8601 with millisecond precision."""
    now = datetime.now(UTC)
    # ``isoformat(timespec="milliseconds")`` yields e.g. "2026-04-26T10:31:04.812+00:00"
    # — replace the offset with the canonical "Z" so the schema example matches.
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ── Console reconfiguration ──────────────────────────────────────────────


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


# ── Result schema ────────────────────────────────────────────────────────


@dataclass
class ErrorInfo:
    """Populated on ``RunResult.error`` when the run did not succeed."""

    category: ErrorCategory
    message: str
    retriable: bool


@dataclass
class RunResult:
    """JSON-serialisable run outcome written to disk in unattended mode.

    Field order and shape match the schema in
    ``docs/unattended-mode-mvp.md`` (FR-3).
    """

    schema_version: int
    run_id: str
    status: ResultStatus
    exit_code: int
    started_at: str
    finished_at: str
    duration_seconds: float
    feature: str
    mode: str
    repo: str
    branch: str | None
    worktree: str | None
    iterations: int
    artifacts: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    commits: list[str] = field(default_factory=lambda: list[str]())
    error: ErrorInfo | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the JSON shape exactly as documented in the spec."""
        data = asdict(self)
        # asdict turns ErrorInfo into a dict; preserve null when absent.
        if self.error is None:
            data["error"] = None
        return data


# ── Exception classification ─────────────────────────────────────────────


@dataclass(frozen=True)
class Classification:
    """Result of mapping an exception to the unattended-mode contract."""

    status: ResultStatus
    exit_code: int
    error: ErrorInfo


def classify_error(exc: BaseException) -> Classification:
    """Map *exc* to (status, exit_code, ErrorInfo) per FR-4.

    The classifier is intentionally pattern-based and conservative: any
    exception we don't recognise falls through to ``internal`` / exit 1
    so the orchestrator never silently widens the success set.
    """
    import click as _click

    from .steps.prd import MaxCyclesAbort
    from .steps.sandbox import PrdParseError

    message = str(exc) or exc.__class__.__name__

    # Signal-driven shutdown. Phase 4 wires the actual handlers; this
    # branch covers code paths that translate a signal into KeyboardInterrupt.
    if isinstance(exc, KeyboardInterrupt):
        return Classification(
            status="interrupted",
            exit_code=EXIT_INTERRUPTED,
            error=ErrorInfo(category="internal", message="interrupted", retriable=True),
        )

    # User input / configuration problems — exit 20 so a caller knows not
    # to retry without changing arguments.
    if isinstance(exc, _click.UsageError | _click.BadParameter):
        return Classification(
            status="failed",
            exit_code=EXIT_CONFIG_ERROR,
            error=ErrorInfo(category="config", message=message, retriable=False),
        )
    if isinstance(exc, ValueError):
        # Config validation, severity/mode parsing, primary-vs-linked
        # worktree checks. All user-fixable.
        return Classification(
            status="failed",
            exit_code=EXIT_CONFIG_ERROR,
            error=ErrorInfo(category="config", message=message, retriable=False),
        )
    if isinstance(exc, FileNotFoundError):
        # Missing config / PRD / runner — user-fixable.
        return Classification(
            status="failed",
            exit_code=EXIT_CONFIG_ERROR,
            error=ErrorInfo(category="config", message=message, retriable=False),
        )

    # Review-loop user abort. ``MaxCyclesAbort`` extends ``SystemExit`` so it
    # bypasses Orchestrator's ``except Exception`` block, but the orchestrator's
    # finally still calls into us.
    if isinstance(exc, MaxCyclesAbort):
        return Classification(
            status="failed",
            exit_code=EXIT_FAILURE,
            error=ErrorInfo(category="review", message=message, retriable=False),
        )

    # PRD parsing error — surfaced from sandbox.py but semantically a PRD
    # problem.
    if isinstance(exc, PrdParseError):
        return Classification(
            status="failed",
            exit_code=EXIT_FAILURE,
            error=ErrorInfo(category="prd", message=message, retriable=False),
        )

    # Everything else — most commonly RuntimeError raised from sandbox or
    # subprocess wrappers. Default to a retriable internal failure rather
    # than assuming a category we cannot prove.
    return Classification(
        status="failed",
        exit_code=EXIT_FAILURE,
        error=ErrorInfo(category="internal", message=message, retriable=False),
    )


# ── Atomic result file writing ───────────────────────────────────────────


def write_atomically(path: Path, result: RunResult) -> None:
    """Write *result* as JSON to *path* atomically.

    The body is written to a sibling ``*.tmp`` file, fsynced, then
    ``os.replace``\\ d into the target. Callers therefore never see a
    half-written result. Parent directories are created on demand.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(result.to_dict(), indent=2, sort_keys=False) + "\n"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def resolve_result_path(
    *,
    explicit: Path | None,
    worktree: Path | None,
    run_id: str,
    cwd: Path,
) -> Path:
    """Decide where to write the result file (FR-2).

    Priority:
      1. ``explicit`` (``--result-file``)
      2. ``{worktree}/scripts/ralph/result.json`` when a worktree exists
      3. ``{cwd}/ralph-result-{run_id}.json``
    """
    if explicit is not None:
        return explicit
    if worktree is not None:
        return worktree / "scripts" / "ralph" / "result.json"
    return cwd / f"ralph-result-{run_id}.json"


# ── Commit enumeration ───────────────────────────────────────────────────


def collect_commits(worktree: Path | None, base_sha: str | None) -> list[str]:
    """Return commit SHAs created on the worktree branch since *base_sha*.

    Empty list when either argument is missing or when ``git`` invocation
    fails — the result file must always be writable, so we never raise from
    this helper.
    """
    if worktree is None or not base_sha:
        return []
    try:
        completed = subprocess.run(
            ["git", "log", f"{base_sha}..HEAD", "--format=%H"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _log.debug("collect_commits failed: %s", exc, exc_info=True)
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


# ── Artifact discovery ───────────────────────────────────────────────────


def collect_artifacts(worktree: Path | None) -> dict[str, str]:
    """Return artifact paths relative to *worktree*.

    Currently surfaces ``prd``, ``progress``, and ``base_sha`` — only the
    keys whose backing file actually exists are included (FR-3).
    """
    if worktree is None:
        return {}

    artifacts: dict[str, str] = {}

    # The PRD filename embeds a slugified feature name; locate it by glob.
    tasks_dir = worktree / "tasks"
    if tasks_dir.is_dir():
        prd_candidates = sorted(tasks_dir.glob("prd-*.md"))
        if prd_candidates:
            artifacts["prd"] = str(prd_candidates[0].relative_to(worktree))

    progress_path = worktree / "scripts" / "ralph" / "progress.txt"
    if progress_path.is_file():
        artifacts["progress"] = str(progress_path.relative_to(worktree))

    base_sha_path = worktree / "scripts" / "ralph" / ".base-sha"
    if base_sha_path.is_file():
        artifacts["base_sha"] = str(base_sha_path.relative_to(worktree))

    return artifacts
