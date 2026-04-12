"""CLI entry point for ralph++."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape

from .config import (
    discover_config_files,
    format_effective_config,
    load_config,
    load_config_with_provenance,
    parse_mode,
)
from .orchestrator import Orchestrator

console = Console()


# ── Click group with default-to-run behaviour ────────────────────────


class _DefaultGroup(click.Group):
    """A click group that falls through to the ``run`` subcommand when the
    first argument is not a recognised subcommand."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # If there are args and the first one is not a known subcommand,
        # implicitly prepend "run" so `ralph++ --feature "…"` still works.
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            args = ["run"] + args
        return super().parse_args(ctx, args)


@click.group(cls=_DefaultGroup)
def main() -> None:
    """ralph++ – automate the full Ralph agentic coding workflow."""


# ── Shared helpers ────────────────────────────────────────────────────


def _resolve_config(
    config_file: Path | None,
    repo: Path | None,
) -> tuple[list[Path], Path | None]:
    """Discover config file paths and resolve repo."""
    if config_file is not None:
        if not config_file.exists():
            raise click.BadParameter(
                f"Config file not found: {config_file}",
                param_hint="'--config'",
            )
        config_paths = [config_file]
    else:
        config_paths = discover_config_files(repo_path=repo)

    for cp in config_paths:
        console.print(f"[dim]Using config: {escape(str(cp))}[/dim]")

    return config_paths, repo


def _build_overrides(
    repo: Path | None,
    claude_config: Path | None,
    codex_config: Path | None,
    sandbox_dir: Path | None,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if repo:
        overrides["repo_path"] = repo
    if claude_config:
        overrides["claude_config_dir"] = claude_config
    if codex_config:
        overrides["codex_config_dir"] = codex_config
    if sandbox_dir:
        overrides["ralph"] = {"sandbox_dir": str(sandbox_dir)}
    return overrides


# ── Common options ────────────────────────────────────────────────────

_repo_option = click.option(
    "--repo",
    "-r",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Path to the git repository. Defaults to current directory.",
)
_config_option = click.option(
    "--config",
    "-c",
    "config_file",
    type=click.Path(exists=False, dir_okay=False, path_type=Path),
    default=None,
    help="Path to ralph++.yaml config file.",
)
_sandbox_dir_option = click.option(
    "--sandbox-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Path to ralph-sandbox checkout directory.",
)


# ── run subcommand (default) ─────────────────────────────────────────


@main.command()
@click.option(
    "--feature",
    "-f",
    required=False,
    default=None,
    help="Feature description (used to name the branch and generate the PRD). "
    "Derived from --prd-file filename when omitted.",
)
@_repo_option
@_config_option
@click.option(
    "--claude-config",
    type=click.Path(exists=False, path_type=Path),
    default=None,
    help="Path to Claude config directory (default: ~/.claude).",
)
@click.option(
    "--codex-config",
    type=click.Path(exists=False, path_type=Path),
    default=None,
    help="Path to Codex config directory (default: ~/.codex).",
)
@click.option(
    "--max-iters",
    type=int,
    default=None,
    help="Maximum Ralph iterations (overrides config).",
)
@click.option(
    "--skip-prd-review",
    is_flag=True,
    default=False,
    help="Skip the PRD review loop.",
)
@click.option(
    "--skip-post-review",
    is_flag=True,
    default=False,
    help="Skip the post-run review loop.",
)
@click.option(
    "--mode",
    "-m",
    type=click.Choice(["delegated", "orchestrated"], case_sensitive=False),
    default=None,
    help="Workflow mode: 'delegated' (default Ralph loop) "
    "or 'orchestrated' (ralph++ controls each iteration).",
)
@_sandbox_dir_option
@click.option(
    "--setup-cmd",
    multiple=True,
    help="Shell command to run in the worktree after creation "
    "(repeatable; prepended to post_worktree_create hooks).",
)
@click.option(
    "--prd-only",
    is_flag=True,
    default=False,
    help="Generate and review the text PRD, then stop. No worktree or implementation.",
)
@click.option(
    "--prd-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to an existing text PRD. Skips generation/review, proceeds to implementation.",
)
@click.option(
    "--prd-prompt",
    default=None,
    help="Prompt text for PRD generation (can be longer/richer than --feature). "
    "When provided, --feature is used only for naming branches and worktrees.",
)
@click.option(
    "--manual-prd",
    is_flag=True,
    default=False,
    help="Open an interactive Claude session for PRD generation without "
    "auto-prompting the feature description. Use the /prd skill manually.",
)
@click.option(
    "--resume-worktree",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Resume a crashed run from an existing worktree. "
    "Skips worktree creation and PRD generation.",
)
@click.option(
    "--story",
    "story_filter",
    multiple=True,
    help="Run only specific stories by ID (e.g. --story US-003 --story US-004). "
    "Repeatable. When set, other stories are treated as already complete.",
)
@click.option(
    "--design-implementation-scope",
    type=click.Choice(["unspecified", "single_pass", "incremental"]),
    default=None,
    help="(#121) Design constraint: implementation scope. 'single_pass' tells "
    "the PRD generator to avoid transitional wrappers; 'incremental' allows them.",
)
@click.option(
    "--design-backward-compatibility",
    type=click.Choice(["unspecified", "required", "not_required"]),
    default=None,
    help="(#121) Design constraint: must new code interoperate with data produced by old code?",
)
@click.option(
    "--design-existing-tests",
    type=click.Choice(["unspecified", "must_pass", "can_update"]),
    default=None,
    help="(#121) Design constraint: must existing tests pass without modification?",
)
@click.option(
    "--design-api-stability",
    type=click.Choice(["unspecified", "extend_only", "can_break"]),
    default=None,
    help="(#121) Design constraint: can the public API change in breaking ways?",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    default=False,
    help="Never prompt on stdin. When a review gate reaches max cycles without "
    "LGTM, apply the configured non_interactive.on_max_cycles_* policy "
    "(default: continue) instead of asking. Also honored automatically when "
    "stdin is not a TTY or RALPH_NON_INTERACTIVE=1 is set.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would be done without executing anything.",
)
def run(
    feature: str | None,
    repo: Path | None,
    config_file: Path | None,
    claude_config: Path | None,
    codex_config: Path | None,
    max_iters: int | None,
    mode: str | None,
    skip_prd_review: bool,
    skip_post_review: bool,
    setup_cmd: tuple[str, ...],
    sandbox_dir: Path | None,
    prd_only: bool,
    prd_file: Path | None,
    prd_prompt: str | None,
    manual_prd: bool,
    resume_worktree: Path | None,
    story_filter: tuple[str, ...],
    design_implementation_scope: str | None,
    design_backward_compatibility: str | None,
    design_existing_tests: str | None,
    design_api_stability: str | None,
    non_interactive: bool,
    dry_run: bool,
) -> None:
    """Run the full Ralph agentic coding workflow."""
    if prd_only and prd_file:
        raise click.UsageError("--prd-only and --prd-file are mutually exclusive.")
    if resume_worktree and (prd_only or prd_file):
        raise click.UsageError(
            "--resume-worktree cannot be combined with --prd-only or --prd-file."
        )

    # Derive feature from PRD filename when not explicitly provided.
    if feature is None and prd_file is not None:
        stem = prd_file.stem  # e.g. "prd-my-feature"
        feature = stem.removeprefix("prd-") if stem.startswith("prd-") else stem
    # For resume, derive feature from the worktree directory name
    if feature is None and resume_worktree is not None:
        feature = resume_worktree.name
    if feature is None:
        raise click.UsageError("--feature is required (or provide --prd-file to derive it).")

    config_paths, repo = _resolve_config(config_file, repo)
    overrides = _build_overrides(repo, claude_config, codex_config, sandbox_dir)

    cfg = load_config(config_paths, overrides)

    # Apply CLI overrides that need post-load handling
    if max_iters is not None:
        cfg.ralph.max_iterations = max_iters
    if mode is not None:
        cfg.ralph.mode = parse_mode(mode)
    if setup_cmd:
        existing = cfg.hooks.get("post_worktree_create", [])
        cfg.hooks["post_worktree_create"] = list(setup_cmd) + existing
    if story_filter:
        cfg.orchestrated.story_filter = list(story_filter)
    # #121: CLI design-stance overrides win over config-file values.
    if design_implementation_scope:
        cfg.design_stance.implementation_scope = design_implementation_scope  # type: ignore[assignment]
    if design_backward_compatibility:
        cfg.design_stance.backward_compatibility = design_backward_compatibility  # type: ignore[assignment]
    if design_existing_tests:
        cfg.design_stance.existing_tests = design_existing_tests  # type: ignore[assignment]
    if design_api_stability:
        cfg.design_stance.api_stability = design_api_stability  # type: ignore[assignment]
    if non_interactive:
        cfg.non_interactive.enabled = True

    orchestrator = Orchestrator(
        feature=feature, config=cfg, dry_run=dry_run, resume_worktree=resume_worktree
    )
    orchestrator.run(
        skip_prd_review=skip_prd_review,
        skip_post_review=skip_post_review,
        prd_only=prd_only,
        prd_file=prd_file,
        prd_prompt=prd_prompt,
        manual_prd=manual_prd,
    )


# ── config subcommand ─────────────────────────────────────────────────


@main.command(name="config")
@_repo_option
@_config_option
@_sandbox_dir_option
@click.option(
    "--show-sources",
    is_flag=True,
    default=False,
    help="Show which config layer set each value.",
)
def show_config(
    repo: Path | None,
    config_file: Path | None,
    sandbox_dir: Path | None,
    show_sources: bool,
) -> None:
    """Print the effective merged config."""
    config_paths, repo = _resolve_config(config_file, repo)
    overrides = _build_overrides(repo, None, None, sandbox_dir)
    if show_sources:
        cfg, provenance = load_config_with_provenance(config_paths, overrides)
        click.echo(provenance.format(cfg))
    else:
        cfg = load_config(config_paths, overrides)
        click.echo(format_effective_config(cfg))


# ── worktrees subcommand group ────────────────────────────────────────


@main.group()
def worktrees() -> None:
    """Manage ralph++ git worktrees."""


@dataclass
class WorktreeInfo:
    """Metadata about a ralph++ git worktree (#95, #99)."""

    path: str
    branch: str
    dirty: bool  # True when git status --porcelain shows uncommitted changes
    last_commit_age_seconds: int | None  # None when the worktree dir is missing


def _find_ralph_worktrees(repo_path: Path) -> list[WorktreeInfo]:
    """Return :class:`WorktreeInfo` for all ralph++ worktrees in *repo_path*."""
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_path,
            text=True,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise click.UsageError(
            f"Not a git repository or git is not available: {repo_path}\n"
            f"  {exc.stderr.strip() if exc.stderr else exc}"
        ) from exc

    raw_pairs: list[tuple[str, str]] = []
    current_path = ""
    current_branch = ""
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line.split(" ", 1)[1]
        elif line.startswith("branch "):
            current_branch = line.split(" ", 1)[1].removeprefix("refs/heads/")
        elif line == "":
            if current_branch.startswith("ralph/"):
                raw_pairs.append((current_path, current_branch))
            current_path = ""
            current_branch = ""
    if current_branch.startswith("ralph/"):
        raw_pairs.append((current_path, current_branch))

    return [_inspect_worktree(path, branch) for path, branch in raw_pairs]


def _inspect_worktree(path: str, branch: str) -> WorktreeInfo:
    """Probe a worktree directory for dirty state and last-commit age."""
    wt_path = Path(path)
    dirty = False
    age_seconds: int | None = None
    if wt_path.is_dir():
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=wt_path,
                text=True,
                capture_output=True,
                check=False,
            )
            dirty = bool(status.stdout.strip())
        except (subprocess.SubprocessError, OSError):
            dirty = False
        try:
            age = subprocess.run(
                ["git", "log", "-1", "--format=%ct"],
                cwd=wt_path,
                text=True,
                capture_output=True,
                check=False,
            )
            if age.returncode == 0 and age.stdout.strip():
                import time as _time

                committer_ts = int(age.stdout.strip())
                age_seconds = max(0, int(_time.time()) - committer_ts)
        except (subprocess.SubprocessError, OSError, ValueError):
            age_seconds = None
    return WorktreeInfo(path=path, branch=branch, dirty=dirty, last_commit_age_seconds=age_seconds)


_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
_DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 86400 * 7,
}


def _parse_duration(value: str) -> int:
    """Parse a duration like '7d', '24h', '30m', '90s' to seconds.

    Raises ``click.BadParameter`` on invalid input. Used by
    ``worktrees clean --older-than`` (#100).
    """
    if not value:
        raise click.BadParameter("duration must not be empty")
    match = _DURATION_RE.match(value.strip())
    if not match:
        raise click.BadParameter(
            f"invalid duration {value!r}; expected forms like '7d', '24h', '30m', '90s'"
        )
    n, unit = match.groups()
    return int(n) * _DURATION_UNITS[unit.lower()]


def _format_age(seconds: int | None) -> str:
    """Render an age in seconds as a compact 'Xd ago' / 'Xh ago' string."""
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 86400 * 14:
        return f"{seconds // 86400}d ago"
    return f"{seconds // (86400 * 7)}w ago"


@worktrees.command(name="list")
@_repo_option
def worktrees_list(repo: Path | None) -> None:
    """List all ralph++ worktrees for this repo with dirty status and age."""
    entries = _find_ralph_worktrees((repo or Path.cwd()).resolve())

    if not entries:
        console.print("[dim]No ralph++ worktrees found.[/dim]")
        return

    for info in entries:
        # Escape the literal brackets so Rich renders "[dirty]" as text,
        # not as an unknown markup tag.
        dirty_marker = "  [yellow]\\[dirty][/yellow]" if info.dirty else ""
        age_str = _format_age(info.last_commit_age_seconds)
        console.print(f"  {escape(info.branch)}  →  {escape(info.path)}  ({age_str}){dirty_marker}")
    console.print(f"\n[dim]{len(entries)} worktree(s)[/dim]")


def _filter_worktrees_for_clean(
    entries: list[WorktreeInfo],
    *,
    older_than: int | None,
    branch_pattern: str | None,
) -> list[WorktreeInfo]:
    """Apply ``--older-than`` and ``--branch`` filters (#100)."""
    result: list[WorktreeInfo] = []
    for info in entries:
        if branch_pattern and not fnmatch.fnmatch(info.branch, branch_pattern):
            continue
        if older_than is not None:
            if info.last_commit_age_seconds is None:
                # Unknown age — skip rather than risk removing fresh work.
                continue
            if info.last_commit_age_seconds < older_than:
                continue
        result.append(info)
    return result


@worktrees.command(name="clean")
@_repo_option
@click.option("--force", is_flag=True, default=False, help="Force removal even if dirty.")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would be removed/skipped without making changes (#98).",
)
@click.option(
    "--older-than",
    "older_than_str",
    default=None,
    help="Only remove worktrees whose last commit is older than this duration "
    "(e.g. '7d', '24h', '30m', '90s'). #100",
)
@click.option(
    "--branch",
    "branch_pattern",
    default=None,
    help="Only remove worktrees whose branch matches this fnmatch glob "
    "(e.g. 'ralph/implement-*'). #100",
)
@click.option(
    "--keep-branches",
    is_flag=True,
    default=False,
    help="Do not delete the underlying ralph/* branches after removing the "
    "worktrees. By default branches are deleted. #97",
)
@click.option(
    "--yes",
    "skip_confirmation",
    is_flag=True,
    default=False,
    help="Skip the interactive confirmation prompt. Required for --dry-run "
    "and selective filters in unattended runs.",
)
def worktrees_clean(
    repo: Path | None,
    force: bool,
    dry_run: bool,
    older_than_str: str | None,
    branch_pattern: str | None,
    keep_branches: bool,
    skip_confirmation: bool,
) -> None:
    """Remove ralph++ worktrees (and by default their branches).

    With no filters this removes all ralph++ worktrees, matching the
    legacy behavior. Use --older-than, --branch, and --dry-run to scope
    or preview the operation. (#95, #97, #98, #99, #100)
    """
    repo_path = (repo or Path.cwd()).resolve()
    older_than = _parse_duration(older_than_str) if older_than_str else None

    entries = _find_ralph_worktrees(repo_path)
    if not entries:
        console.print("[dim]No ralph++ worktrees to clean.[/dim]")
        return

    candidates = _filter_worktrees_for_clean(
        entries, older_than=older_than, branch_pattern=branch_pattern
    )
    if not candidates:
        console.print("[dim]No ralph++ worktrees match the given filters.[/dim]")
        return

    # Confirmation: bypass for --dry-run, --yes, or when filters are in use
    # (the user has already narrowed scope explicitly).
    if (
        not dry_run
        and not skip_confirmation
        and not click.confirm(f"Remove {len(candidates)} ralph++ worktree(s)?", default=False)
    ):
        console.print("[dim]Aborted.[/dim]")
        return

    removed = 0
    skipped = 0
    failed = 0
    for info in candidates:
        if info.dirty and not force:
            console.print(
                f"[yellow]Skip (dirty):[/yellow] {escape(info.path)} ({escape(info.branch)})"
            )
            skipped += 1
            continue

        if dry_run:
            console.print(f"[cyan]Would remove:[/cyan] {escape(info.path)} ({escape(info.branch)})")
            removed += 1
            continue

        console.print(f"[yellow]Removing:[/yellow] {escape(info.path)} ({escape(info.branch)})")
        force_flag = ["--force"] if force else []
        wt_result = subprocess.run(
            ["git", "worktree", "remove", *force_flag, info.path],
            cwd=repo_path,
            check=False,
            capture_output=True,
            text=True,
        )
        if wt_result.returncode != 0:
            failed += 1
            msg = wt_result.stderr.strip() or f"exit code {wt_result.returncode}"
            # Git stderr can contain bracketed text — escape it for Rich (#125).
            console.print(f"[red]  ✗ Failed to remove worktree: {escape(msg)}[/red]")
            continue
        if not keep_branches:
            subprocess.run(
                ["git", "branch", "-D", info.branch],
                cwd=repo_path,
                check=False,
                capture_output=True,
            )
        removed += 1

    if dry_run:
        console.print(
            f"\n[cyan]Dry run:[/cyan] would remove {removed}, "
            f"skip {skipped} (use --force to include dirty worktrees)"
        )
    else:
        if removed:
            verb = "Removed"
            console.print(f"[green]✓ {verb} {removed} worktree(s)[/green]")
        if skipped:
            console.print(
                f"[yellow]Skipped {skipped} dirty worktree(s) "
                "(use --force to include them)[/yellow]"
            )
    if failed:
        console.print(f"[red]✗ Failed to remove {failed} worktree(s)[/red]")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
