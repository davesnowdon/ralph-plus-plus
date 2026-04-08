"""Docker sandbox invocation for the Ralph loop.

Supports two modes:
  - delegated: invoke ralph-sandbox with its built-in Ralph loop
  - orchestrated: ralph++ controls each iteration, reviewing between them
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from ..config import TEST_COMMANDS_GUIDANCE, Config, OrchestratedConfig
from ..sandbox import resolve_sandbox_dir
from ..tools.base import parse_max_severity, severity_at_or_above
from ..tools.cli_tool import CliTool
from ..tools.permissions import bash_permissions_from_commands
from ._git import (
    commit_if_dirty,
    format_test_results,
    get_diff,
    get_head_sha,
    run_test_commands_with_output,
)
from ._prompts import render_prompt

logger = logging.getLogger(__name__)
console = Console()

_ORCHESTRATED_CODER_PROMPT = """\
# Ralph Agent Instructions (Orchestrated Mode)

You are an autonomous coding agent working on a software project.

## Your Task

1. Read the PRD at `scripts/ralph/prd.json`
2. Read the progress log at `scripts/ralph/progress.txt` (check Codebase Patterns section first)
3. Pick the **highest priority** user story where `passes` is `false`\
{story_filter_instruction}
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


def validate_sandbox_prerequisites(config: Config) -> None:
    """Validate sandbox configuration before expensive workflow steps.

    Call this early (before worktree creation) so misconfigurations
    fail fast instead of minutes into the workflow.
    """
    # Validate sandbox directory resolves
    resolve_sandbox_dir(config)

    # Validate session runner exists (orchestrated mode only)
    if config.ralph.mode == "orchestrated":
        _session_runner_path(config)


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

    # Mutable counters dict so _run_orchestrated can update it even if it
    # raises — the finally block always persists whatever was recorded.
    counters: dict[str, int] = {"iterations": 0, "retries": 0}

    if config.ralph.mode == "orchestrated":
        try:
            success = _run_orchestrated(worktree_path, config, counters)
        finally:
            _save_counters(worktree_path, counters["iterations"], counters["retries"])
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

    return RunSummary(
        mode=mode,
        sandbox_ok=success,
        iterations=counters["iterations"],
        stories_completed=completed,
        stories_total=len(story_status),
        base_sha=base_sha,
        final_sha=get_head_sha(worktree_path),
        retries=counters["retries"],
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
    result: dict[str, bool] = {}
    for i, s in enumerate(data["userStories"]):
        sid = s.get("id")
        if not sid:
            raise PrdParseError(f"{prd_json}: userStories[{i}] is missing 'id' field")
        result[sid] = bool(s.get("passes", False))
    return result


def format_stories(prd_json: Path, story_ids: set[str]) -> str:
    """Extract and format specific stories from prd.json for embedding in prompts."""
    data = load_prd(prd_json)
    stories = [s for s in data["userStories"] if s.get("id") in story_ids]
    if story_ids and len(stories) < len(story_ids):
        found = {s.get("id") for s in stories}
        missing = story_ids - found
        logger.warning("Story IDs not found in %s: %s", prd_json, ", ".join(sorted(missing)))
    parts: list[str] = []
    for s in stories:
        criteria = "\n".join(f"  - {c}" for c in s.get("acceptanceCriteria", []))
        parts.append(
            f"### {s['id']}: {s.get('title', '(no title)')}\n"
            f"{s.get('description', '')}\n\n"
            f"Acceptance criteria:\n{criteria}"
        )
    return "\n\n".join(parts)


def enforce_passes_baseline(
    prd_json: Path,
    baseline: dict[str, bool],
    *,
    approved: set[str] | None = None,
) -> set[str]:
    """Restore ``passes`` in *prd_json* to the *baseline* for unapproved stories.

    The reviewer (not the coder) owns the ``passes`` field. If the coder
    flipped a story to ``true`` during an iteration but the reviewer has not
    approved it, we must roll the flag back to its baseline state. This is
    the structural defense for issue #129.

    *approved* is the set of story IDs that the reviewer *has* approved this
    iteration (typically empty at rejection time; only populated when we want
    to preserve reviewer-approved flips). Stories in *approved* retain their
    current ``passes`` value.

    Returns the set of story IDs whose ``passes`` was rolled back.
    """
    approved = approved or set()
    data = load_prd(prd_json)
    changed: set[str] = set()
    for story in data["userStories"]:
        sid = story.get("id")
        if not sid or sid in approved:
            continue
        current = bool(story.get("passes", False))
        expected = baseline.get(sid, False)
        if current != expected:
            story["passes"] = expected
            changed.add(sid)
    if changed:
        prd_json.write_text(json.dumps(data, indent=2) + "\n")
    return changed


def next_target_story(
    prd_json: Path,
    excluded_ids: set[str] | None = None,
    story_filter: set[str] | None = None,
) -> str | None:
    """Return the story ID the coder is expected to pick next, or ``None``.

    Mirrors the orchestrated coder prompt rule: "highest priority story where
    ``passes`` is ``false``". Stories in *excluded_ids* (skipped after
    retry exhaustion) and those outside *story_filter* are ignored.
    """
    excluded = excluded_ids or set()
    data = load_prd(prd_json)
    candidates: list[dict[str, Any]] = []
    for story in data["userStories"]:
        sid = story.get("id")
        if not sid or sid in excluded:
            continue
        if story_filter is not None and sid not in story_filter:
            continue
        if story.get("passes", False):
            continue
        candidates.append(story)
    if not candidates:
        return None
    # Stable order: by priority (lower is higher priority, per the
    # orchestrated-mode prompt convention), then by id for determinism.
    candidates.sort(key=lambda s: (int(s.get("priority", 999)), str(s.get("id", ""))))
    return str(candidates[0].get("id"))


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

_RETRY_ESCALATION_HEADER = """\
⚠ RETRY {attempt}/{max_attempts} — REPEATED FAILURE ({repeat_count}x)
You have failed to address these specific findings {repeat_count} times
in a row. Your current approach is not working. You MUST:

  1. Re-read the relevant source files from scratch, paying attention
     to the code paths cited in the reviewer's evidence below — not
     just the area you have been editing.
  2. Take a FUNDAMENTALLY different approach than your previous attempts.
  3. If the reviewer cites a specific function, class, or code path,
     start by reading that exact location and understanding why the
     previous change did not satisfy the requirement.

The reviewer's findings are reproduced verbatim below:

"""


# #126: token-set Jaccard similarity used to detect same-finding convergence.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _normalize_findings(text: str) -> set[str]:
    """Normalize reviewer output for similarity comparison.

    Lowercased alphanumeric tokens only — ignores punctuation, whitespace,
    and ordering. Short tokens (<3 chars) are dropped because they add noise
    without carrying semantic weight.
    """
    if not text:
        return set()
    return {tok.lower() for tok in _TOKEN_RE.findall(text) if len(tok) >= 3}


def findings_similarity(a: str, b: str) -> float:
    """Return Jaccard similarity (0.0–1.0) between two reviewer outputs.

    Returns 0.0 when either side is empty. Used to detect #126 convergence:
    when retry N+1 cites essentially the same finding as retry N, the coder
    has locked onto a wrong interpretation and the orchestrator should
    escalate the prompt or stop wasting cycles.
    """
    tokens_a = _normalize_findings(a)
    tokens_b = _normalize_findings(b)
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return intersection / union if union else 0.0


def _wrap_retry_findings(
    findings: str,
    attempt: int,
    max_attempts: int,
    repeat_count: int = 0,
) -> str:
    """Prepend a structured header to findings on retry attempts.

    When *repeat_count* is >= 1 (meaning this is the N-th consecutive retry
    that cites substantially the same findings) the header escalates to
    explicitly call out the repeat pattern and demand a different approach
    (#126).
    """
    if attempt <= 1 or not findings:
        return findings
    if repeat_count >= 1:
        return (
            _RETRY_ESCALATION_HEADER.format(
                attempt=attempt, max_attempts=max_attempts, repeat_count=repeat_count + 1
            )
            + findings
        )
    return _RETRY_HEADER.format(attempt=attempt, max_attempts=max_attempts) + findings


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


def _write_coder_prompt(
    worktree_path: Path,
    findings: str = "",
    story_filter: list[str] | None = None,
    skipped_story_ids: set[str] | None = None,
) -> None:
    """Write the orchestrated coder prompt, optionally with review findings."""
    ralph_dir = worktree_path / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True, exist_ok=True)

    parts: list[str] = []
    if story_filter:
        ids = ", ".join(story_filter)
        parts.append(
            f"\n   **IMPORTANT: Only work on these story IDs: {ids}. "
            "Skip all other stories even if they have `passes` set to `false`.**"
        )
    if skipped_story_ids:
        skipped = ", ".join(sorted(skipped_story_ids))
        parts.append(
            f"\n   **IMPORTANT: Do NOT work on these story IDs (they have been "
            f"skipped after exhausting retries): {skipped}. "
            "Pick the next highest-priority unfinished story instead.**"
        )
    filter_instruction = "".join(parts)

    prompt = _ORCHESTRATED_CODER_PROMPT.replace("{story_filter_instruction}", filter_instruction)
    if findings:
        prompt += "\n" + findings
    (ralph_dir / "CLAUDE.md").write_text(prompt)


def _setup_worktree_files(
    worktree_path: Path,
    story_filter: list[str] | None = None,
) -> None:
    """Ensure scripts/ralph/ has the required files for orchestrated mode.

    In custom-runner mode the sandbox entrypoint does not copy ralph.sh or
    CLAUDE.md, so ralph++ must set them up before the first iteration.
    """
    _write_coder_prompt(worktree_path, story_filter=story_filter)

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


def truncate_diff(diff: str, max_chars: int) -> str:
    """Truncate a diff to *max_chars* with a note if truncated."""
    if max_chars <= 0 or len(diff) <= max_chars:
        return diff
    return (
        diff[:max_chars] + f"\n\n... [diff truncated at {max_chars} characters; "
        f"{len(diff) - max_chars} characters omitted] ..."
    )


def _review_iteration(
    iteration: int,
    diff: str,
    worktree_path: Path,
    config: Config,
    previous_findings: str = "",
    fixer_diff: str = "",
    stories_under_review: str = "",
    test_results: str = "",
    first_review: bool = False,
) -> ReviewResult:
    """Run reviewer on the iteration diff."""
    orch = config.orchestrated
    # Apply reviewer_timeout to the tool config if not already set.
    # Tool config wins when both are set; log the precedence so users can
    # understand why their orchestrated.reviewer_timeout setting may
    # appear to have no effect (#82).
    tool_cfg = config.get_tool(orch.reviewer)
    if orch.reviewer_timeout and not tool_cfg.timeout:
        tool_cfg = dataclasses.replace(tool_cfg, timeout=orch.reviewer_timeout)
    elif orch.reviewer_timeout and tool_cfg.timeout:
        logger.debug(
            "orchestrated.reviewer_timeout=%d ignored; tool '%s' already has timeout=%d",
            orch.reviewer_timeout,
            orch.reviewer,
            tool_cfg.timeout,
        )

    # Use the tool factory to augment Bash permissions when auto_allow_test_commands
    # is enabled, matching the post_review path (#89).
    if orch.auto_allow_test_commands and orch.test_commands and tool_cfg.allowed_tools:
        extra = bash_permissions_from_commands(orch.test_commands)
        tool_cfg = dataclasses.replace(
            tool_cfg,
            allowed_tools=list(tool_cfg.allowed_tools) + extra,
        )
    reviewer = CliTool(name=orch.reviewer, config=tool_cfg)

    # Truncate diffs to prevent exceeding model context windows
    diff = truncate_diff(diff, orch.max_diff_chars)
    if fixer_diff:
        fixer_diff = truncate_diff(fixer_diff, orch.max_diff_chars)

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

    # Use the feasibility-aware prompt for the first review of each iteration
    prompt_template = (
        orch.first_review_prompt if first_review and not previous_findings else orch.review_prompt
    )
    review_prompt = render_prompt(
        prompt_template,
        diff=diff,
        stories_under_review=stories_under_review,
        previous_findings=context,
        test_commands_guidance=_test_commands_guidance(orch),
        test_results=test_results,
    )
    result = reviewer.run(prompt=review_prompt, cwd=worktree_path)
    if not result.success:
        raise RuntimeError(
            f"Iteration reviewer failed (exit {result.exit_code}): "
            f"{(result.output or result.stderr)[:200]}"
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
    fix_prompt = render_prompt(
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
    env_patch = {"RALPH_PROMPT_FILE": str(worktree_path / "scripts" / "ralph" / ".fix-prompt.md")}
    console.print(f"  [dim]Running fixer ({orch.fixer})...[/dim]")
    try:
        return subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            env=_merge_env(env_patch),
            timeout=orch.fixer_timeout or None,
        )
    except subprocess.TimeoutExpired:
        console.print(f"  [red]✗ Fixer timed out after {orch.fixer_timeout}s[/red]")
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="timeout")


def _merge_env(extra: dict[str, str]) -> dict[str, str]:
    """Merge extra env vars into a copy of the current environment."""
    env = os.environ.copy()
    env.update(extra)
    return env


def _skip_story(
    worktree_path: Path,
    story_id: str,
    iteration: int,
    findings: str,
    skipped_story_ids: set[str],
    *,
    pre_sha: str | None = None,
    restore_files: dict[Path, str] | None = None,
) -> None:
    """Record a retry-exhaustion skip and (in backout mode) reset the worktree.

    - Adds *story_id* to *skipped_story_ids* so future iterations exclude it.
    - Appends a failure entry to ``scripts/ralph/progress.txt`` with truncated
      reviewer findings so the post-run review (#127) and humans can see why.
    - When *pre_sha* is provided, hard-resets the worktree to that SHA and
      restores *restore_files*, matching the backout path (#127). In
      fix-in-place mode the caller passes ``pre_sha=None`` so any partial
      fix-cycle commits are left in place; they can still be rolled back by
      the post-run review if needed.
    """
    skipped_story_ids.add(story_id)
    console.print(
        f"  [yellow]Skipping {story_id} after retry exhaustion — advancing to next story[/yellow]"
    )

    progress_file = worktree_path / "scripts" / "ralph" / "progress.txt"
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    trimmed_findings = (findings or "").strip()
    if len(trimmed_findings) > 1500:
        trimmed_findings = trimmed_findings[:1500] + "\n... (truncated)"
    with open(progress_file, "a") as f:
        f.write(
            f"\n## Iteration {iteration} — {story_id} SKIPPED (retry exhaustion)\n"
            f"Reason: all retries/fix cycles exhausted without reviewer LGTM.\n"
            f"Last reviewer findings:\n{trimmed_findings or '(none)'}\n---\n"
        )

    if pre_sha is not None:
        _backout_to(worktree_path, pre_sha, restore_files=restore_files)


def _save_counters(worktree_path: Path, iterations: int, retries: int) -> None:
    """Persist iteration/retry counters for RunSummary."""
    counters = worktree_path / "scripts" / "ralph" / ".run-counters"
    counters.parent.mkdir(parents=True, exist_ok=True)
    counters.write_text(f"iterations={iterations}\nretries={retries}\n")


def _run_orchestrated(
    worktree_path: Path,
    config: Config,
    counters: dict[str, int] | None = None,
) -> bool:
    """Mode 2: ralph++ controls each iteration with review between them.

    Updates *counters* ``{"iterations": …, "retries": …}`` in place so
    the caller can read them even if this function raises.
    """
    if counters is None:
        counters = {"iterations": 0, "retries": 0}
    orch: OrchestratedConfig = config.orchestrated
    strategy = "backout" if orch.backout_on_failure else "fixup"
    console.print(f"[bold cyan]\n── Orchestrated mode ({strategy}) ──[/bold cyan]")
    session_runner = _session_runner_path(config)

    # Phase 0: Setup
    _setup_worktree_files(worktree_path, story_filter=orch.story_filter or None)
    prd_json = worktree_path / "scripts" / "ralph" / "prd.json"
    if not prd_json.exists():
        raise FileNotFoundError(f"prd.json not found at {prd_json}")

    last_findings = ""
    consecutive_idle = 0
    consecutive_infra_failures = 0
    total_retries = 0
    # #127: stories that exhausted their retry budget. Excluded from future
    # iterations so independent downstream work can still make progress.
    skipped_story_ids: set[str] = set()
    prev_story_status = read_story_status(prd_json)

    # Apply story filter: treat non-filtered stories as already complete
    filter_set: set[str] | None = None
    if orch.story_filter:
        filter_set = set(orch.story_filter)
        unknown = filter_set - set(prev_story_status)
        if unknown:
            raise ValueError(
                f"Unknown story IDs in --story filter: {', '.join(sorted(unknown))}. "
                f"Valid IDs: {', '.join(sorted(prev_story_status))}"
            )
        for sid in prev_story_status:
            if sid not in filter_set:
                prev_story_status[sid] = True
        console.print(f"[cyan]Story filter active: {', '.join(sorted(filter_set))}[/cyan]")

    total_stories = len(prev_story_status)

    for iteration in range(1, config.ralph.max_iterations + 1):
        # Reset findings at the start of each outer iteration so that
        # stale context from a previous iteration does not suppress
        # legitimate findings in the current one (#32).
        last_findings = ""

        counters["iterations"] = iteration
        completed = sum(1 for v in prev_story_status.values() if v)
        console.print(
            f"\n[bold]═══ Iteration {iteration}/{config.ralph.max_iterations} "
            f"({completed}/{total_stories} stories done) ═══[/bold]"
        )

        # #127: Compute the story the coder is expected to pick this
        # iteration, so that on retry exhaustion we can mark *that* story
        # as skipped rather than guessing. Also lets us exit early when
        # every remaining story has been skipped.
        target_story_id = next_target_story(
            prd_json,
            excluded_ids=skipped_story_ids,
            story_filter=filter_set,
        )
        if target_story_id is None and skipped_story_ids:
            # We ran out of targets *because* stories were skipped. Exit so
            # the post-run review can surface them. Empty prd.json and
            # already-done states are handled by the existing completion
            # checks at the bottom of the loop.
            console.print(
                f"[yellow]All remaining stories have been skipped "
                f"({', '.join(sorted(skipped_story_ids))}) — finishing "
                "iteration loop[/yellow]"
            )
            break

        # Refresh CLAUDE.md with the latest skip list so the coder excludes
        # stories that previously exhausted their retry budget.
        _write_coder_prompt(
            worktree_path,
            story_filter=orch.story_filter or None,
            skipped_story_ids=skipped_story_ids or None,
        )

        pre_sha = get_head_sha(worktree_path)

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
        # #126: detect when consecutive retries cite essentially the same
        # reviewer findings — that means the coder has converged on a wrong
        # interpretation and continued retries will waste cycles without
        # making progress.
        prev_retry_findings = ""
        same_finding_count = 0
        converged_on_same_finding = False

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
                prompt_text = render_prompt(
                    orch.prompt_template,
                    iteration=str(iteration),
                    prd_file=str(prd_json),
                    progress=progress_text,
                    review_findings=_wrap_retry_findings(
                        last_findings, attempt, max_attempts, same_finding_count
                    ),
                )
                iter_prompt = worktree_path / "scripts" / "ralph" / ".iteration-prompt.md"
                iter_prompt.write_text(prompt_text)
                extra_env["RALPH_PROMPT_FILE"] = str(
                    worktree_path / "scripts" / "ralph" / ".iteration-prompt.md"
                )
            elif attempt > 1 and last_findings:
                # Default prompt flow: append review findings to CLAUDE.md so the
                # coder knows why its previous attempt was rejected.
                _write_coder_prompt(
                    worktree_path,
                    findings=_wrap_retry_findings(
                        last_findings, attempt, max_attempts, same_finding_count
                    ),
                    story_filter=orch.story_filter or None,
                    skipped_story_ids=skipped_story_ids or None,
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
            try:
                result = subprocess.run(
                    cmd,
                    text=True,
                    capture_output=True,
                    env=_merge_env(extra_env) if extra_env else None,
                    timeout=orch.coder_timeout or None,
                )
            except subprocess.TimeoutExpired:
                console.print(f"  [red]✗ Coder timed out after {orch.coder_timeout}s[/red]")
                result = subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="timeout")

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
                consecutive_infra_failures += 1
                if (
                    orch.max_consecutive_infra_failures > 0
                    and consecutive_infra_failures >= orch.max_consecutive_infra_failures
                ):
                    last_error = (result.stderr or result.stdout or "unknown").strip()
                    if len(last_error) > 200:
                        last_error = last_error[:200] + "..."
                    console.print(
                        f"[red]✗ {consecutive_infra_failures} consecutive infra "
                        f"failures — aborting (last error: {last_error})[/red]"
                    )
                    console.print(
                        "[yellow]Fix the underlying issue (auth, network, tool config) "
                        "and resume with --resume-worktree.[/yellow]"
                    )
                    return False
                break

            # Coder ran cleanly — reset the infra circuit-breaker counter.
            consecutive_infra_failures = 0

            # Check for completion signal — verify against prd.json
            if COMPLETE_SIGNAL in combined_output:
                commit_if_dirty(worktree_path, f"ralph: coder iteration {iteration}")
                story_status = read_story_status(prd_json)
                # When a story filter is active, only check filtered stories (#86)
                if filter_set:
                    relevant = {sid: v for sid, v in story_status.items() if sid in filter_set}
                else:
                    relevant = story_status
                if all(relevant.values()):
                    # #124: emit one final Progress line so the stream shows
                    # N/N before handing off to the post-run review. Without
                    # this, readers see the last in-loop count (e.g. 10/11)
                    # and only learn the full count from the final banner.
                    done_count = sum(1 for v in story_status.values() if v)
                    console.print(
                        f"  [dim]Progress: {done_count}/{total_stories} stories done[/dim]"
                    )
                    console.print("[green]Ralph signaled COMPLETE[/green]")
                    return True
                incomplete = [sid for sid, p in relevant.items() if not p]
                console.print(
                    f"[yellow]Ralph signaled COMPLETE but {len(incomplete)} stories "
                    f"still have passes=false: {', '.join(sorted(incomplete))} — "
                    "continuing iterations[/yellow]"
                )

            # Force-commit any uncommitted coder changes
            commit_if_dirty(worktree_path, f"ralph: coder iteration {iteration}")

            # Idle detection: if coder made no changes, it may have finished
            post_sha = get_head_sha(worktree_path)
            if post_sha == pre_sha:
                consecutive_idle += 1
                if consecutive_idle >= orch.max_idle_iterations:
                    # Verify at least one story completed before declaring success (#87)
                    idle_status = read_story_status(prd_json)
                    if filter_set:
                        idle_relevant = {s: v for s, v in idle_status.items() if s in filter_set}
                    else:
                        idle_relevant = idle_status
                    any_complete = any(idle_relevant.values())
                    if any_complete:
                        console.print(
                            f"[green]No changes for {consecutive_idle} consecutive "
                            "iterations — treating as complete[/green]"
                        )
                        return True
                    console.print(
                        f"[yellow]No changes for {consecutive_idle} consecutive "
                        "iterations but no stories are complete — treating as "
                        "failure[/yellow]"
                    )
                    return False
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

            # Review changes — scope to newly-completed stories only.
            # When no story was marked complete, tell the reviewer to
            # evaluate the diff on its own rather than scoping to all
            # incomplete stories (which would be noisy and misleading).
            if newly_completed:
                stories_text = format_stories(prd_json, newly_completed)
            else:
                stories_text = (
                    "(The coder made changes but did not mark any story "
                    "as complete. Review the diff on its own merits.)"
                )
            diff = get_diff(worktree_path, pre_sha)
            review = _review_iteration(
                iteration,
                diff,
                worktree_path,
                config,
                previous_findings=last_findings,
                stories_under_review=stories_text,
                test_results=test_results_text,
                first_review=True,
            )
            last_findings = review.findings

            if review.passed and not tests_failed:
                if review.minor_only and review.max_severity is not None:
                    console.print("  [dim]Minor findings carried forward[/dim]")
                # Reviewer owns the passes field (#129): strip any passes
                # flips the coder made for stories the reviewer did not
                # actually approve this iteration.
                reverted = enforce_passes_baseline(
                    prd_json, prev_story_status, approved=newly_completed
                )
                if reverted:
                    console.print(
                        f"  [yellow]Rolled back unauthorized passes flip for: "
                        f"{', '.join(sorted(reverted))}[/yellow]"
                    )
                iteration_passed = True
                break
            elif tests_failed and review.passed:
                console.print(
                    "  [yellow]Reviewer approved but tests failed — not accepting[/yellow]"
                )
                last_findings = "Tests failed. " + review.findings

            # Handle review failure.
            # Reviewer owns the passes field (#129): before we backout or
            # invoke the fixer, revert any passes flips the coder made this
            # iteration. Backout mode also relies on restore_files, but
            # enforcing the baseline here keeps fix-in-place mode safe too.
            reverted_after_reject = enforce_passes_baseline(prd_json, prev_story_status)
            if reverted_after_reject:
                console.print(
                    f"  [yellow]Rolled back unauthorized passes flip for: "
                    f"{', '.join(sorted(reverted_after_reject))}[/yellow]"
                )

            # #126: same-finding convergence detection. Compare the current
            # rejection's findings against the previous retry's; if they are
            # essentially the same, bump the counter and maybe escalate /
            # bail out.
            if prev_retry_findings:
                sim = findings_similarity(prev_retry_findings, review.findings)
                if sim >= orch.same_finding_similarity_threshold:
                    same_finding_count += 1
                    console.print(
                        f"  [yellow]⚠ Same finding detected {same_finding_count + 1}x "
                        f"in a row (similarity {sim:.2f}) — coder may be converging "
                        f"on a wrong interpretation[/yellow]"
                    )
                else:
                    same_finding_count = 0
            prev_retry_findings = review.findings

            if (
                orch.max_same_finding_retries > 0
                and same_finding_count >= orch.max_same_finding_retries
            ):
                console.print(
                    f"  [red]✗ Reviewer cited the same finding "
                    f"{same_finding_count + 1} times in a row — stopping "
                    f"retries for iteration {iteration} to avoid wasted cycles[/red]"
                )
                converged_on_same_finding = True
                # Fall through to the exhaustion handler below.

            # Handle review failure

            if orch.backout_on_failure:
                # PATH A: Backout and retry
                if attempt < max_attempts and not converged_on_same_finding:
                    total_retries += 1
                    counters["retries"] = total_retries
                    _backout_to(worktree_path, pre_sha, restore_files=restore_files)
                else:
                    # #126 + #127: log convergence vs plain exhaustion, then
                    # honor the on_retry_exhaustion policy (skip-story or abort).
                    if converged_on_same_finding:
                        console.print(
                            f"  [red]✗ Iteration {iteration} converged on the same finding[/red]"
                        )
                    else:
                        console.print(
                            f"  [red]✗ All retries exhausted for iteration {iteration}[/red]"
                        )
                    if orch.on_retry_exhaustion == "skip-story" and target_story_id:
                        _skip_story(
                            worktree_path,
                            target_story_id,
                            iteration,
                            review.findings,
                            skipped_story_ids,
                            pre_sha=pre_sha,
                            restore_files=restore_files,
                        )
                        break  # exit attempt loop, outer loop advances
                    console.print("  [red]Aborting iteration loop[/red]")
                    return False
            else:
                # PATH B: Invoke fixer to fix in-place
                prev_fix_findings: str = ""
                escalation_count = 0
                for fix_cycle in range(1, orch.max_iteration_retries + 1):
                    total_retries += 1
                    counters["retries"] = total_retries
                    console.print(
                        f"  [dim]Fix cycle {fix_cycle}/{orch.max_iteration_retries}[/dim]"
                    )
                    pre_fix_sha = get_head_sha(worktree_path)
                    fixer_result = _run_fixer_in_sandbox(
                        review.findings, worktree_path, config, stories_text
                    )
                    if fixer_result.returncode != 0:
                        console.print(
                            f"  [red]✗ Fixer process failed (exit {fixer_result.returncode})[/red]"
                        )
                        break

                    # Force-commit any uncommitted fixer changes
                    commit_if_dirty(
                        worktree_path,
                        f"ralph: fixer cycle {fix_cycle} iteration {iteration}",
                    )
                    fix_diff = get_diff(worktree_path, pre_fix_sha)

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
                    diff = get_diff(worktree_path, pre_sha)
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

                    # Detect escalating findings — new issues each cycle
                    # suggests a spec-level problem, not an implementation bug
                    if prev_fix_findings and review.findings != prev_fix_findings:
                        escalation_count += 1
                    prev_fix_findings = review.findings

                    if escalation_count >= 2:
                        console.print(
                            f"\n  [bold yellow]⚠ Fix cycles for iteration {iteration} "
                            f"show escalating findings (new issues each cycle)."
                            f"[/bold yellow]\n"
                            f"  [yellow]This suggests a spec-level issue, not an "
                            f"implementation bug.[/yellow]"
                        )
                        action = click.prompt(
                            "  Choose [skip/continue/quit]",
                            type=click.Choice(["skip", "continue", "quit"]),
                            default="skip",
                        )
                        if action == "quit":
                            console.print("  [red]Aborting by user request[/red]")
                            return False
                        if action == "skip":
                            console.print(
                                f"  [yellow]Skipping iteration {iteration} — "
                                f"continuing to next story[/yellow]"
                            )
                            break
                        # action == "continue" → keep going through remaining cycles

                if not iteration_passed:
                    if orch.on_retry_exhaustion == "skip-story" and target_story_id:
                        console.print(
                            f"  [red]✗ Fix cycles exhausted for iteration {iteration}[/red]"
                        )
                        _skip_story(
                            worktree_path,
                            target_story_id,
                            iteration,
                            review.findings,
                            skipped_story_ids,
                            # No backout in fix-in-place mode
                            pre_sha=None,
                            restore_files=None,
                        )
                        break  # exit attempt loop, outer loop advances
                    console.print(
                        f"  [red]✗ Fix cycles exhausted for iteration {iteration} — aborting[/red]"
                    )
                    return False
                break  # In fix-in-place mode we don't retry the coder, only the fixer

        # Update story status for next iteration
        prev_story_status = read_story_status(prd_json)
        # Re-apply story filter so non-targeted stories stay marked complete (#86)
        if filter_set:
            for sid in prev_story_status:
                if sid not in filter_set:
                    prev_story_status[sid] = True

        updated_completed = sum(1 for v in prev_story_status.values() if v)
        skipped_note = f" ({len(skipped_story_ids)} skipped)" if skipped_story_ids else ""
        console.print(
            f"  [dim]Progress: {updated_completed}/{total_stories} stories done{skipped_note}[/dim]"
        )

        # Append to progress (skip idle iterations — the coder writes its own
        # detailed entries via the orchestrated prompt)
        if consecutive_idle == 0:
            progress_file = worktree_path / "scripts" / "ralph" / "progress.txt"
            progress_file.parent.mkdir(parents=True, exist_ok=True)
            status = "passed" if iteration_passed else "failed"
            with open(progress_file, "a") as f:
                f.write(f"\n## Iteration {iteration} — {status}\n---\n")

        # Early termination: if every non-skipped story is complete,
        # skip remaining iterations instead of waiting for the coder to emit
        # a COMPLETE signal (#92 + #127). Skipped stories are considered
        # "handled" for the purposes of loop termination — the post-run review
        # will surface them.
        remaining = {
            sid: done for sid, done in prev_story_status.items() if sid not in skipped_story_ids
        }
        if iteration_passed and remaining and all(remaining.values()):
            if skipped_story_ids:
                console.print(
                    f"[green]All non-skipped stories complete "
                    f"({len(skipped_story_ids)} skipped: "
                    f"{', '.join(sorted(skipped_story_ids))}) — finishing[/green]"
                )
            else:
                console.print("[green]All stories complete — finishing early[/green]")
            return True

    if skipped_story_ids:
        console.print(
            f"[yellow]Reached max iterations ({config.ralph.max_iterations}); "
            f"{len(skipped_story_ids)} stories skipped: "
            f"{', '.join(sorted(skipped_story_ids))}[/yellow]"
        )
    else:
        console.print(
            f"[yellow]Reached max iterations ({config.ralph.max_iterations}) "
            "without completion signal[/yellow]"
        )
    # Partial progress counts as success if anything was completed — the
    # post-run review will then surface incomplete work. The caller only
    # treats False as an infrastructure abort.
    any_done = any(v for v in prev_story_status.values())
    return any_done
