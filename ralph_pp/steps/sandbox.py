"""Docker sandbox invocation for the Ralph loop.

Supports two modes:
  - delegated: invoke ralph-sandbox with its built-in Ralph loop
  - orchestrated: ralph++ controls each iteration, reviewing between them
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from rich.console import Console

from ..config import Config, OrchestratedConfig
from ..sandbox import resolve_sandbox_dir
from ..tools import make_tool

console = Console()

_FALLBACK_CODER_PROMPT = """\
Read scripts/ralph/prd.json and implement the next smallest incomplete user story.

Requirements:
- make only the changes needed for that story and its acceptance criteria
- inspect the surrounding code before editing
- preserve existing behavior unless the story requires a change
- update or add tests when appropriate
- keep the repository in a coherent, runnable state
- commit your changes if your workflow expects it

If all stories are complete, output exactly:
<promise>COMPLETE</promise>

Do not output that completion signal unless every story in prd.json is complete.
"""

COMPLETE_SIGNAL = "<promise>COMPLETE</promise>"


def run_sandbox(worktree_path: Path, config: Config) -> bool:
    """Run the Ralph loop. Dispatches to delegated or orchestrated mode."""
    if config.ralph.mode == "orchestrated":
        return _run_orchestrated(worktree_path, config)
    return _run_delegated(worktree_path, config)


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
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git rev-parse HEAD failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _backout_to(worktree_path: Path, sha: str) -> None:
    console.print(f"[yellow]  Backing out to {sha[:8]}...[/yellow]")
    subprocess.run(
        ["git", "reset", "--hard", sha],
        cwd=worktree_path,
        check=True,
    )


def _run_test_commands(worktree_path: Path, commands: list[str]) -> bool:
    """Run test/lint commands. Returns True if all pass."""
    for cmd in commands:
        console.print(f"[dim]  $ {cmd}[/dim]")
        result = subprocess.run(cmd, shell=True, cwd=worktree_path)
        if result.returncode != 0:
            console.print(f"[red]  ✗ Command failed: {cmd}[/red]")
            return False
    return True


def _render_prompt(template: str, **kwargs: str) -> str:
    """Substitute placeholders in a prompt template."""
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", value)
    return result


def _get_diff(worktree_path: Path, from_sha: str) -> str:
    result = subprocess.run(
        ["git", "diff", from_sha],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed (exit {result.returncode}): {result.stderr.strip()}")
    return result.stdout or "(no diff)"


def _commit_if_dirty(worktree_path: Path, message: str) -> bool:
    """Stage and commit all changes if the working tree has uncommitted work.

    Returns True if a commit was created, False if tree was clean.
    """
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        raise RuntimeError(f"git status failed: {status.stderr.strip()}")
    if not status.stdout.strip():
        return False
    add = subprocess.run(
        ["git", "add", "-A"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        raise RuntimeError(f"git add failed: {add.stderr.strip()}")
    commit = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"git commit failed: {commit.stderr.strip()}")
    return True


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


def _setup_worktree_files(worktree_path: Path) -> None:
    """Ensure scripts/ralph/ has the required files for orchestrated mode.

    In custom-runner mode the sandbox entrypoint does not copy ralph.sh or
    CLAUDE.md, so ralph++ must set them up before the first iteration.
    """
    ralph_dir = worktree_path / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True, exist_ok=True)

    # Copy upstream CLAUDE.md from the ralph-sandbox image's /opt/ralph/
    # source — but since we're on the host, we look for it in the worktree
    # or fall back to a reasonable default prompt.
    claude_md = ralph_dir / "CLAUDE.md"
    if not claude_md.exists():
        # Check if there's a CLAUDE.md at the repo root we can use
        repo_claude = worktree_path / "CLAUDE.md"
        if repo_claude.exists():
            shutil.copy2(repo_claude, claude_md)
        else:
            claude_md.write_text(_FALLBACK_CODER_PROMPT)

    progress = ralph_dir / "progress.txt"
    if not progress.exists():
        progress.write_text("# Ralph Progress Log\nStarted: orchestrated mode\n---\n")


def _review_iteration(
    iteration: int,
    diff: str,
    worktree_path: Path,
    config: Config,
    previous_findings: str = "",
) -> tuple[bool, str]:
    """Run reviewer on the iteration diff. Returns (passed, findings)."""
    orch = config.orchestrated
    reviewer = make_tool(orch.reviewer, config)
    prd_file = str(worktree_path / "scripts" / "ralph" / "prd.json")

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

    review_prompt = _render_prompt(
        orch.review_prompt, diff=diff, prd_file=prd_file, previous_findings=context
    )
    result = reviewer.run(prompt=review_prompt, cwd=worktree_path)
    if not result.success:
        raise RuntimeError(
            f"Iteration reviewer failed (exit {result.exit_code}): {result.output[:200]}"
        )

    if result.is_lgtm:
        console.print(f"  [green]✓ Review passed (LGTM) — iteration {iteration}[/green]")
        return True, result.output

    console.print(f"  [yellow]Issues found in iteration {iteration}[/yellow]")
    return False, result.output


def _run_fixer_in_sandbox(
    findings: str,
    worktree_path: Path,
    config: Config,
) -> subprocess.CompletedProcess[str]:
    """Invoke the fixer agent inside the sandbox with the fix prompt."""
    orch = config.orchestrated
    prd_file = str(worktree_path / "scripts" / "ralph" / "prd.json")
    fix_prompt = _render_prompt(orch.fix_prompt, findings=findings, prd_file=prd_file)

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
    console.print("[bold cyan]\n── Orchestrated mode ──[/bold cyan]")

    orch: OrchestratedConfig = config.orchestrated
    session_runner = _session_runner_path(config)

    # Phase 0: Setup
    _setup_worktree_files(worktree_path)
    prd_json = worktree_path / "scripts" / "ralph" / "prd.json"
    if not prd_json.exists():
        raise FileNotFoundError(f"prd.json not found at {prd_json}")

    last_findings = ""

    for iteration in range(1, config.ralph.max_iterations + 1):
        console.print(f"\n[bold]═══ Iteration {iteration}/{config.ralph.max_iterations} ═══[/bold]")

        pre_sha = _get_head_sha(worktree_path)

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
                    review_findings=last_findings,
                )
                iter_prompt = worktree_path / "scripts" / "ralph" / ".iteration-prompt.md"
                iter_prompt.write_text(prompt_text)
                extra_env["RALPH_PROMPT_FILE"] = str(Path("scripts/ralph/.iteration-prompt.md"))

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
                    _backout_to(worktree_path, pre_sha)
                    continue
                console.print("  [red]Infra failure — skipping review[/red]")
                break

            # Check for completion signal
            if COMPLETE_SIGNAL in combined_output:
                console.print("[green]Ralph signaled COMPLETE[/green]")
                return True

            # Force-commit any uncommitted coder changes
            _commit_if_dirty(worktree_path, f"ralph: coder iteration {iteration}")

            # Run tests/linter (optional)
            tests_failed = False
            if orch.run_tests_between_steps and orch.test_commands:
                console.print("  [dim]Running test commands...[/dim]")
                tests_ok = _run_test_commands(worktree_path, orch.test_commands)
                if not tests_ok:
                    console.print("  [yellow]Tests failed — treating as review failure[/yellow]")
                    tests_failed = True
                    if orch.backout_on_failure and attempt < max_attempts:
                        _backout_to(worktree_path, pre_sha)
                        continue

            # Review changes
            diff = _get_diff(worktree_path, pre_sha)
            passed, findings = _review_iteration(
                iteration, diff, worktree_path, config, previous_findings=last_findings
            )
            last_findings = findings

            if passed and not tests_failed:
                iteration_passed = True
                break
            elif tests_failed and passed:
                console.print(
                    "  [yellow]Reviewer approved but tests failed — not accepting[/yellow]"
                )
                passed = False
                last_findings = "Tests failed. " + findings

            # Handle review failure
            if orch.backout_on_failure:
                # PATH A: Backout and retry
                if attempt < max_attempts:
                    _backout_to(worktree_path, pre_sha)
                else:
                    console.print(
                        f"  [red]✗ All retries exhausted for iteration {iteration} — aborting[/red]"
                    )
                    return False
            else:
                # PATH B: Invoke fixer to fix in-place
                for fix_cycle in range(1, orch.max_iteration_retries + 1):
                    console.print(
                        f"  [dim]Fix cycle {fix_cycle}/{orch.max_iteration_retries}[/dim]"
                    )
                    fixer_result = _run_fixer_in_sandbox(findings, worktree_path, config)
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

                    # Re-run tests after fix (if enabled)
                    if orch.run_tests_between_steps and orch.test_commands:
                        console.print("  [dim]Re-running test commands after fix...[/dim]")
                        if not _run_test_commands(worktree_path, orch.test_commands):
                            console.print("  [yellow]Tests still failing after fix[/yellow]")
                            continue

                    # Re-review after fix
                    diff = _get_diff(worktree_path, pre_sha)
                    passed, findings = _review_iteration(
                        iteration, diff, worktree_path, config, previous_findings=findings
                    )
                    last_findings = findings
                    if passed:
                        iteration_passed = True
                        break

                if not iteration_passed:
                    console.print(
                        f"  [red]✗ Fix cycles exhausted for iteration {iteration} — aborting[/red]"
                    )
                    return False
                break  # In fix-in-place mode we don't retry the coder, only the fixer

        # Append to progress
        progress_file = worktree_path / "scripts" / "ralph" / "progress.txt"
        status = "passed" if iteration_passed else "failed"
        with open(progress_file, "a") as f:
            f.write(f"\n## Iteration {iteration} — {status}\n---\n")

    console.print(
        f"[yellow]Reached max iterations ({config.ralph.max_iterations}) "
        "without completion signal[/yellow]"
    )
    return False
