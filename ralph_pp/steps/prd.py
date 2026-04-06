"""PRD generation, review loop, prd.json conversion, and prd.json review."""

from __future__ import annotations

import json
import re
from pathlib import Path

import click
from rich.console import Console

from ..config import Config, PrdReviewConfig
from ..tools import make_tool
from ._git import get_diff, get_head_sha
from ._prompts import render_prompt

console = Console()


class MaxCyclesAbort(SystemExit):
    """Raised when the user chooses to quit after max review cycles."""

    def __init__(self) -> None:
        super().__init__("Review aborted by user after max cycles reached")


def prompt_max_cycles(
    phase: str,
    max_cycles: int,
    continue_label: str = "Continue — proceed without reviewer approval",
) -> str:
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
        f"  [cyan]3)[/cyan] {continue_label}"
    )
    choice = click.prompt(
        "Choose",
        type=click.Choice(["1", "2", "3"]),
        default="3",
    )
    return {"1": "quit", "2": "retry", "3": "continue"}[choice]


def feature_to_slug(feature: str) -> str:
    """Convert a feature description to a kebab-case slug for filenames."""
    slug = feature.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug


def generate_prd(
    feature: str,
    worktree_path: Path,
    config: Config,
    *,
    manual: bool = False,
) -> Path:
    """
    Invoke the Claude /prd skill to generate a text PRD.
    Returns the path to the generated PRD markdown file.

    When *manual* is True the feature description is not sent as a prompt,
    allowing the user to drive the conversation interactively.
    """
    console.print("[bold cyan]\n── Step: Generate PRD ──[/bold cyan]")
    slug = feature_to_slug(feature)
    prd_filename = f"prd-{slug}.md"
    (worktree_path / "tasks").mkdir(exist_ok=True)
    tool = make_tool(config.prd_tool, config)

    if manual:
        prompt = f"Save the PRD to tasks/{prd_filename} when done."
    else:
        prompt = (
            f"Create a PRD for the following feature: {feature}\n\n"
            "You have the /prd skill available. Use it to generate a detailed Product "
            "Requirements Document with goals, user stories, acceptance criteria, "
            "non-goals, and technical considerations.\n\n"
            f"Save the PRD to tasks/{prd_filename}"
        )

    tool_cfg = config.get_tool(config.prd_tool)
    if tool_cfg.interactive:
        if manual:
            console.print(
                "\n[bold yellow]You are in an interactive Claude session.\n"
                f"Use the /prd skill to generate the PRD, then save it to tasks/{prd_filename}\n"
                "When done, type /exit to return to ralph++.\n"
                "Do not use Ctrl-C — it may corrupt Claude's configuration.[/bold yellow]\n"
            )
        else:
            console.print(
                "\n[bold yellow]When Claude finishes generating the PRD, "
                "type /exit to return to ralph++.\n"
                "Do not use Ctrl-C — it may corrupt Claude's configuration.[/bold yellow]\n"
            )

    result = tool.run(prompt=prompt, cwd=worktree_path)
    if not result.success:
        raise RuntimeError(f"PRD generation failed (exit {result.exit_code})")

    prd_file = worktree_path / "tasks" / prd_filename
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
    last_fixer_diff: str = ""
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
                if last_fixer_diff:
                    context += (
                        "\nThe fixer made the following changes to address those findings:\n\n"
                        f"{last_fixer_diff}\n"
                    )
            else:
                context = ""

            review_prompt = render_prompt(
                review_cfg.reviewer_prompt,
                prd_file=str(prd_file),
                previous_findings=context,
                repo_path=str(worktree_path),
            )
            result = reviewer.run(prompt=review_prompt, cwd=worktree_path)
            if not result.success:
                raise RuntimeError(
                    f"PRD reviewer failed (exit {result.exit_code}): "
                    f"{(result.output or result.stderr)[:200]}"
                )

            if result.is_lgtm:
                console.print("[green]✓ PRD review passed (LGTM)[/green]")
                return

            previous_findings = result.output
            console.print(
                f"[yellow]Issues found in cycle {total_cycles} — running fix pass...[/yellow]"
            )
            pre_fix_sha = get_head_sha(worktree_path)
            fix_prompt = render_prompt(
                review_cfg.fixer_prompt,
                prd_file=str(prd_file),
                findings=result.output,
            )
            fix_result = fixer.run(prompt=fix_prompt, cwd=worktree_path)
            if not fix_result.success:
                raise RuntimeError(
                    f"PRD fixer failed (exit {fix_result.exit_code}): "
                    f"{(fix_result.output or fix_result.stderr)[:200]}"
                )
            last_fixer_diff = get_diff(worktree_path, pre_fix_sha)

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
    tool = make_tool(config.prd_json_tool, config)
    prompt = (
        f"Read the PRD at {prd_file} and convert it to structured JSON format.\n\n"
        "Save the output to scripts/ralph/prd.json\n\n"
        "## Output Format\n\n"
        "```json\n"
        "{\n"
        '  "project": "[Project Name]",\n'
        '  "branchName": "ralph/[feature-name-kebab-case]",\n'
        '  "description": "[Feature description from PRD]",\n'
        '  "userStories": [\n'
        "    {\n"
        '      "id": "US-001",\n'
        '      "title": "[Story title]",\n'
        '      "description": "As a [user], I want [feature] so that [benefit]",\n'
        '      "acceptanceCriteria": [\n'
        '        "Criterion 1",\n'
        '        "Criterion 2",\n'
        '        "Typecheck passes"\n'
        "      ],\n"
        '      "priority": 1,\n'
        '      "passes": false,\n'
        '      "notes": ""\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "```\n\n"
        "## Rules\n\n"
        "1. **Story size**: Each story must be completable in ONE iteration (one context "
        "window). If you cannot describe the change in 2-3 sentences, split it.\n"
        "   - Right-sized: add a database column, add a UI component, update a server action\n"
        "   - Too big: build entire dashboard, add authentication, refactor API\n\n"
        "2. **Dependency ordering**: Stories execute in priority order. Earlier stories "
        "must NOT depend on later ones.\n"
        "   - Correct: schema → backend logic → UI components → dashboards\n\n"
        "3. **Acceptance criteria**: Must be verifiable, not vague.\n"
        "   - Good: \"Add status column with default 'pending'\"\n"
        '   - Bad: "Works correctly", "Good UX"\n'
        "   - For validation requirements ('X must be Y'), include both:\n"
        "     - Positive: 'X works correctly when Y'\n"
        "     - Negative: 'X raises [Error] when not Y'\n\n"
        '4. **Always include** "Typecheck passes" as final criterion in every story.\n\n'
        '5. **For UI stories**, also include "Verify in browser using dev-browser skill".\n\n'
        "6. **IDs**: Sequential (US-001, US-002, etc.)\n\n"
        '7. **All stories**: Set `passes: false` and `notes: ""`\n\n'
        "8. **branchName**: Derive from feature name, kebab-case, prefixed with `ralph/`\n"
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


def review_prd_json_loop(
    prd_file: Path,
    prd_json: Path,
    worktree_path: Path,
    config: Config,
) -> None:
    """Review generated prd.json against the original PRD and codebase.

    Catches acceptance criteria that were sharpened, invented, or made
    infeasible during the PRD-to-JSON conversion step.
    """
    review_cfg = config.prd_json_review
    if not review_cfg.enabled:
        console.print("[dim]prd.json review disabled — skipping[/dim]")
        return

    console.print("[bold cyan]\n── Step: prd.json Review ──[/bold cyan]")
    reviewer = make_tool(review_cfg.reviewer, config)
    fixer = make_tool(review_cfg.fixer, config)

    for cycle in range(1, review_cfg.max_cycles + 1):
        console.print(f"[bold]prd.json review cycle {cycle}/{review_cfg.max_cycles}[/bold]")

        review_prompt = render_prompt(
            review_cfg.reviewer_prompt,
            prd_file=str(prd_file),
            prd_json_file=str(prd_json),
            repo_path=str(worktree_path),
        )
        result = reviewer.run(prompt=review_prompt, cwd=worktree_path)
        if not result.success:
            raise RuntimeError(
                f"prd.json reviewer failed (exit {result.exit_code}): "
                f"{(result.output or result.stderr)[:200]}"
            )

        if result.is_lgtm:
            console.print("[green]✓ prd.json review passed (LGTM)[/green]")
            return

        console.print("[yellow]Issues found in prd.json — running fix pass...[/yellow]")
        fix_prompt = render_prompt(
            review_cfg.fixer_prompt,
            prd_json_file=str(prd_json),
            prd_file=str(prd_file),
            findings=result.output,
        )
        fix_result = fixer.run(prompt=fix_prompt, cwd=worktree_path)
        if not fix_result.success:
            raise RuntimeError(
                f"prd.json fixer failed (exit {fix_result.exit_code}): "
                f"{(fix_result.output or fix_result.stderr)[:200]}"
            )

    console.print(
        f"[yellow]⚠ prd.json review: max cycles ({review_cfg.max_cycles}) "
        f"reached without LGTM — continuing[/yellow]"
    )
