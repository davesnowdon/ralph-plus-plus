"""Configuration loading and validation for ralph++."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ToolConfig:
    type: str = "cli"          # cli | shell
    command: str = ""
    args: list[str] = field(default_factory=list)
    stdin: str | None = None   # template string sent via stdin
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class ReviewConfig:
    reviewer: str = "codex"
    reviewer_prompt: str = ""
    fixer: str = "claude"
    fixer_prompt: str = ""
    max_cycles: int = 3
    enabled: bool = True


@dataclass
class RalphConfig:
    max_iterations: int = 20
    mode: str = "delegated"              # "delegated" | "orchestrated"
    sandbox_dir: str = ""                # path to ralph-sandbox checkout
    sandbox_tool: str = "claude"         # tool for sandbox (delegated mode): claude | codex
    session_runner: str = "scripts/ralph-single-step.sh"  # session runner for orchestrated mode


@dataclass
class OrchestratedConfig:
    coder: str = "claude"
    reviewer: str = "codex"
    fixer: str = "claude"
    max_iteration_retries: int = 2
    run_tests_between_steps: bool = False
    test_commands: list[str] = field(default_factory=list)
    backout_on_failure: bool = True
    review_prompt: str = (
        "Review the following git diff against the requirements in {prd_file}.\n"
        "Identify any flaws, omissions, regressions, or test failures.\n"
        "If everything looks correct, output exactly: LGTM\n\n"
        "GIT DIFF:\n{diff}"
    )
    fix_prompt: str = (
        "The following issues were found in the latest code changes against {prd_file}.\n"
        "Please fix them and ensure all tests pass:\n\n{findings}"
    )
    prompt_template: str | None = None


@dataclass
class Config:
    # Paths
    repo_path: Path = field(default_factory=Path.cwd)
    claude_config_dir: Path = field(default_factory=lambda: Path("~/.claude").expanduser())
    codex_config_dir: Path = field(default_factory=lambda: Path("~/.codex").expanduser())

    # Branch naming
    branch_prefix: str = "ralph/"
    branch_suffix_length: int = 4

    # Tools
    tools: dict[str, ToolConfig] = field(default_factory=dict)

    # Review stages
    prd_review: ReviewConfig = field(default_factory=ReviewConfig)
    inner_review: ReviewConfig = field(default_factory=ReviewConfig)
    post_review: ReviewConfig = field(default_factory=ReviewConfig)

    # Ralph
    ralph: RalphConfig = field(default_factory=RalphConfig)

    # Orchestrated mode
    orchestrated: OrchestratedConfig = field(default_factory=OrchestratedConfig)

    # Hooks
    hooks: dict[str, list[str]] = field(default_factory=dict)

    def get_tool(self, name: str) -> ToolConfig:
        if name not in self.tools:
            raise ValueError(f"Tool '{name}' not defined in config. Available: {list(self.tools.keys())}")
        return self.tools[name]


def _expand(path: Any) -> Path:
    return Path(str(path)).expanduser().resolve()


def _parse_bool(value: Any, default: bool) -> bool:
    """Parse a boolean value, handling YAML string booleans correctly."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() in ("true", "yes", "1"):
            return True
        if value.lower() in ("false", "no", "0"):
            return False
        raise ValueError(f"Invalid boolean value: {value!r}")
    return bool(value)


def _parse_review(data: dict[str, Any], defaults: ReviewConfig) -> ReviewConfig:
    return ReviewConfig(
        reviewer=data.get("reviewer", defaults.reviewer),
        reviewer_prompt=data.get("reviewer_prompt", defaults.reviewer_prompt),
        fixer=data.get("fixer", defaults.fixer),
        fixer_prompt=data.get("fixer_prompt", defaults.fixer_prompt),
        max_cycles=int(data.get("max_cycles", defaults.max_cycles)),
        enabled=_parse_bool(data.get("enabled", defaults.enabled), defaults.enabled),
    )


def _parse_tools(data: dict[str, Any]) -> dict[str, ToolConfig]:
    tools: dict[str, ToolConfig] = {}
    for name, cfg in data.items():
        tools[name] = ToolConfig(
            type=cfg.get("type", "cli"),
            command=cfg.get("command", name),
            args=cfg.get("args", []),
            stdin=cfg.get("stdin"),
            env=cfg.get("env", {}),
        )
    return tools


def load_config(path: Path | None, overrides: dict[str, Any] | None = None) -> Config:
    """Load config from YAML file with optional CLI overrides."""
    data: dict[str, Any] = {}

    if path and path.exists():
        with open(path) as f:
            data = yaml.safe_load(f) or {}

    if overrides:
        data.update({k: v for k, v in overrides.items() if v is not None})

    cfg = Config()

    if "repo_path" in data:
        cfg.repo_path = _expand(data["repo_path"])
    if "claude_config_dir" in data:
        cfg.claude_config_dir = _expand(data["claude_config_dir"])
    if "codex_config_dir" in data:
        cfg.codex_config_dir = _expand(data["codex_config_dir"])

    cfg.branch_prefix = data.get("branch_prefix", cfg.branch_prefix)
    cfg.branch_suffix_length = int(data.get("branch_suffix_length", cfg.branch_suffix_length))

    if "tools" in data:
        cfg.tools = _parse_tools(data["tools"])
    else:
        # Sensible defaults if no tools section
        cfg.tools = {
            "codex": ToolConfig(type="cli", command="codex", args=["{prompt}"]),
            "claude": ToolConfig(
                type="cli",
                command="claude",
                args=["--dangerously-skip-permissions", "--print"],
                stdin="{prompt}",
            ),
        }

    if "prd_review" in data:
        cfg.prd_review = _parse_review(data["prd_review"], cfg.prd_review)
    if "inner_review" in data:
        cfg.inner_review = _parse_review(data["inner_review"], cfg.inner_review)
    if "post_review" in data:
        cfg.post_review = _parse_review(data["post_review"], cfg.post_review)

    if "ralph" in data:
        r = data["ralph"]
        cfg.ralph = RalphConfig(
            max_iterations=int(r.get("max_iterations", 20)),
            mode=r.get("mode", "delegated"),
            sandbox_dir=r.get("sandbox_dir", ""),
            sandbox_tool=r.get("sandbox_tool", "claude"),
            session_runner=r.get("session_runner", "scripts/ralph-single-step.sh"),
        )

    if "orchestrated" in data:
        o = data["orchestrated"]
        defaults = OrchestratedConfig()
        cfg.orchestrated = OrchestratedConfig(
            coder=o.get("coder", defaults.coder),
            reviewer=o.get("reviewer", defaults.reviewer),
            fixer=o.get("fixer", defaults.fixer),
            max_iteration_retries=int(o.get("max_iteration_retries", defaults.max_iteration_retries)),
            run_tests_between_steps=_parse_bool(
                o.get("run_tests_between_steps", defaults.run_tests_between_steps),
                defaults.run_tests_between_steps,
            ),
            test_commands=o.get("test_commands", defaults.test_commands),
            backout_on_failure=_parse_bool(
                o.get("backout_on_failure", defaults.backout_on_failure),
                defaults.backout_on_failure,
            ),
            review_prompt=o.get("review_prompt", defaults.review_prompt),
            fix_prompt=o.get("fix_prompt", defaults.fix_prompt),
            prompt_template=o.get("prompt_template", defaults.prompt_template),
        )

    cfg.hooks = data.get("hooks", {})

    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    """Validate config values that would otherwise cause confusing runtime errors."""
    errors: list[str] = []

    valid_modes = {"delegated", "orchestrated"}
    if cfg.ralph.mode not in valid_modes:
        errors.append(f"ralph.mode={cfg.ralph.mode!r} not in {valid_modes}")

    if cfg.ralph.sandbox_tool not in cfg.tools:
        errors.append(
            f"ralph.sandbox_tool={cfg.ralph.sandbox_tool!r} not in tools {list(cfg.tools)}"
        )

    for attr in ("coder", "reviewer", "fixer"):
        name = getattr(cfg.orchestrated, attr)
        if name not in cfg.tools:
            errors.append(
                f"orchestrated.{attr}={name!r} not in tools {list(cfg.tools)}"
            )

    for stage_name, review_cfg in [
        ("prd_review", cfg.prd_review),
        ("post_review", cfg.post_review),
    ]:
        if not review_cfg.enabled:
            continue
        if review_cfg.reviewer not in cfg.tools:
            errors.append(
                f"{stage_name}.reviewer={review_cfg.reviewer!r} not in tools {list(cfg.tools)}"
            )
        if review_cfg.fixer not in cfg.tools:
            errors.append(
                f"{stage_name}.fixer={review_cfg.fixer!r} not in tools {list(cfg.tools)}"
            )

    if errors:
        raise ValueError("Config validation failed:\n  " + "\n  ".join(errors))
