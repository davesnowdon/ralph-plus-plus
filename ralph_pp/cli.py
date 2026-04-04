"""CLI entry point for ralph++."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
from rich.console import Console

from .config import (
    discover_config_files,
    format_effective_config,
    load_config,
    load_config_with_provenance,
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
        if args and args[0] not in self.commands:
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
        console.print(f"[dim]Using config: {cp}[/dim]")

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
    "--manual-prd",
    is_flag=True,
    default=False,
    help="Open an interactive Claude session for PRD generation without "
    "auto-prompting the feature description. Use the /prd skill manually.",
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
    manual_prd: bool,
    dry_run: bool,
) -> None:
    """Run the full Ralph agentic coding workflow."""
    if prd_only and prd_file:
        raise click.UsageError("--prd-only and --prd-file are mutually exclusive.")

    # Derive feature from PRD filename when not explicitly provided.
    if feature is None and prd_file is not None:
        stem = prd_file.stem  # e.g. "prd-my-feature"
        feature = stem.removeprefix("prd-") if stem.startswith("prd-") else stem
    if feature is None:
        raise click.UsageError("--feature is required (or provide --prd-file to derive it).")

    config_paths, repo = _resolve_config(config_file, repo)
    overrides = _build_overrides(repo, claude_config, codex_config, sandbox_dir)

    cfg = load_config(config_paths, overrides)

    # Apply CLI overrides that need post-load handling
    if max_iters is not None:
        cfg.ralph.max_iterations = max_iters
    if mode is not None:
        cfg.ralph.mode = mode
    if setup_cmd:
        existing = cfg.hooks.get("post_worktree_create", [])
        cfg.hooks["post_worktree_create"] = list(setup_cmd) + existing

    orchestrator = Orchestrator(feature=feature, config=cfg, dry_run=dry_run)
    orchestrator.run(
        skip_prd_review=skip_prd_review,
        skip_post_review=skip_post_review,
        prd_only=prd_only,
        prd_file=prd_file,
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


@worktrees.command(name="list")
@_repo_option
def worktrees_list(repo: Path | None) -> None:
    """List all ralph++ worktrees for this repo."""
    import subprocess

    repo_path = (repo or Path.cwd()).resolve()
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=True,
    )

    worktree_entries: list[tuple[str, str]] = []
    current_path = ""
    current_branch = ""
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line.split(" ", 1)[1]
        elif line.startswith("branch "):
            current_branch = line.split(" ", 1)[1]
            # refs/heads/ralph/... → ralph/...
            current_branch = current_branch.removeprefix("refs/heads/")
        elif line == "":
            if current_branch.startswith("ralph/"):
                worktree_entries.append((current_path, current_branch))
            current_path = ""
            current_branch = ""
    # Handle last entry
    if current_branch.startswith("ralph/"):
        worktree_entries.append((current_path, current_branch))

    if not worktree_entries:
        console.print("[dim]No ralph++ worktrees found.[/dim]")
        return

    for path, branch in worktree_entries:
        console.print(f"  {branch}  →  {path}")
    console.print(f"\n[dim]{len(worktree_entries)} worktree(s)[/dim]")


@worktrees.command(name="clean")
@_repo_option
@click.option("--force", is_flag=True, default=False, help="Force removal even if dirty.")
@click.confirmation_option(prompt="Remove all ralph++ worktrees?")
def worktrees_clean(repo: Path | None, force: bool) -> None:
    """Remove all ralph++ worktrees and their branches."""
    import subprocess

    repo_path = (repo or Path.cwd()).resolve()
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=True,
    )

    worktree_entries: list[tuple[str, str]] = []
    current_path = ""
    current_branch = ""
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line.split(" ", 1)[1]
        elif line.startswith("branch "):
            current_branch = line.split(" ", 1)[1].removeprefix("refs/heads/")
        elif line == "":
            if current_branch.startswith("ralph/"):
                worktree_entries.append((current_path, current_branch))
            current_path = ""
            current_branch = ""
    if current_branch.startswith("ralph/"):
        worktree_entries.append((current_path, current_branch))

    if not worktree_entries:
        console.print("[dim]No ralph++ worktrees to clean.[/dim]")
        return

    for path, branch in worktree_entries:
        console.print(f"[yellow]Removing:[/yellow] {path} ({branch})")
        force_flag = ["--force"] if force else []
        subprocess.run(
            ["git", "worktree", "remove", *force_flag, path],
            cwd=repo_path,
            check=False,
        )
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=repo_path,
            check=False,
            capture_output=True,
        )

    console.print(f"[green]✓ Removed {len(worktree_entries)} worktree(s)[/green]")


if __name__ == "__main__":
    main()
