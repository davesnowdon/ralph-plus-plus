"""Post-run review and fix loop."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from ..config import Config, ReviewConfig
from ..tools import make_tool

console = Console()


def post_review_loop(worktree_path: Path, config: Config) -> None:
    """
    After the Ralph sandbox completes, run a full review of the implementation
    against prd.json. Iteratively fix issues until LGTM or max_cycles reached.
    """
    review_cfg: ReviewConfig = config.post_review
    if not review_cfg.enabled:
        console.print("[dim]Post-run review disabled — skipping[/dim]")
        return

    console.print("[bold cyan]\n── Step: Post-Run Review Loop ──[/bold cyan]")
    reviewer = make_tool(review_cfg.reviewer, config)
    fixer = make_tool(review_cfg.fixer, config)

    prd_json = worktree_path / "scripts" / "ralph" / "prd.json"

    for cycle in range(1, review_cfg.max_cycles + 1):
        console.print(f"[bold]Post-run review cycle {cycle}/{review_cfg.max_cycles}[/bold]")

        review_prompt = review_cfg.reviewer_prompt.replace("{prd_file}", str(prd_json))
        result = reviewer.run(prompt=review_prompt, cwd=worktree_path)
        if not result.success:
            raise RuntimeError(
                f"Post-run reviewer failed (exit {result.exit_code}): {result.output[:200]}"
            )

        if result.is_lgtm:
            console.print("[green]✓ Post-run review passed (LGTM)[/green]")
            return

        console.print(f"[yellow]Issues found in cycle {cycle} — running fix pass...[/yellow]")
        fix_prompt = review_cfg.fixer_prompt.replace("{prd_file}", str(prd_json)).replace(
            "{findings}", result.output
        )
        fix_result = fixer.run(prompt=fix_prompt, cwd=worktree_path)
        if not fix_result.success:
            raise RuntimeError(
                f"Post-run fixer failed (exit {fix_result.exit_code}): {fix_result.output[:200]}"
            )

    console.print(
        f"[yellow]⚠ Post-run review: max cycles ({review_cfg.max_cycles}) "
        "reached — continuing[/yellow]"
    )
