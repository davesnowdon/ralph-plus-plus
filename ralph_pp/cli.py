"""CLI entry point for ralph++."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from .config import load_config
from .orchestrator import Orchestrator

console = Console()


@click.command()
@click.option(
    "--feature", "-f",
    required=True,
    help="Feature description (used to name the branch and generate the PRD).",
)
@click.option(
    "--repo", "-r",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Path to the git repository. Defaults to current directory.",
)
@click.option(
    "--config", "-c",
    "config_file",
    type=click.Path(exists=False, dir_okay=False, path_type=Path),
    default=None,
    help="Path to ralph++.yaml config file.",
)
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
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would be done without executing anything.",
)
def main(
    feature: str,
    repo: Path | None,
    config_file: Path | None,
    claude_config: Path | None,
    codex_config: Path | None,
    max_iters: int | None,
    skip_prd_review: bool,
    skip_post_review: bool,
    dry_run: bool,
) -> None:
    """Automate the full Ralph agentic coding workflow."""

    # Search for config file if not specified
    if config_file is None:
        candidates = [
            Path("ralph++.yaml"),
            Path("ralph++.yml"),
            Path.home() / ".ralph++.yaml",
        ]
        for candidate in candidates:
            if candidate.exists():
                config_file = candidate
                console.print(f"[dim]Using config: {config_file}[/dim]")
                break

    # Build overrides from CLI flags
    overrides: dict = {}
    if repo:
        overrides["repo_path"] = repo
    elif repo is None:
        overrides["repo_path"] = Path.cwd()
    if claude_config:
        overrides["claude_config_dir"] = claude_config
    if codex_config:
        overrides["codex_config_dir"] = codex_config

    cfg = load_config(config_file, overrides)

    # Apply CLI overrides that need post-load handling
    if max_iters is not None:
        cfg.ralph.max_iterations = max_iters

    orchestrator = Orchestrator(feature=feature, config=cfg, dry_run=dry_run)
    orchestrator.run(
        skip_prd_review=skip_prd_review,
        skip_post_review=skip_post_review,
    )


if __name__ == "__main__":
    main()
