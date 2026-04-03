"""Main workflow orchestrator — runs all steps in sequence."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from .config import Config
from .hooks import run_hooks
from .skills import ensure_prd_skills
from .steps.post_review import post_review_loop
from .steps.prd import convert_prd_to_json, generate_prd, review_prd_loop
from .steps.sandbox import run_sandbox
from .steps.worktree import cleanup_git_config, create_worktree

console = Console()


class Orchestrator:
    def __init__(self, feature: str, config: Config, dry_run: bool = False) -> None:
        self.feature = feature
        self.config = config
        self.dry_run = dry_run
        self.worktree_path: Path | None = None
        self.branch: str | None = None

    def run(
        self,
        skip_prd_review: bool = False,
        skip_post_review: bool = False,
    ) -> None:
        title = "ralph++\nFeature: " + self.feature
        console.print(Panel.fit(title, border_style="bright_blue"))

        if self.dry_run:
            console.print("[yellow]DRY RUN — no commands will be executed[/yellow]")
            return

        try:
            self._step_worktree()
            self._step_prd(skip_prd_review)
            self._step_sandbox()
            if not skip_post_review:
                self._step_post_review()
            self._step_cleanup()
        except Exception as exc:
            console.print("[bold red]\n✗ Workflow failed:[/bold red] " + str(exc))
            raise

        console.print(Rule(style="green"))
        summary = (
            "✓ ralph++ complete!\nBranch: "
            + str(self.branch)
            + "\nWorktree: "
            + str(self.worktree_path)
        )
        console.print(Panel.fit(summary, border_style="green"))

    # ── Steps ──────────────────────────────────────────────────────────

    def _step_worktree(self) -> None:
        console.print(Rule("[bold]1 · Worktree[/bold]"))
        self.worktree_path, self.branch = create_worktree(self.feature, self.config)
        run_hooks("post_worktree_create", self.config.hooks, self.worktree_path)

    def _step_prd(self, skip_review: bool) -> None:
        assert self.worktree_path is not None
        console.print(Rule("[bold]2 · PRD[/bold]"))
        ensure_prd_skills(self.config, self.worktree_path)
        run_hooks("pre_prd_generate", self.config.hooks, self.worktree_path)
        prd_file = generate_prd(self.feature, self.worktree_path, self.config)
        run_hooks("post_prd_generate", self.config.hooks, self.worktree_path)
        if not skip_review:
            review_prd_loop(prd_file, self.worktree_path, self.config)
        convert_prd_to_json(prd_file, self.worktree_path, self.config)

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
        success = run_sandbox(self.worktree_path, self.config)
        run_hooks("post_sandbox", self.config.hooks, self.worktree_path)
        if not success:
            console.print(
                "[yellow]⚠ Ralph did not signal COMPLETE — "
                "continuing to post-review anyway[/yellow]"
            )

    def _step_post_review(self) -> None:
        assert self.worktree_path is not None
        console.print(Rule("[bold]4 · Post-Run Review[/bold]"))
        post_review_loop(self.worktree_path, self.config)

    def _step_cleanup(self) -> None:
        assert self.worktree_path is not None
        console.print(Rule("[bold]5 · Cleanup[/bold]"))
        cleanup_git_config(self.worktree_path)
        run_hooks("post_complete", self.config.hooks, self.worktree_path)
