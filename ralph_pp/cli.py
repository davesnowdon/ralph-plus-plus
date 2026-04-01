"""CLI entry point for ralph++."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
from rich.console import Console

from .config import discover_config_files, format_effective_config, load_config
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
        config_paths = [config_file] if config_file.exists() else []
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
    elif repo is None:
        overrides["repo_path"] = Path.cwd()
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
    required=True,
    help="Feature description (used to name the branch and generate the PRD).",
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
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would be done without executing anything.",
)
def run(
    feature: str,
    repo: Path | None,
    config_file: Path | None,
    claude_config: Path | None,
    codex_config: Path | None,
    max_iters: int | None,
    mode: str | None,
    skip_prd_review: bool,
    skip_post_review: bool,
    sandbox_dir: Path | None,
    dry_run: bool,
) -> None:
    """Run the full Ralph agentic coding workflow."""
    config_paths, repo = _resolve_config(config_file, repo)
    overrides = _build_overrides(repo, claude_config, codex_config, sandbox_dir)

    cfg = load_config(config_paths, overrides)

    # Apply CLI overrides that need post-load handling
    if max_iters is not None:
        cfg.ralph.max_iterations = max_iters
    if mode is not None:
        cfg.ralph.mode = mode

    orchestrator = Orchestrator(feature=feature, config=cfg, dry_run=dry_run)
    orchestrator.run(
        skip_prd_review=skip_prd_review,
        skip_post_review=skip_post_review,
    )


# ── config subcommand ─────────────────────────────────────────────────


@main.command(name="config")
@_repo_option
@_config_option
@_sandbox_dir_option
def show_config(
    repo: Path | None,
    config_file: Path | None,
    sandbox_dir: Path | None,
) -> None:
    """Print the effective merged config."""
    config_paths, repo = _resolve_config(config_file, repo)
    overrides = _build_overrides(repo, None, None, sandbox_dir)
    cfg = load_config(config_paths, overrides)
    click.echo(format_effective_config(cfg))


if __name__ == "__main__":
    main()
