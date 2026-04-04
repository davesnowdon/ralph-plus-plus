"""Docker sandbox invocation for the Ralph loop.

Supports two modes:
  - delegated: invoke ralph-sandbox with its built-in Ralph loop
  - orchestrated: ralph++ controls each iteration, reviewing between them
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

from ..config import TEST_COMMANDS_GUIDANCE, Config, OrchestratedConfig
from ..sandbox import resolve_sandbox_dir
from ..tools import make_tool
from ..tools.base import parse_max_severity, severity_at_or_above
from ._git import (
    commit_if_dirty,
    format_test_results,
    get_diff,
    get_head_sha,
    run_test_commands_with_output,
)

console = Console()

_ORCHESTRATED_CODER_PROMPT = """\
# Ralph Agent Instructions (Orchestrated Mode)

You are an autonomous coding agent working on a software project.

## Your Task

1. Read the PRD at `scripts/ralph/prd.json`
2. Read the progress log at `scripts/ralph/progress.txt` (check Codebase Patterns section first)
3. Pick the **highest priority** user story where `passes` is `false`
4. Implement that single user story
5. Run quality checks (e.g., typecheck, lint, test — use whatever your project requires)
6. If checks pass, commit ALL changes with message: `feat: [Story ID] - [Story Title]`
7. Update `scripts/ralph/prd.json` to set `passes` to `true` for the completed story
8. Append your progress to `scripts/ralph/progress.txt`

## Progress Report Format

APPEND to `scripts/ralph/progress.txt` (never replace, always append):
```
## [Date/Time] - [Story ID]
- What was implemented
- Files changed
- **Learnings for future iterations:**
  - Patterns discovered (e.g., "this codebase uses X for Y")
  - Gotchas encountered (e.g., "don't forget to update Z when changing W")
  - Useful context for future work
---
```

The learnings section is critical — it helps future iterations avoid repeating \
mistakes and understand the codebase better.

## Consolidate Patterns

If you discover a **reusable pattern** that future iterations should know, add \
it to the `## Codebase Patterns` section at the TOP of `scripts/ralph/progress.txt` \
(create it if it doesn't exist). Only add patterns that are **general and reusable**, \
not story-specific details.

## Quality Requirements

- ALL commits must pass your project's quality checks (typecheck, lint, test)
- Do NOT commit broken code
- Keep changes focused and minimal
- Follow existing code patterns

## Stop Condition

After completing a user story, check if ALL stories have `passes` set to `true`.

If ALL stories are complete and passing, reply with:
<promise>COMPLETE</promise>

If there are still stories with `passes` set to `false`, end your response \
normally (another iteration will pick up the next story).

## Important

- Work on ONE story per iteration
- Commit frequently
- Keep CI green
- Read the Codebase Patterns section in progress.txt before starting
"""

COMPLETE_SIGNAL = "<promise>COMPLETE</promise>"


BASE_SHA_FILE = "scripts/ralph/.base-sha"


@dataclass
class RunSummary:
    """Summary statistics from a sandbox run."""

    mode: str  # e.g. "orchestrated (backout)", "delegated"
    sandbox_ok: bool  # whether the sandbox signaled success
    iterations: int  # total iterations run (0 for delegated)
    stories_completed: int
    stories_total: int
    base_sha: str
    final_sha: str
    retries: int  # total backout retries or fix cycles


def run_sandbox(worktree_path: Path, config: Config) -> RunSummary:
    """Run the Ralph loop. Dispatches to delegated or orchestrated mode.

    Saves the pre-run HEAD SHA to ``scripts/ralph/.base-sha`` so that the
    post-run review can diff against the starting point.

    Returns a :class:`RunSummary` with statistics about the run.
    """
    base_sha = get_head_sha(worktree_path)
    base_sha_path = worktree_path / BASE_SHA_FILE
    base_sha_path.parent.mkdir(parents=True, exist_ok=True)
    base_sha_path.write_text(base_sha)

    if config.ralph.mode == "orchestrated":
        success = _run_orchestrated(worktree_path, config)
    else:
        success = _run_delegated(worktree_path, config)

    # Build summary from post-run state
    prd_json = worktree_path / "scripts" / "ralph" / "prd.json"
    story_status = read_story_status(prd_json) if prd_json.exists() else {}
    completed = sum(1 for v in story_status.values() if v)

    orch = config.orchestrated
    if config.ralph.mode == "orchestrated":
        strategy = "backout" if orch.backout_on_failure else "fixup"
        mode = f"orchestrated ({strategy})"
    else:
        mode = "delegated"

    # Read iteration/retry counters written by _run_orchestrated
    counters_path = worktree_path / "scripts" / "ralph" / ".run-counters"
    iterations = 0
    retries = 0
    if counters_path.exists():
        for line in counters_path.read_text().splitlines():
            key, _, val = line.partition("=")
            if key == "iterations":
                iterations = int(val)
            elif key == "retries":
                retries = int(val)

    return RunSummary(
        mode=mode,
        sandbox_ok=success,
        iterations=iterations,
        stories_completed=completed,
        stories_total=len(story_status),
        base_sha=base_sha,
        final_sha=get_head_sha(worktree_path),
        retries=retries,
    )


# ── Helpers ─────────────────────────────────────────────────────────────


def _sandbox_wrapper(config: Config) -> Path:
    """Resolve the path to bin/ralph-sandbox."""
    sandbox_dir = resolve_sandbox_dir(config)
    return sandbox_dir / "bin" / "ralph-sandbox"


def _session_runner_path(config: Config) -> Path:
    """Resolve the session runner script path (relative to ralph-plus-plus repo)."""
    # session_runner is relative to the ralph-plus-plus repo root
    rpp_root = Path(__file__).resolve().parent.parent.parent
    runner = rpp_root / config.ralph.session_runner
    if not runner.is_file():
        raise FileNotFoundError(f"Session runner not found at {runner}")
    return runner


def _build_sandbox_command(
    worktree_path: Path,
    config: Config,
    tool: str,
    session_runner: Path | None = None,
    extra_env: dict[str, str] | None = None,
    ralph_args: list[str] | None = None,
) -> list[str]:
    """Build the bin/ralph-sandbox CLI command."""
    wrapper = _sandbox_wrapper(config)

    cmd = [
        str(wrapper),
        "--project-dir",
        str(worktree_path),
        "--tool",
        tool,
        "--claude-config-dir",
        str(config.claude_config_dir),
        "--codex-config-dir",
        str(config.codex_config_dir),
    ]

    if session_runner is not None:
        cmd.extend(["--session-runner", str(session_runner)])

    if ralph_args:
        cmd.append("--")
        cmd.extend(ralph_args)

    return cmd


def _get_head_sha(worktree_path: Path) -> str:
    return get_head_sha(worktree_path)


def _backout_to(
    worktree_path: Path,
    sha: str,
    *,
    restore_files: dict[Path, str] | None = None,
) -> None:
    """Hard-reset to *sha* and optionally restore scaffold files.

    ``git reset --hard`` removes files that were staged after the target SHA
    (e.g. ``scripts/ralph/`` scaffold written by ``_setup_worktree_files``).
    Pass *restore_files* — a mapping of absolute paths to their content — to
    re-create them after the reset.
    """
    console.print(f"[yellow]  Backing out to {sha[:8]}...[/yellow]")
    subprocess.run(
        ["git", "reset", "--hard", sha],
        cwd=worktree_path,
        check=True,
    )
    # Restore scaffold directory and base files destroyed by the hard reset.
    _setup_worktree_files(worktree_path)
    if restore_files:
        for path, content in restore_files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)


def _render_prompt(template: str, **kwargs: str) -> str:
    """Substitute placeholders in a prompt template."""
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", value)
    return result


def _test_commands_guidance(orch: OrchestratedConfig) -> str:
    """Build test-command guidance block for reviewer prompts, or empty string."""
    if not orch.test_commands:
        return ""
    cmd_list = "\n".join(f"  $ {cmd}" for cmd in orch.test_commands)
    return TEST_COMMANDS_GUIDANCE.format(commands=cmd_list)


# ── PRD helpers ────────────────────────────────────────────────────────


class PrdParseError(RuntimeError):
    """Raised when prd.json cannot be parsed or is structurally invalid."""


def load_prd(prd_json: Path) -> dict[str, Any]:
    """Load and validate prd.json structure. Raises PrdParseError on failure."""
    try:
        data: dict[str, Any] = json.loads(prd_json.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise PrdParseError(f"Failed to parse {prd_json}: {e}") from e
    if not isinstance(data, dict) or "userStories" not in data:
        raise PrdParseError(f"{prd_json} is missing 'userStories' key")
    return data


def read_story_status(prd_json: Path) -> dict[str, bool]:
    """Read prd.json and return ``{story_id: passes}`` mapping."""
    data = load_prd(prd_json)
    return {s["id"]: bool(s.get("passes", False)) for s in data["userStories"]}


def format_stories(prd_json: Path, story_ids: set[str]) -> str:
    """Extract and format specific stories from prd.json for embedding in prompts."""
    data = load_prd(prd_json)
    stories = [s for s in data["userStories"] if s["id"] in story_ids]
    parts: list[str] = []
    for s in stories:
        criteria = "\n".join(f"  - {c}" for c in s.get("acceptanceCriteria", []))
        parts.append(
            f"### {s['id']}: {s.get('title', '(no title)')}\n"
            f"{s.get('description', '')}\n\n"
            f"Acceptance criteria:\n{criteria}"
        )
    return "\n\n".join(parts)


def format_all_completed(prd_json: Path) -> tuple[str, list[str]]:
    """Format all completed stories and return IDs of incomplete ones.

    Returns ``(formatted_text, incomplete_ids)``.
    """
    data = load_prd(prd_json)
    completed_ids = {s["id"] for s in data["userStories"] if s.get("passes")}
    incomplete_ids = [s["id"] for s in data["userStories"] if not s.get("passes")]
    text = format_stories(prd_json, completed_ids)
    return text, incomplete_ids


_RETRY_HEADER = """\
⚠ RETRY {attempt}/{max_attempts}
Your previous attempt was REJECTED by the reviewer.
The following findings MUST be resolved. Failure to address
these specific issues will result in another rejection:

"""


def _wrap_retry_findings(findings: str, attempt: int, max_attempts: int) -> str:
    """Prepend a structured header to findings on retry attempts."""
    if attempt <= 1 or not findings:
        return findings
    return _RETRY_HEADER.format(attempt=attempt, max_attempts=max_attempts) + findings


def _get_diff(worktree_path: Path, from_sha: str) -> str:
    return get_diff(worktree_path, from_sha)


def _commit_if_dirty(worktree_path: Path, message: str) -> bool:
    """Stage and commit all changes if the working tree has uncommitted work.

    Returns True if a commit was created, False if tree was clean.
    """
    return commit_if_dirty(worktree_path, message)


# ── Delegated mode ─────────────────────────────────────────────────────


def _run_delegated(worktree_path: Path, config: Config) -> bool:
    """Mode 1: Invoke ralph-sandbox with its built-in Ralph loop."""
    console.print("[bold cyan]\n── Delegated mode ──[/bold cyan]")

    cmd = _build_sandbox_command(
        worktree_path,
        config,
        tool=config.ralph.sandbox_tool,
        ralph_args=[str(config.ralph.max_iterations)],
    )

    console.print("[dim]$ " + " ".join(cmd[:5]) + " ...[/dim]")
    result = subprocess.run(cmd, text=True)

    if result.returncode == 0:
        console.print("[green]✓ Ralph completed successfully[/green]")
        return True
    else:
        console.print(f"[red]✗ Ralph exited with code {result.returncode}[/red]")
        return False


# ── Orchestrated mode ──────────────────────────────────────────────────


def _write_coder_prompt(worktree_path: Path, findings: str = "") -> None:
    """Write the orchestrated coder prompt, optionally with review findings."""
    ralph_dir = worktree_path / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True, exist_ok=True)
    prompt = _ORCHESTRATED_CODER_PROMPT
    if findings:
        prompt += "\n" + findings
    (ralph_dir / "CLAUDE.md").write_text(prompt)


def _setup_worktree_files(worktree_path: Path) -> None:
    """Ensure scripts/ralph/ has the required files for orchestrated mode.

    In custom-runner mode the sandbox entrypoint does not copy ralph.sh or
    CLAUDE.md, so ralph++ must set them up before the first iteration.
    """
    _write_coder_prompt(worktree_path)

    progress = worktree_path / "scripts" / "ralph" / "progress.txt"
    if not progress.exists():
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_text("# Ralph Progress Log\nStarted: orchestrated mode\n---\n")


@dataclass
class ReviewResult:
    """Structured result from a review iteration."""

    passed: bool
    findings: str
    max_severity: str | None  # None when LGTM or unparseable
    minor_only: bool  # True when all findings are minor (or LGTM)


def _review_iteration(
    iteration: int,
    diff: str,
    worktree_path: Path,
    config: Config,
    previous_findings: str = "",
    fixer_diff: str = "",
    stories_under_review: str = "",
    test_results: str = "",
) -> ReviewResult:
    """Run reviewer on the iteration diff."""
    orch = config.orchestrated
    reviewer = make_tool(orch.reviewer, config)

    if previous_findings:
        context = (
            "\nThe previous review cycle found these issues (which have since "
            "been addressed by a fix pass). Focus on whether the fixes are "
            "adequate and whether any NEW issues remain. Do not re-raise issues "
            "that have been resolved:\n\n"
            f"{previous_findings}\n"
        )
        if fixer_diff:
            context += (
                "\nThe fixer made the following changes to address those findings:\n\n"
                f"{fixer_diff}\n"
            )
    else:
        context = ""

    review_prompt = _render_prompt(
        orch.review_prompt,
        diff=diff,
        stories_under_review=stories_under_review,
        previous_findings=context,
        test_commands_guidance=_test_commands_guidance(orch),
        test_results=test_results,
    )
    result = reviewer.run(prompt=review_prompt, cwd=worktree_path)
    if not result.success:
        raise RuntimeError(
            f"Iteration reviewer failed (exit {result.exit_code}): {result.output[:200]}"
        )

    if result.is_lgtm:
        console.print(f"  [green]✓ Review passed (LGTM) — iteration {iteration}[/green]")
        return ReviewResult(passed=True, findings=result.output, max_severity=None, minor_only=True)

    # Parse severity from reviewer output
    max_sev = parse_max_severity(result.output)
    threshold = orch.backout_severity_threshold

    if max_sev is not None and not severity_at_or_above(max_sev, threshold):
        # All findings are below the backout threshold — accept with warnings
        console.print(
            f"  [yellow]Review found only {max_sev} issues — "
            f"accepting iteration {iteration} with warnings[/yellow]"
        )
        return ReviewResult(
            passed=True, findings=result.output, max_severity=max_sev, minor_only=True
        )

    console.print(f"  [yellow]Issues found in iteration {iteration}[/yellow]")
    return ReviewResult(
        passed=False, findings=result.output, max_severity=max_sev, minor_only=False
    )


def _run_fixer_in_sandbox(
    findings: str,
    worktree_path: Path,
    config: Config,
    stories_under_review: str = "",
) -> subprocess.CompletedProcess[str]:
    """Invoke the fixer agent inside the sandbox with the fix prompt."""
    orch = config.orchestrated
    fix_prompt = _render_prompt(
        orch.fix_prompt,
        findings=findings,
        stories_under_review=stories_under_review,
    )

    # Write fix prompt to a temp file in the worktree so the session runner can read it
    prompt_file = worktree_path / "scripts" / "ralph" / ".fix-prompt.md"
    prompt_file.write_text(fix_prompt)

    session_runner = _session_runner_path(config)
    cmd = _build_sandbox_command(
        worktree_path,
        config,
        tool=orch.fixer,
        session_runner=session_runner,
        ralph_args=["1"],
    )

    # Set RALPH_PROMPT_FILE so the session runner uses the fix prompt
    env_patch = {"RALPH_PROMPT_FILE": str(Path("scripts/ralph/.fix-prompt.md"))}
    console.print(f"  [dim]Running fixer ({orch.fixer})...[/dim]")
    return subprocess.run(cmd, text=True, capture_output=True, env=_merge_env(env_patch))


def _merge_env(extra: dict[str, str]) -> dict[str, str]:
    """Merge extra env vars into a copy of the current environment."""
    import os

    env = os.environ.copy()
    env.update(extra)
    return env


def _run_orchestrated(worktree_path: Path, config: Config) -> bool:
    """Mode 2: ralph++ controls each iteration with review between them."""
    orch: OrchestratedConfig = config.orchestrated
    strategy = "backout" if orch.backout_on_failure else "fixup"
    console.print(f"[bold cyan]\n── Orchestrated mode ({strategy}) ──[/bold cyan]")
    session_runner = _session_runner_path(config)

    # Phase 0: Setup
    _setup_worktree_files(worktree_path)
    prd_json = worktree_path / "scripts" / "ralph" / "prd.json"
    if not prd_json.exists():
        raise FileNotFoundError(f"prd.json not found at {prd_json}")

    last_findings = ""
    consecutive_idle = 0
    total_retries = 0
    prev_story_status = read_story_status(prd_json)
    total_stories = len(prev_story_status)

    def _save_counters(iters: int) -> None:
        """Persist iteration/retry counters for RunSummary."""
        counters = worktree_path / "scripts" / "ralph" / ".run-counters"
        counters.write_text(f"iterations={iters}\nretries={total_retries}\n")

    for iteration in range(1, config.ralph.max_iterations + 1):
        # Reset findings at the start of each outer iteration so that
        # stale context from a previous iteration does not suppress
        # legitimate findings in the current one (#32).
        last_findings = ""

        completed = sum(1 for v in prev_story_status.values() if v)
        console.print(
            f"\n[bold]═══ Iteration {iteration}/{config.ralph.max_iterations} "
            f"({completed}/{total_stories} stories done) ═══[/bold]"
        )

        pre_sha = _get_head_sha(worktree_path)

        # Snapshot files that must survive git reset --hard during backout.
        # prd.json is re-read each iteration so backout restores the current
        # state (with previously-completed stories) rather than the initial one.
        # .base-sha is static but gets destroyed by reset if it was staged.
        base_sha_path = worktree_path / BASE_SHA_FILE
        restore_files: dict[Path, str] = {prd_json: prd_json.read_text()}
        if base_sha_path.exists():
            restore_files[base_sha_path] = base_sha_path.read_text()

        # Run coding step (with retries for backout mode)
        max_attempts = orch.max_iteration_retries + 1 if orch.backout_on_failure else 1

        iteration_passed = False
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                console.print(
                    f"  [yellow]Retry {attempt}/{max_attempts} for iteration {iteration}[/yellow]"
                )

            # Write per-attempt prompt (inside loop so retries get latest findings)
            extra_env: dict[str, str] = {}
            if orch.prompt_template is not None:
                progress_file = worktree_path / "scripts" / "ralph" / "progress.txt"
                progress_text = progress_file.read_text() if progress_file.exists() else ""
                prompt_text = _render_prompt(
                    orch.prompt_template,
                    iteration=str(iteration),
                    prd_file=str(prd_json),
                    progress=progress_text,
                    review_findings=_wrap_retry_findings(last_findings, attempt, max_attempts),
                )
                iter_prompt = worktree_path / "scripts" / "ralph" / ".iteration-prompt.md"
                iter_prompt.write_text(prompt_text)
                extra_env["RALPH_PROMPT_FILE"] = str(Path("scripts/ralph/.iteration-prompt.md"))
            elif attempt > 1 and last_findings:
                # Default prompt flow: append review findings to CLAUDE.md so the
                # coder knows why its previous attempt was rejected.
                _write_coder_prompt(
                    worktree_path,
                    findings=_wrap_retry_findings(last_findings, attempt, max_attempts),
                )

            # Run coder in sandbox
            cmd = _build_sandbox_command(
                worktree_path,
                config,
                tool=orch.coder,
                session_runner=session_runner,
                ralph_args=["1"],
            )
            console.print(f"  [dim]Running coder ({orch.coder})...[/dim]")
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                env=_merge_env(extra_env) if extra_env else None,
            )

            combined_output = (result.stdout or "") + (result.stderr or "")
            if combined_output:
                console.print(combined_output)

            # Check for infra/sandbox failure
            if result.returncode != 0:
                console.print(f"  [red]✗ Coder process failed (exit {result.returncode})[/red]")
                if orch.backout_on_failure and attempt < max_attempts:
                    _backout_to(worktree_path, pre_sha, restore_files=restore_files)
                    continue
                console.print("  [red]Infra failure — skipping review[/red]")
                break

            # Check for completion signal — verify against prd.json
            if COMPLETE_SIGNAL in combined_output:
                _commit_if_dirty(worktree_path, f"ralph: coder iteration {iteration}")
                story_status = read_story_status(prd_json)
                if all(story_status.values()):
                    console.print("[green]Ralph signaled COMPLETE[/green]")
                    _save_counters(iteration)
                    return True
                incomplete = [sid for sid, p in story_status.items() if not p]
                console.print(
                    f"[yellow]Ralph signaled COMPLETE but {len(incomplete)} stories "
                    f"still have passes=false: {', '.join(sorted(incomplete))} — "
                    "continuing iterations[/yellow]"
                )

            # Force-commit any uncommitted coder changes
            _commit_if_dirty(worktree_path, f"ralph: coder iteration {iteration}")

            # Idle detection: if coder made no changes, it may have finished
            post_sha = _get_head_sha(worktree_path)
            if post_sha == pre_sha:
                consecutive_idle += 1
                if consecutive_idle >= orch.max_idle_iterations:
                    console.print(
                        f"[green]No changes for {consecutive_idle} consecutive iterations "
                        "— treating as complete[/green]"
                    )
                    _save_counters(iteration)
                    return True
                console.print(
                    f"  [dim]No changes this iteration "
                    f"(idle {consecutive_idle}/{orch.max_idle_iterations})[/dim]"
                )
                break  # Skip review — nothing to review
            else:
                consecutive_idle = 0

            # Detect which stories the coder completed this iteration
            curr_story_status = read_story_status(prd_json)
            newly_completed = {
                sid
                for sid, passes in curr_story_status.items()
                if passes and not prev_story_status.get(sid, False)
            }
            if newly_completed:
                console.print(
                    f"  [green]Stories completed: {', '.join(sorted(newly_completed))}[/green]"
                )

            # Run tests/linter (optional)
            tests_failed = False
            test_results_text = ""
            if orch.run_tests_between_steps and orch.test_commands:
                console.print("  [dim]Running test commands...[/dim]")
                tests_ok, test_output = run_test_commands_with_output(
                    worktree_path, orch.test_commands
                )
                if test_output:
                    console.print(test_output)
                test_results_text = format_test_results(test_output, tests_ok)
                if not tests_ok:
                    console.print("  [yellow]Tests failed — treating as review failure[/yellow]")
                    tests_failed = True
                    if orch.backout_on_failure and attempt < max_attempts:
                        _backout_to(worktree_path, pre_sha, restore_files=restore_files)
                        continue

            # Review changes — scope to newly-completed stories only
            stories_text = (
                format_stories(prd_json, newly_completed)
                if newly_completed
                else format_stories(
                    prd_json,
                    {sid for sid, p in curr_story_status.items() if not p},
                )
            )
            diff = _get_diff(worktree_path, pre_sha)
            review = _review_iteration(
                iteration,
                diff,
                worktree_path,
                config,
                previous_findings=last_findings,
                stories_under_review=stories_text,
                test_results=test_results_text,
            )
            last_findings = review.findings

            if review.passed and not tests_failed:
                if review.minor_only and review.max_severity is not None:
                    console.print("  [dim]Minor findings carried forward[/dim]")
                iteration_passed = True
                break
            elif tests_failed and review.passed:
                console.print(
                    "  [yellow]Reviewer approved but tests failed — not accepting[/yellow]"
                )
                last_findings = "Tests failed. " + review.findings

            # Handle review failure
            if orch.backout_on_failure:
                # PATH A: Backout and retry
                if attempt < max_attempts:
                    total_retries += 1
                    _backout_to(worktree_path, pre_sha, restore_files=restore_files)
                else:
                    console.print(
                        f"  [red]✗ All retries exhausted for iteration {iteration} — aborting[/red]"
                    )
                    _save_counters(iteration)
                    return False
            else:
                # PATH B: Invoke fixer to fix in-place
                for fix_cycle in range(1, orch.max_iteration_retries + 1):
                    total_retries += 1
                    console.print(
                        f"  [dim]Fix cycle {fix_cycle}/{orch.max_iteration_retries}[/dim]"
                    )
                    pre_fix_sha = _get_head_sha(worktree_path)
                    fixer_result = _run_fixer_in_sandbox(
                        review.findings, worktree_path, config, stories_text
                    )
                    if fixer_result.returncode != 0:
                        console.print(
                            f"  [red]✗ Fixer process failed (exit {fixer_result.returncode})[/red]"
                        )
                        break

                    # Force-commit any uncommitted fixer changes
                    _commit_if_dirty(
                        worktree_path,
                        f"ralph: fixer cycle {fix_cycle} iteration {iteration}",
                    )
                    fix_diff = _get_diff(worktree_path, pre_fix_sha)

                    # Re-run tests after fix (if enabled)
                    fix_test_results = ""
                    if orch.run_tests_between_steps and orch.test_commands:
                        console.print("  [dim]Re-running test commands after fix...[/dim]")
                        fix_tests_ok, fix_test_output = run_test_commands_with_output(
                            worktree_path, orch.test_commands
                        )
                        if fix_test_output:
                            console.print(fix_test_output)
                        fix_test_results = format_test_results(fix_test_output, fix_tests_ok)
                        if not fix_tests_ok:
                            console.print("  [yellow]Tests still failing after fix[/yellow]")
                            continue

                    # Re-review after fix
                    diff = _get_diff(worktree_path, pre_sha)
                    review = _review_iteration(
                        iteration,
                        diff,
                        worktree_path,
                        config,
                        previous_findings=review.findings,
                        fixer_diff=fix_diff,
                        stories_under_review=stories_text,
                        test_results=fix_test_results,
                    )
                    last_findings = review.findings
                    if review.passed:
                        iteration_passed = True
                        break

                if not iteration_passed:
                    console.print(
                        f"  [red]✗ Fix cycles exhausted for iteration {iteration} — aborting[/red]"
                    )
                    _save_counters(iteration)
                    return False
                break  # In fix-in-place mode we don't retry the coder, only the fixer

        # Update story status for next iteration
        prev_story_status = read_story_status(prd_json)

        # Append to progress (skip idle iterations — the coder writes its own
        # detailed entries via the orchestrated prompt)
        if consecutive_idle == 0:
            progress_file = worktree_path / "scripts" / "ralph" / "progress.txt"
            progress_file.parent.mkdir(parents=True, exist_ok=True)
            status = "passed" if iteration_passed else "failed"
            with open(progress_file, "a") as f:
                f.write(f"\n## Iteration {iteration} — {status}\n---\n")

    console.print(
        f"[yellow]Reached max iterations ({config.ralph.max_iterations}) "
        "without completion signal[/yellow]"
    )
    _save_counters(config.ralph.max_iterations)
    return False
