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
    sandbox_image: str = "ralph-sandbox"
    ralph_script: str = "scripts/ralph/ralph-reviewed.sh"


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

    # Hooks
    hooks: dict[str, list[str]] = field(default_factory=dict)

    def get_tool(self, name: str) -> ToolConfig:
        if name not in self.tools:
            raise ValueError(f"Tool '{name}' not defined in config. Available: {list(self.tools.keys())}")
        return self.tools[name]


def _expand(path: Any) -> Path:
    return Path(str(path)).expanduser().resolve()


def _parse_review(data: dict[str, Any], defaults: ReviewConfig) -> ReviewConfig:
    return ReviewConfig(
        reviewer=data.get("reviewer", defaults.reviewer),
        reviewer_prompt=data.get("reviewer_prompt", defaults.reviewer_prompt),
        fixer=data.get("fixer", defaults.fixer),
        fixer_prompt=data.get("fixer_prompt", defaults.fixer_prompt),
        max_cycles=int(data.get("max_cycles", defaults.max_cycles)),
        enabled=bool(data.get("enabled", defaults.enabled)),
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
            sandbox_image=r.get("sandbox_image", "ralph-sandbox"),
            ralph_script=r.get("ralph_script", "scripts/ralph/ralph-reviewed.sh"),
        )

    cfg.hooks = data.get("hooks", {})

    return cfg
