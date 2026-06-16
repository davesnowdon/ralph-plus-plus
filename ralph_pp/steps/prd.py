"""PRD generation, review loop, prd.json conversion, and prd.json review."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Literal

import click
from rich.console import Console
from rich.markup import escape

from ..config import (
    Config,
    DesignStanceConfig,
    NonInteractiveConfig,
    OnMaxCycles,
    PrdReviewConfig,
)
from ..tools import make_tool
from ..tools.base import parse_max_severity, severity_at_or_above
from ._git import get_diff, get_head_sha
from ._prompts import render_prompt

console = Console()

MaxCyclesAction = Literal["quit", "retry", "continue", "explore"]


class MaxCyclesAbort(SystemExit):
    """Raised when the user chooses to quit after max review cycles."""

    def __init__(self) -> None:
        super().__init__("Review aborted by user after max cycles reached")


def is_non_interactive(non_interactive: NonInteractiveConfig | None = None) -> bool:
    """Return True when the workflow should skip stdin prompts.

    Detection order:
      1. ``non_interactive.enabled`` config flag (explicit opt-in)
      2. ``RALPH_NON_INTERACTIVE`` env var (any truthy value)
      3. stdin is not a TTY (CI, cron, piped runs)
    """
    if non_interactive is not None and non_interactive.enabled:
        return True
    env = os.environ.get("RALPH_NON_INTERACTIVE", "").strip().lower()
    if env and env not in ("0", "false", "no", ""):
        return True
    try:
        return not sys.stdin.isatty()
    except (AttributeError, ValueError):
        # No stdin (closed, or unusual test harness) — treat as non-interactive.
        return True


def _action_from_policy(policy: OnMaxCycles, retry_used: bool) -> MaxCyclesAction:
    """Map a non-interactive policy to a concrete action.

    ``retry-once`` returns ``"retry"`` the first time it is consulted for a
    given gate, then ``"continue"`` on subsequent calls.
    """
    if policy == "abort":
        return "quit"
    if policy == "continue":
        return "continue"
    # retry-once
    return "continue" if retry_used else "retry"


def prompt_max_cycles(
    phase: str,
    max_cycles: int,
    continue_label: str = "Continue — proceed without reviewer approval",
    *,
    non_interactive: NonInteractiveConfig | None = None,
    policy: OnMaxCycles | None = None,
    retry_used: bool = False,
    allow_explore: bool = False,
) -> MaxCyclesAction:
    """Prompt the user for action when max review cycles are exhausted.

    Returns one of: "quit", "retry", "continue", or "explore" (when
    *allow_explore* is True — see #120).

    In non-interactive mode (see :func:`is_non_interactive`) the function
    does not read stdin. Instead it applies *policy* (from the per-gate
    config) and logs the chosen action so unattended runs do not hang.
    The "explore" option is only offered when both *allow_explore* is True
    AND we are running interactively — it has no meaning unattended.
    """
    console.print(
        f"\n[yellow]⚠ {phase} review: max cycles ({max_cycles}) reached without LGTM[/yellow]"
    )

    if is_non_interactive(non_interactive):
        resolved_policy: OnMaxCycles = policy or "continue"
        action = _action_from_policy(resolved_policy, retry_used)
        console.print(
            f"[yellow]Non-interactive mode: applying policy '{resolved_policy}' → {action}[/yellow]"
        )
        return action

    options_text = (
        "[bold]Options:[/bold]\n"
        "  [cyan]1)[/cyan] Quit — abort the workflow\n"
        f"  [cyan]2)[/cyan] Retry — run another {max_cycles} review cycles\n"
        f"  [cyan]3)[/cyan] {continue_label}"
    )
    valid_choices = ["1", "2", "3"]
    if allow_explore:
        options_text += (
            "\n  [cyan]4)[/cyan] Explore — open an interactive session to "
            "edit the PRD, then run another review cycle"
        )
        valid_choices.append("4")
    console.print(options_text)
    choice = click.prompt(
        "Choose",
        type=click.Choice(valid_choices),
        default="3",
    )
    mapping: dict[str, MaxCyclesAction] = {
        "1": "quit",
        "2": "retry",
        "3": "continue",
        "4": "explore",
    }
    return mapping[choice]


def _launch_interactive_explore(
    phase: str,
    target_file: Path,
    findings: str,
    config: Config,
) -> None:
    """Drop the user into an interactive Claude session to explore *target_file*.

    Pre-loads the file path and last reviewer findings as context. Used by
    the PRD review loops when the user picks the "Explore" option (#120).

    Looks up an interactive tool by checking ``config.tools`` for any tool
    with ``interactive=True``, preferring ``claude-interactive`` if present.
    Raises ``RuntimeError`` when no interactive tool is configured.
    """
    interactive_tool_name: str | None = None
    if "claude-interactive" in config.tools and config.tools["claude-interactive"].interactive:
        interactive_tool_name = "claude-interactive"
    else:
        for name, tool_cfg in config.tools.items():
            if tool_cfg.interactive:
                interactive_tool_name = name
                break

    if interactive_tool_name is None:
        raise RuntimeError(
            "No interactive tool configured. Define a tool with `interactive: true` "
            "in your ralph++.yaml (e.g. 'claude-interactive') to use the Explore option."
        )

    tool = make_tool(interactive_tool_name, config)
    findings_block = ""
    if findings:
        findings_block = f"\n\nMost recent reviewer findings (verbatim):\n\n{findings.strip()}\n"
    prompt = (
        f"You are in an interactive review session for the {phase} at "
        f"{target_file}. Read the file, discuss its design with the user, "
        f"and edit it as needed. When you are done, type /exit to return "
        f"to the ralph++ review loop, which will then run another review "
        f"cycle to verify your changes.{findings_block}"
    )
    console.print(
        f"\n[bold yellow]Opening interactive session ({interactive_tool_name}). "
        "Type /exit when done.[/bold yellow]\n"
    )
    result = tool.run(prompt=prompt, cwd=target_file.parent)
    if not result.success:
        console.print(
            f"[red]Interactive session ended with non-zero exit code ({result.exit_code})[/red]"
        )


def feature_to_slug(feature: str) -> str:
    """Convert a feature description to a kebab-case slug for filenames."""
    slug = feature.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _normalize_findings(text: str) -> set[str]:
    """Lowercased token set with short tokens dropped, used for similarity."""
    if not text:
        return set()
    return {tok.lower() for tok in _TOKEN_RE.findall(text) if len(tok) >= 3}


def findings_jaccard(a: str, b: str) -> float:
    """Token-set Jaccard similarity. 0.0 when either is empty.

    Used by the PRD review loop (#118) to detect when consecutive cycles
    produce essentially the same findings — a signal that we've hit
    diminishing returns and should stop iterating.
    """
    tokens_a = _normalize_findings(a)
    tokens_b = _normalize_findings(b)
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


_CODEBASE_CONTEXT_INSTRUCTION = """\

## Read the existing codebase first

Before specifying contracts or acceptance criteria, read the existing
codebase at {repo_path} to understand current types, schemas, and
constraints. Pay particular attention to:
- Dataclass / Protocol / Pydantic / TypedDict definitions referenced by the PRD
- Database schema (SQL DDL, ORM models, migrations) and column constraints
- Nullable vs non-nullable fields
- Existing test fixtures that construct the types you're specifying
- Public API surfaces that callers depend on

Acceptance criteria you write must be satisfiable against the actual code,
not against an idealized version of it. If a criterion would require
changing an existing type or schema in a way that breaks callers, call
that out explicitly in the PRD's "Constraints" or "Risks" section.
"""


def _build_design_stance_block(stance: DesignStanceConfig | None) -> str:
    """Render the design-stance answers as constraints for the generator (#121).

    Returns an empty string when *stance* is None or all fields are unset.
    """
    if stance is None:
        return ""
    parts: list[str] = []
    if stance.implementation_scope == "single_pass":
        parts.append(
            "- This PRD covers a SINGLE implementation pass — all phases will "
            "be implemented together. Do NOT introduce transitional wrappers, "
            "adapters, or compatibility shims between phases. Design each phase "
            "assuming all previous phases are already complete."
        )
    elif stance.implementation_scope == "incremental":
        parts.append(
            "- This PRD will be implemented incrementally — phases may ship "
            "independently. Design transitional contracts that let earlier "
            "phases run before later ones land."
        )

    if stance.backward_compatibility == "required":
        parts.append(
            "- Backward compatibility is REQUIRED. New code must read/write "
            "data created by old code. Schema migrations must preserve "
            "existing rows. Do not redesign storage in ways that strand old data."
        )
    elif stance.backward_compatibility == "not_required":
        parts.append(
            "- Backward compatibility is NOT required. Storage layouts and "
            "data formats may be redesigned freely."
        )

    if stance.existing_tests == "must_pass":
        parts.append(
            "- ALL existing tests must continue to pass without modification. "
            "Constrain contract changes to be backward-compatible with current "
            "test expectations."
        )
    elif stance.existing_tests == "can_update":
        parts.append("- Existing tests MAY be updated as part of this work.")

    if stance.api_stability == "extend_only":
        parts.append(
            "- The public API may only EXTEND with optional parameters. "
            "Do not change signatures of existing public functions/methods "
            "in a breaking way."
        )
    elif stance.api_stability == "can_break":
        parts.append("- The public API may be changed in breaking ways.")

    if stance.notes:
        parts.append(f"- Additional design constraints: {stance.notes}")

    if not parts:
        return ""
    return (
        "\n## Design constraints\n\n"
        "Incorporate the following design-stance answers as hard constraints "
        "throughout the PRD:\n\n" + "\n".join(parts) + "\n"
    )


def generate_prd(
    feature: str,
    worktree_path: Path,
    config: Config,
    *,
    manual: bool = False,
    prd_prompt: str | None = None,
    repo_path: Path | None = None,
) -> Path:
    """
    Invoke the Claude /prd skill to generate a text PRD.
    Returns the path to the generated PRD markdown file.

    When *manual* is True the feature description is not sent as a prompt,
    allowing the user to drive the conversation interactively.

    When *prd_prompt* is provided it is used as the generation prompt instead
    of the short *feature* string.  This allows a richer description while
    keeping *feature* short for branch/worktree naming.

    When *repo_path* is provided (and the prompt is non-manual), a standard
    "read the codebase first" instruction is appended so the generator
    grounds its acceptance criteria in real types/schemas (#117).
    """
    console.print("[bold cyan]\n── Step: Generate PRD ──[/bold cyan]")
    slug = feature_to_slug(feature)
    prd_filename = f"prd-{slug}.md"
    (worktree_path / "tasks").mkdir(exist_ok=True)
    tool = make_tool(config.prd_tool, config)

    description = prd_prompt if prd_prompt else feature

    if manual:
        prompt = f"Save the PRD to tasks/{prd_filename} when done."
    else:
        prompt = (
            f"Create a PRD for the following feature: {description}\n\n"
            "You have the /prd skill available. Use it to generate a detailed Product "
            "Requirements Document with goals, user stories, acceptance criteria, "
            "non-goals, and technical considerations.\n\n"
            f"Save the PRD to tasks/{prd_filename}"
        )
        # #117: ground the generator in the actual codebase
        codebase_target = repo_path or config.repo_path
        if codebase_target:
            prompt += _CODEBASE_CONTEXT_INSTRUCTION.format(repo_path=str(codebase_target))
        # #121: inject design-stance answers as hard constraints
        prompt += _build_design_stance_block(config.design_stance)

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
    # #118: detect diminishing returns when consecutive cycles surface
    # essentially the same findings.
    convergence_threshold = 0.8
    retry_used = False
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

            max_sev = parse_max_severity(result.output)
            if max_sev and not severity_at_or_above(max_sev, "major"):
                console.print(
                    f"[green]✓ PRD review passed (only {max_sev} findings remain)[/green]"
                )
                console.print(f"[dim]{escape(result.output or '')}[/dim]")
                return

            # #118: convergence detection. If the new findings are
            # essentially the same as the previous cycle's, accept early —
            # the reviewer has stopped making forward progress and further
            # cycles will only produce marginal refinements.
            if previous_findings:
                similarity = findings_jaccard(previous_findings, result.output)
                if similarity >= convergence_threshold:
                    console.print(
                        f"[yellow]⚠ PRD review cycle {total_cycles} produced findings "
                        f"~{int(similarity * 100)}% similar to the previous cycle — "
                        "accepting (diminishing returns)[/yellow]"
                    )
                    console.print(f"[dim]{escape(result.output or '')}[/dim]")
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
            if not last_fixer_diff or last_fixer_diff.strip() == "(no diff)":
                console.print(
                    "[yellow]⚠ Fixer produced no changes — findings may be "
                    "spec-level issues that cannot be fixed automatically[/yellow]"
                )
                last_fixer_diff = ""

        action = prompt_max_cycles(
            "PRD",
            review_cfg.max_cycles,
            non_interactive=config.non_interactive,
            policy=config.non_interactive.on_max_cycles_prd,
            retry_used=retry_used,
            allow_explore=True,
        )
        if action == "quit":
            raise MaxCyclesAbort
        if action == "continue":
            console.print("[yellow]Continuing without reviewer approval[/yellow]")
            return
        if action == "explore":
            # #120: drop into an interactive session, then run another batch
            try:
                _launch_interactive_explore(
                    phase="PRD",
                    target_file=prd_file,
                    findings=previous_findings,
                    config=config,
                )
            except RuntimeError as e:
                console.print(f"[red]{e}[/red]")
                console.print("[yellow]Falling back to retry[/yellow]")
            console.print(f"[cyan]Resuming review loop ({review_cfg.max_cycles} cycles)...[/cyan]")
            # Reset the per-batch context so the reviewer evaluates the
            # post-explore PRD on its own merits.
            previous_findings = ""
            last_fixer_diff = ""
            continue
        # action == "retry" → loop again
        retry_used = True
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

    When *max_cycles* is exhausted without LGTM the user is prompted to choose:
    quit, retry another batch of cycles, or continue anyway.
    """
    review_cfg = config.prd_json_review
    if not review_cfg.enabled:
        console.print("[dim]prd.json review disabled — skipping[/dim]")
        return

    console.print("[bold cyan]\n── Step: prd.json Review ──[/bold cyan]")
    reviewer = make_tool(review_cfg.reviewer, config)
    fixer = make_tool(review_cfg.fixer, config)

    total_cycles = 0
    retry_used = False
    while True:
        for cycle in range(1, review_cfg.max_cycles + 1):
            total_cycles += 1
            console.print(
                f"[bold]prd.json review cycle {total_cycles} "
                f"({cycle}/{review_cfg.max_cycles} this batch)[/bold]"
            )

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

            max_sev = parse_max_severity(result.output)
            if max_sev and not severity_at_or_above(max_sev, "major"):
                console.print(
                    f"[green]✓ prd.json review passed (only {max_sev} findings remain)[/green]"
                )
                console.print(f"[dim]{escape(result.output or '')}[/dim]")
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

        action = prompt_max_cycles(
            "prd.json",
            review_cfg.max_cycles,
            non_interactive=config.non_interactive,
            policy=config.non_interactive.on_max_cycles_prd_json,
            retry_used=retry_used,
            allow_explore=True,
        )
        if action == "quit":
            raise MaxCyclesAbort
        if action == "continue":
            console.print("[yellow]Continuing without prd.json reviewer approval[/yellow]")
            return
        if action == "explore":
            try:
                _launch_interactive_explore(
                    phase="prd.json",
                    target_file=prd_json,
                    findings="",
                    config=config,
                )
            except RuntimeError as e:
                console.print(f"[red]{e}[/red]")
                console.print("[yellow]Falling back to retry[/yellow]")
            console.print(f"[cyan]Resuming review loop ({review_cfg.max_cycles} cycles)...[/cyan]")
            continue
        retry_used = True
        console.print(f"[cyan]Retrying another {review_cfg.max_cycles} review cycles...[/cyan]")
