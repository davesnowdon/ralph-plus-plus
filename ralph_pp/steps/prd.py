"""PRD generation, review loop, and prd.json conversion."""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

from ..config import Config, ReviewConfig
from ..tools import make_tool

console = Console()


def generate_prd(feature: str, worktree_path: Path, config: Config) -> Path:
    """
    Invoke the Claude /prd skill to generate a text PRD.
    Returns the path to the generated PRD markdown file.
    """
    console.print("[bold cyan]\n── Step: Generate PRD ──[/bold cyan]")
    tool = make_tool("claude", config)
    prompt = (
        f"Create a PRD for the following feature: {feature}\n\n"
        "Use the /prd skill to generate a detailed Product Requirements Document. "
        "Save the output to tasks/prd.md"
    )
    result = tool.run(prompt=prompt, cwd=worktree_path)
    if not result.success:
        raise RuntimeError(f"PRD generation failed (exit {result.exit_code})")

    prd_file = worktree_path / "tasks" / "prd.md"
    if not prd_file.exists():
        raise RuntimeError(
            f"PRD generation succeeded (exit 0) but {prd_file} was not created"
        )
    console.print(f"[green]✓ PRD generated:[/green] {prd_file}")
    return prd_file


def review_prd_loop(prd_file: Path, worktree_path: Path, config: Config) -> None:
    """
    Iteratively review and fix the text PRD until LGTM or max_cycles reached.
    """
    review_cfg: ReviewConfig = config.prd_review
    if not review_cfg.enabled:
        console.print("[dim]PRD review disabled — skipping[/dim]")
        return

    console.print("[bold cyan]\n── Step: PRD Review Loop ──[/bold cyan]")
    reviewer = make_tool(review_cfg.reviewer, config)
    fixer = make_tool(review_cfg.fixer, config)

    for cycle in range(1, review_cfg.max_cycles + 1):
        console.print(f"[bold]PRD review cycle {cycle}/{review_cfg.max_cycles}[/bold]")

        review_prompt = review_cfg.reviewer_prompt.replace("{prd_file}", str(prd_file))
        result = reviewer.run(prompt=review_prompt, cwd=worktree_path)
        if not result.success:
            raise RuntimeError(
                f"PRD reviewer failed (exit {result.exit_code}): {result.output[:200]}"
            )

        if result.is_lgtm:
            console.print("[green]✓ PRD review passed (LGTM)[/green]")
            return

        console.print(f"[yellow]Issues found in cycle {cycle} — running fix pass...[/yellow]")
        fix_prompt = (
            review_cfg.fixer_prompt
            .replace("{prd_file}", str(prd_file))
            .replace("{findings}", result.output)
        )
        fix_result = fixer.run(prompt=fix_prompt, cwd=worktree_path)
        if not fix_result.success:
            raise RuntimeError(
                f"PRD fixer failed (exit {fix_result.exit_code}): {fix_result.output[:200]}"
            )

    console.print(
        f"[yellow]⚠ PRD review: max cycles ({review_cfg.max_cycles}) reached — continuing[/yellow]"
    )


def convert_prd_to_json(prd_file: Path, worktree_path: Path, config: Config) -> Path:
    """
    Invoke the Claude /ralph skill to convert the text PRD to prd.json.
    Returns the path to prd.json.
    """
    console.print("[bold cyan]\n── Step: Convert PRD to prd.json ──[/bold cyan]")
    tool = make_tool("claude", config)
    prompt = (
        f"Convert the PRD at {prd_file} to prd.json format using the /ralph skill. "
        "Save the output to scripts/ralph/prd.json"
    )
    result = tool.run(prompt=prompt, cwd=worktree_path)
    if not result.success:
        raise RuntimeError(f"PRD conversion failed (exit {result.exit_code})")

    prd_json = worktree_path / "scripts" / "ralph" / "prd.json"
    if not prd_json.exists():
        raise RuntimeError(
            f"PRD conversion succeeded (exit 0) but {prd_json} was not created"
        )
    try:
        json.loads(prd_json.read_text())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"prd.json is not valid JSON: {e}") from e
    console.print(f"[green]✓ prd.json generated:[/green] {prd_json}")
    return prd_json
