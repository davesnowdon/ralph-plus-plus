"""PRD generation, review loop, and prd.json conversion."""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.console import Console

from ..config import Config, PrdReviewConfig
from ..tools import make_tool

console = Console()


class MaxCyclesAbort(SystemExit):
    """Raised when the user chooses to quit after max review cycles."""

    def __init__(self) -> None:
        super().__init__("Review aborted by user after max cycles reached")


def prompt_max_cycles(phase: str, max_cycles: int) -> str:
    """Prompt the user for action when max review cycles are exhausted.

    Returns one of: "quit", "retry", "continue".
    """
    console.print(
        f"\n[yellow]⚠ {phase} review: max cycles ({max_cycles}) reached without LGTM[/yellow]"
    )
    console.print(
        "[bold]Options:[/bold]\n"
        "  [cyan]1)[/cyan] Quit — abort the workflow\n"
        f"  [cyan]2)[/cyan] Retry — run another {max_cycles} review cycles\n"
        "  [cyan]3)[/cyan] Continue — proceed without reviewer approval"
    )
    choice = click.prompt(
        "Choose",
        type=click.Choice(["1", "2", "3"]),
        default="3",
    )
    return {"1": "quit", "2": "retry", "3": "continue"}[choice]


def generate_prd(feature: str, worktree_path: Path, config: Config) -> Path:
    """
    Invoke the Claude /prd skill to generate a text PRD.
    Returns the path to the generated PRD markdown file.
    """
    console.print("[bold cyan]\n── Step: Generate PRD ──[/bold cyan]")
    tool = make_tool(config.prd_tool, config)
    prompt = (
        f"Create a PRD for the following feature: {feature}\n\n"
        "You have the /prd skill available. Use it to generate a detailed Product "
        "Requirements Document with goals, user stories, acceptance criteria, "
        "non-goals, and technical considerations.\n\n"
        "Save the PRD to tasks/prd.md"
    )
    result = tool.run(prompt=prompt, cwd=worktree_path)
    if not result.success:
        raise RuntimeError(f"PRD generation failed (exit {result.exit_code})")

    prd_file = worktree_path / "tasks" / "prd.md"
    if not prd_file.exists():
        raise RuntimeError(f"PRD generation succeeded (exit 0) but {prd_file} was not created")
    console.print(f"[green]✓ PRD generated:[/green] {prd_file}")
    return prd_file


def review_prd_loop(prd_file: Path, worktree_path: Path, config: Config) -> None:
    """
    Iteratively review and fix the text PRD until LGTM or max_cycles reached.

    When *max_cycles* is exhausted without LGTM the user is prompted to choose:
    quit, retry another batch of cycles, or continue anyway.
    """
    review_cfg: PrdReviewConfig = config.prd_review
    if not review_cfg.enabled:
        console.print("[dim]PRD review disabled — skipping[/dim]")
        return

    console.print("[bold cyan]\n── Step: PRD Review Loop ──[/bold cyan]")
    reviewer = make_tool(review_cfg.reviewer, config)
    fixer = make_tool(review_cfg.fixer, config)

    total_cycles = 0
    previous_findings: str = ""
    while True:
        for cycle in range(1, review_cfg.max_cycles + 1):
            total_cycles += 1
            console.print(
                f"[bold]PRD review cycle {total_cycles} "
                f"({cycle}/{review_cfg.max_cycles} this batch)[/bold]"
            )

            if previous_findings:
                context = (
                    "\nThe previous review cycle found these issues (which have since "
                    "been addressed by a fix pass). Focus on whether the fixes are "
                    "adequate and whether any NEW issues remain. Do not re-raise issues "
                    "that have been resolved:\n\n"
                    f"{previous_findings}\n"
                )
            else:
                context = ""

            review_prompt = review_cfg.reviewer_prompt.replace("{prd_file}", str(prd_file)).replace(
                "{previous_findings}", context
            )
            result = reviewer.run(prompt=review_prompt, cwd=worktree_path)
            if not result.success:
                raise RuntimeError(
                    f"PRD reviewer failed (exit {result.exit_code}): {result.output[:200]}"
                )

            if result.is_lgtm:
                console.print("[green]✓ PRD review passed (LGTM)[/green]")
                return

            previous_findings = result.output
            console.print(
                f"[yellow]Issues found in cycle {total_cycles} — running fix pass...[/yellow]"
            )
            fix_prompt = review_cfg.fixer_prompt.replace("{prd_file}", str(prd_file)).replace(
                "{findings}", result.output
            )
            fix_result = fixer.run(prompt=fix_prompt, cwd=worktree_path)
            if not fix_result.success:
                raise RuntimeError(
                    f"PRD fixer failed (exit {fix_result.exit_code}): {fix_result.output[:200]}"
                )

        action = prompt_max_cycles("PRD", review_cfg.max_cycles)
        if action == "quit":
            raise MaxCyclesAbort
        if action == "continue":
            console.print("[yellow]Continuing without reviewer approval[/yellow]")
            return
        # action == "retry" → loop again
        console.print(f"[cyan]Retrying another {review_cfg.max_cycles} review cycles...[/cyan]")


def convert_prd_to_json(prd_file: Path, worktree_path: Path, config: Config) -> Path:
    """
    Invoke the Claude /ralph skill to convert the text PRD to prd.json.
    Returns the path to prd.json.
    """
    console.print("[bold cyan]\n── Step: Convert PRD to prd.json ──[/bold cyan]")
    tool = make_tool(config.prd_tool, config)
    prompt = (
        f"Convert the PRD at {prd_file} to structured JSON format.\n\n"
        "You have the /ralph skill available. Use it to convert the PRD into an "
        "executable prd.json with properly sized user stories, dependency ordering, "
        "and verifiable acceptance criteria.\n\n"
        "Save the output to scripts/ralph/prd.json"
    )
    result = tool.run(prompt=prompt, cwd=worktree_path)
    if not result.success:
        raise RuntimeError(f"PRD conversion failed (exit {result.exit_code})")

    prd_json = worktree_path / "scripts" / "ralph" / "prd.json"
    if not prd_json.exists():
        raise RuntimeError(f"PRD conversion succeeded (exit 0) but {prd_json} was not created")
    try:
        json.loads(prd_json.read_text())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"prd.json is not valid JSON: {e}") from e
    console.print(f"[green]✓ prd.json generated:[/green] {prd_json}")
    return prd_json
