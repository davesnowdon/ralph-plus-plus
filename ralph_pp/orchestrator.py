"""Main workflow orchestrator — runs all steps in sequence."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from .config import Config
from .hooks import run_hooks
from .skills import ensure_prd_skills
from .steps.post_review import PostReviewResult, post_review_loop
from .steps.prd import (
    convert_prd_to_json,
    feature_to_slug,
    generate_prd,
    review_prd_json_loop,
    review_prd_loop,
)
from .steps.sandbox import RunSummary, run_sandbox, validate_sandbox_prerequisites
from .steps.worktree import (
    cleanup_git_config,
    cleanup_orchestration_artifacts,
    create_worktree,
    snapshot_local_config,
)

console = Console()


class Orchestrator:
    def __init__(
        self,
        feature: str,
        config: Config,
        dry_run: bool = False,
        resume_worktree: Path | None = None,
    ) -> None:
        self.feature = feature
        self.config = config
        self.dry_run = dry_run
        self.resume_worktree = resume_worktree
        self.worktree_path: Path | None = None
        self.branch: str | None = None
        self._baseline_config_keys: set[str] | None = None
        self._run_summary: RunSummary | None = None
        self._review_result: PostReviewResult | None = None

    def run(
        self,
        skip_prd_review: bool = False,
        skip_post_review: bool = False,
        prd_only: bool = False,
        prd_file: Path | None = None,
        manual_prd: bool = False,
    ) -> None:
        title = "ralph++\nFeature: " + self.feature
        console.print(Panel.fit(title, border_style="bright_blue"))

        if self.dry_run:
            self._print_dry_run_plan(
                skip_prd_review, skip_post_review, prd_only, prd_file, manual_prd
            )
            return

        start_time = time.monotonic()
        failed = False
        try:
            if prd_only:
                self._step_prd_only(skip_prd_review, manual_prd=manual_prd)
                return
            if self.resume_worktree:
                self._step_resume()
            else:
                # Validate sandbox prerequisites before creating the worktree
                # so we fail fast on misconfiguration (#16).
                validate_sandbox_prerequisites(self.config)
                self._step_worktree()
                if prd_file is not None:
                    self._step_prd_from_file(prd_file)
                else:
                    self._step_prd(skip_prd_review, manual_prd=manual_prd)
            self._step_sandbox()
            if not skip_post_review:
                self._step_post_review()
        except Exception as exc:
            failed = True
            console.print("[bold red]\n✗ Workflow failed:[/bold red] " + str(exc))
            raise
        finally:
            # Always attempt git config cleanup if we have a worktree
            if self.worktree_path:
                try:
                    self._step_cleanup()
                except Exception:
                    logging.getLogger(__name__).debug("Cleanup failed", exc_info=True)
            if failed and self.worktree_path:
                console.print(f"[yellow]Worktree preserved at:[/yellow] {self.worktree_path}")
                console.print(f"[yellow]Branch:[/yellow] {self.branch}")
                console.print(
                    f"[dim]Clean up manually with: git worktree remove {self.worktree_path}[/dim]"
                )
                try:
                    run_hooks("post_failure", self.config.hooks, self.worktree_path)
                except Exception:
                    logging.getLogger(__name__).debug("post_failure hook failed", exc_info=True)

        elapsed = time.monotonic() - start_time
        self._print_summary(elapsed, skip_post_review)

    # ── Steps ──────────────────────────────────────────────────────────

    def _step_resume(self) -> None:
        """Resume from an existing worktree — skip worktree creation and PRD."""
        assert self.resume_worktree is not None
        console.print(Rule("[bold]Resuming[/bold]"))

        # Validate sandbox prerequisites early so failures are clear (#75)
        validate_sandbox_prerequisites(self.config)

        wt = self.resume_worktree.resolve()
        if not wt.is_dir():
            raise FileNotFoundError(f"Worktree directory not found: {wt}")
        prd_json = wt / "scripts" / "ralph" / "prd.json"
        if not prd_json.exists():
            raise FileNotFoundError(f"prd.json not found in worktree: {prd_json}")
        self.worktree_path = wt
        # Snapshot config baseline so cleanup works on resume too
        self._baseline_config_keys = snapshot_local_config(wt)
        # Detect the branch from the worktree
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=wt,
            text=True,
            capture_output=True,
            check=True,
        )
        self.branch = result.stdout.strip()
        console.print(f"[green]✓ Resuming worktree:[/green] {wt}")
        console.print(f"[green]  Branch:[/green] {self.branch}")

    def _step_worktree(self) -> None:
        console.print(Rule("[bold]1 · Worktree[/bold]"))
        self.worktree_path, self.branch = create_worktree(self.feature, self.config)
        # Snapshot local config before hooks run, so cleanup can diff later
        self._baseline_config_keys = snapshot_local_config(self.worktree_path)
        run_hooks("post_worktree_create", self.config.hooks, self.worktree_path)

    def _step_prd_only(self, skip_review: bool, *, manual_prd: bool = False) -> None:
        """Generate (and optionally review) the text PRD, then stop."""
        base = self.config.repo_path
        console.print(Rule("[bold]PRD Only[/bold]"))
        ensure_prd_skills(self.config, base)
        run_hooks("pre_prd_generate", self.config.hooks, base)
        prd_file = generate_prd(self.feature, base, self.config, manual=manual_prd)
        run_hooks("post_prd_generate", self.config.hooks, base)
        if not skip_review:
            review_prd_loop(prd_file, base, self.config)

        console.print(Rule(style="green"))
        console.print(Panel.fit(prd_file.read_text(), title="PRD", border_style="cyan"))
        summary = (
            "✓ PRD generated!\n\n"
            f"File: {prd_file}\n\n"
            "To run implementation with this PRD:\n"
            f"  ralph++ --feature {self.feature!r} --prd-file {prd_file}"
        )
        console.print(Panel.fit(summary, border_style="green"))

    def _step_prd_from_file(self, prd_file: Path) -> None:
        """Copy an existing text PRD into the worktree and convert to JSON."""
        assert self.worktree_path is not None
        console.print(Rule("[bold]2 · PRD (from file)[/bold]"))
        slug = feature_to_slug(self.feature)
        dest = self.worktree_path / "tasks" / f"prd-{slug}.md"
        dest.parent.mkdir(exist_ok=True)
        shutil.copy2(prd_file, dest)
        console.print(f"[green]✓ PRD copied:[/green] {prd_file} → {dest}")
        prd_json = convert_prd_to_json(dest, self.worktree_path, self.config)
        review_prd_json_loop(dest, prd_json, self.worktree_path, self.config)

    def _step_prd(self, skip_review: bool, *, manual_prd: bool = False) -> None:
        assert self.worktree_path is not None
        console.print(Rule("[bold]2 · PRD[/bold]"))
        ensure_prd_skills(self.config, self.worktree_path)
        run_hooks("pre_prd_generate", self.config.hooks, self.worktree_path)
        prd_file = generate_prd(self.feature, self.worktree_path, self.config, manual=manual_prd)
        run_hooks("post_prd_generate", self.config.hooks, self.worktree_path)
        if not skip_review:
            review_prd_loop(prd_file, self.worktree_path, self.config)
        prd_json = convert_prd_to_json(prd_file, self.worktree_path, self.config)
        review_prd_json_loop(prd_file, prd_json, self.worktree_path, self.config)

    def _step_sandbox(self) -> None:
        assert self.worktree_path is not None
        mode = self.config.ralph.mode
        if mode == "orchestrated":
            strategy = "backout" if self.config.orchestrated.backout_on_failure else "fixup"
            label = f"{mode} mode, {strategy}"
        else:
            label = f"{mode} mode"
        console.print(Rule(f"[bold]3 · Ralph Sandbox ({label})[/bold]"))
        run_hooks("pre_sandbox", self.config.hooks, self.worktree_path)
        self._run_summary = run_sandbox(self.worktree_path, self.config)
        run_hooks("post_sandbox", self.config.hooks, self.worktree_path)
        if not self._run_summary.sandbox_ok:
            console.print(
                "[yellow]⚠ Ralph did not signal COMPLETE — "
                "continuing to post-review anyway[/yellow]"
            )

    def _step_post_review(self) -> None:
        assert self.worktree_path is not None
        console.print(Rule("[bold]4 · Post-Run Review[/bold]"))
        self._review_result = post_review_loop(self.worktree_path, self.config)

    def _step_cleanup(self) -> None:
        assert self.worktree_path is not None
        console.print(Rule("[bold]5 · Cleanup[/bold]"))
        cleanup_orchestration_artifacts(self.worktree_path)
        cleanup_git_config(self.worktree_path, self._baseline_config_keys)
        run_hooks("post_complete", self.config.hooks, self.worktree_path)

    def _print_summary(self, elapsed: float, skip_post_review: bool) -> None:
        console.print(Rule(style="green"))

        mins, secs = divmod(int(elapsed), 60)
        duration = f"{mins}m {secs:02d}s" if mins else f"{secs}s"

        lines = ["[bold green]✓ ralph++ complete![/bold green]", ""]

        if self._run_summary:
            s = self._run_summary
            lines.append(f"Mode:         {s.mode}")
            lines.append(f"Duration:     {duration}")
            lines.append(f"Iterations:   {s.iterations}")
            lines.append(f"Stories:      {s.stories_completed}/{s.stories_total} completed")
            if s.retries:
                lines.append(f"Retries:      {s.retries}")
            lines.append(f"SHA:          {s.base_sha[:7]} → {s.final_sha[:7]}")
        else:
            lines.append(f"Duration:     {duration}")

        if self._review_result:
            r = self._review_result
            if r.outcome == "lgtm":
                review_str = f"LGTM ({r.cycles} cycle{'s' if r.cycles != 1 else ''})"
            elif r.outcome == "accepted":
                review_str = f"accepted without approval ({r.cycles} cycles)"
            else:
                review_str = r.outcome
            lines.append(f"Post-review:  {review_str}")
        elif skip_post_review:
            lines.append("Post-review:  skipped")

        lines.append("")
        lines.append(f"Branch:       {self.branch}")
        lines.append(f"Worktree:     {self.worktree_path}")

        console.print(Panel.fit("\n".join(lines), border_style="green"))

    def _print_dry_run_plan(
        self,
        skip_prd_review: bool,
        skip_post_review: bool,
        prd_only: bool,
        prd_file: Path | None,
        manual_prd: bool,
    ) -> None:
        cfg = self.config
        mode = cfg.ralph.mode
        orch = cfg.orchestrated

        lines = ["[yellow]DRY RUN — no commands will be executed[/yellow]", ""]
        lines.append(f"Feature:       {self.feature}")
        lines.append(f"Repo:          {cfg.repo_path}")
        lines.append(f"Mode:          {mode}")
        lines.append(f"Max iterations: {cfg.ralph.max_iterations}")

        if prd_only:
            lines.append("\n[bold]Steps:[/bold]")
            lines.append("  1. Generate PRD" + (" (manual)" if manual_prd else ""))
            if not skip_prd_review:
                lines.append("  2. Review PRD loop")
            lines.append("  → Stop (--prd-only)")
        else:
            lines.append("\n[bold]Steps:[/bold]")
            lines.append("  1. Create worktree + branch")
            if prd_file:
                lines.append(f"  2. Copy PRD from: {prd_file}")
            else:
                lines.append("  2. Generate PRD" + (" (manual)" if manual_prd else ""))
                if not skip_prd_review:
                    lines.append("     + Review PRD loop")
            lines.append("  3. Convert PRD to prd.json")
            if mode == "orchestrated":
                strategy = "backout" if orch.backout_on_failure else "fixup"
                lines.append(f"  4. Orchestrated sandbox ({strategy})")
                lines.append(
                    f"     Coder: {orch.coder}  Reviewer: {orch.reviewer}  Fixer: {orch.fixer}"
                )
                if orch.test_commands:
                    lines.append(f"     Tests: {', '.join(orch.test_commands)}")
            else:
                lines.append(f"  4. Delegated sandbox (tool: {cfg.ralph.sandbox_tool})")
            if not skip_post_review:
                lines.append(f"  5. Post-run review loop (max {cfg.post_review.max_cycles} cycles)")
            lines.append("  6. Cleanup")

        hooks = cfg.hooks
        active_hooks = [k for k, v in hooks.items() if v]
        if active_hooks:
            lines.append(f"\n[bold]Active hooks:[/bold] {', '.join(active_hooks)}")

        console.print(Panel.fit("\n".join(lines), title="Execution Plan", border_style="yellow"))
