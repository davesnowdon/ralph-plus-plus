"""Post-run review and fix loop."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from ..config import TEST_COMMANDS_GUIDANCE, Config, PostReviewConfig
from ..tools import make_tool, make_tool_with_permissions
from ._git import format_test_results, get_diff, get_head_sha, run_test_commands_with_output
from ._prompts import render_prompt
from .prd import MaxCyclesAbort, prompt_max_cycles
from .sandbox import BASE_SHA_FILE, format_all_completed, truncate_diff

console = Console()


@dataclass
class PostReviewResult:
    """Outcome of the post-run review loop."""

    outcome: str  # "lgtm", "accepted", "skipped"
    cycles: int  # total review cycles run


def post_review_loop(worktree_path: Path, config: Config) -> PostReviewResult:
    """
    After the Ralph sandbox completes, run a full review of the implementation
    against prd.json. Iteratively fix issues until LGTM or max_cycles reached.

    When *max_cycles* is exhausted without LGTM the user is prompted to choose:
    quit, retry another batch of cycles, or continue anyway.
    """
    review_cfg: PostReviewConfig = config.post_review
    if not review_cfg.enabled:
        console.print("[dim]Post-run review disabled — skipping[/dim]")
        return PostReviewResult(outcome="skipped", cycles=0)

    console.print("[bold cyan]\n── Step: Post-Run Review Loop ──[/bold cyan]")
    if config.orchestrated.auto_allow_test_commands and config.orchestrated.test_commands:
        reviewer = make_tool_with_permissions(
            review_cfg.reviewer, config, config.orchestrated.test_commands
        )
    else:
        reviewer = make_tool(review_cfg.reviewer, config)
    if config.orchestrated.auto_allow_test_commands and config.orchestrated.test_commands:
        fixer = make_tool_with_permissions(
            review_cfg.fixer, config, config.orchestrated.test_commands
        )
    else:
        fixer = make_tool(review_cfg.fixer, config)

    prd_json = worktree_path / "scripts" / "ralph" / "prd.json"

    # Extract completed stories for the reviewer
    stories_text, incomplete_ids = format_all_completed(prd_json)
    if incomplete_ids:
        incomplete_note = (
            f"\nNote: The following {len(incomplete_ids)} stories were not attempted "
            f"and should NOT be reviewed: {', '.join(incomplete_ids)}\n"
        )
    else:
        incomplete_note = ""

    # Compute full diff from the pre-run baseline (saved by run_sandbox)
    base_sha_path = worktree_path / BASE_SHA_FILE
    if base_sha_path.exists():
        base_sha = base_sha_path.read_text().strip()
        full_diff = get_diff(worktree_path, base_sha)
        full_diff = truncate_diff(full_diff, config.orchestrated.max_diff_chars)
        diff_text = f"\n## Git diff (all changes since run start)\n\n{full_diff}\n"
    else:
        diff_text = ""

    total_cycles = 0
    previous_findings: str = ""
    last_fixer_diff: str = ""
    retry_used = False
    while True:
        for cycle in range(1, review_cfg.max_cycles + 1):
            total_cycles += 1
            console.print(
                f"[bold]Post-run review cycle {total_cycles} "
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

            test_cmds = config.orchestrated.test_commands
            if test_cmds:
                cmd_list = "\n".join(f"  $ {cmd}" for cmd in test_cmds)
                guidance = TEST_COMMANDS_GUIDANCE.format(commands=cmd_list)
            else:
                guidance = ""

            # Pre-run tests so the reviewer gets concrete results.
            # Respect run_tests_between_steps — if the user disabled tests
            # during orchestrated iterations, skip them here too (#14).
            test_results_text = ""
            if test_cmds and config.orchestrated.run_tests_between_steps:
                console.print("  [dim]Running test commands before review...[/dim]")
                tests_ok, test_output = run_test_commands_with_output(worktree_path, test_cmds)
                status_str = "PASSED" if tests_ok else "FAILED"
                console.print(f"  [dim]Tests {status_str}[/dim]")
                test_results_text = format_test_results(test_output, tests_ok)

            review_prompt = render_prompt(
                review_cfg.reviewer_prompt,
                stories_under_review=stories_text,
                incomplete_stories_note=incomplete_note,
                diff=diff_text,
                previous_findings=context,
                test_commands_guidance=guidance,
                test_results=test_results_text,
            )
            result = reviewer.run(prompt=review_prompt, cwd=worktree_path)
            if not result.success:
                raise RuntimeError(
                    f"Post-run reviewer failed (exit {result.exit_code}): "
                    f"{(result.output or result.stderr)[:200]}"
                )

            if result.is_lgtm:
                console.print("[green]✓ Post-run review passed (LGTM)[/green]")
                return PostReviewResult(outcome="lgtm", cycles=total_cycles)

            previous_findings = result.output
            console.print(
                f"[yellow]Issues found in cycle {total_cycles} — running fix pass...[/yellow]"
            )
            pre_fix_sha = get_head_sha(worktree_path)
            fix_prompt = render_prompt(
                review_cfg.fixer_prompt,
                stories_under_review=stories_text,
                findings=result.output,
            )
            fix_result = fixer.run(prompt=fix_prompt, cwd=worktree_path)
            if not fix_result.success:
                raise RuntimeError(
                    f"Post-run fixer failed (exit {fix_result.exit_code}): "
                    f"{(fix_result.output or fix_result.stderr)[:200]}"
                )
            last_fixer_diff = get_diff(worktree_path, pre_fix_sha)

        action = prompt_max_cycles(
            "Post-run",
            review_cfg.max_cycles,
            continue_label="Accept — finish without reviewer approval",
            non_interactive=config.non_interactive,
            policy=config.non_interactive.on_max_cycles_post,
            retry_used=retry_used,
        )
        if action == "quit":
            raise MaxCyclesAbort
        if action == "continue":
            console.print("[yellow]Accepting implementation without reviewer approval[/yellow]")
            return PostReviewResult(outcome="accepted", cycles=total_cycles)
        # action == "retry" → loop again
        retry_used = True
        console.print(f"[cyan]Retrying another {review_cfg.max_cycles} review cycles...[/cyan]")
