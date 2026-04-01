"""Configuration loading and validation for ralph++."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml


@dataclass
class ToolConfig:
    type: str = "cli"  # cli | shell
    command: str = ""
    args: list[str] = field(default_factory=lambda: list[str]())
    stdin: str | None = None  # template string sent via stdin
    env: dict[str, str] = field(default_factory=lambda: dict[str, str]())


@dataclass
class ReviewConfig:
    """Base review config — prefer PrdReviewConfig or PostReviewConfig."""

    reviewer: str = "codex"
    reviewer_prompt: str = ""
    fixer: str = "claude"
    fixer_prompt: str = ""
    max_cycles: int = 3
    enabled: bool = True


@dataclass
class PrdReviewConfig(ReviewConfig):
    reviewer_prompt: str = (
        "Read the PRD at {prd_file}. Evaluate whether it is complete, unambiguous, "
        "and implementable as a series of independent user stories. List any flaws, "
        "omissions, or areas for improvement. If the PRD is fully satisfactory, "
        "output exactly: LGTM"
    )
    fixer_prompt: str = (
        "The following issues were found in the PRD at {prd_file}.\nPlease fix them:\n\n{findings}"
    )


@dataclass
class PostReviewConfig(ReviewConfig):
    reviewer_prompt: str = (
        "Read prd.json and review the entire implementation. Does the code fully "
        "satisfy every user story? List any flaws, omissions or areas for "
        "improvement. If fully satisfied, output exactly: LGTM"
    )
    fixer_prompt: str = (
        "The following issues were found in the final implementation.\n"
        "Please fix them and ensure all tests pass:\n\n{findings}"
    )


@dataclass
class RalphConfig:
    max_iterations: int = 20
    mode: str = "delegated"  # "delegated" | "orchestrated"
    sandbox_dir: str = ""  # path to ralph-sandbox checkout
    sandbox_tool: str = "claude"  # tool for sandbox (delegated mode): claude | codex
    session_runner: str = "scripts/ralph-single-step.sh"  # session runner for orchestrated mode


@dataclass
class OrchestratedConfig:
    coder: str = "claude"
    reviewer: str = "codex"
    fixer: str = "claude"
    max_iteration_retries: int = 2
    run_tests_between_steps: bool = False
    test_commands: list[str] = field(default_factory=lambda: list[str]())
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
    tools: dict[str, ToolConfig] = field(default_factory=lambda: dict[str, ToolConfig]())
    prd_tool: str = "claude"  # tool for PRD generation and conversion

    # Review stages
    prd_review: PrdReviewConfig = field(default_factory=PrdReviewConfig)
    post_review: PostReviewConfig = field(default_factory=PostReviewConfig)

    # Ralph
    ralph: RalphConfig = field(default_factory=RalphConfig)

    # Orchestrated mode
    orchestrated: OrchestratedConfig = field(default_factory=OrchestratedConfig)

    # Hooks
    hooks: dict[str, list[str]] = field(default_factory=lambda: dict[str, list[str]]())

    def get_tool(self, name: str) -> ToolConfig:
        if name not in self.tools:
            raise ValueError(
                f"Tool '{name}' not defined in config. Available: {list(self.tools.keys())}"
            )
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


_RC = Any  # TypeVar would be cleaner but not worth the import for internal use


def _parse_review(data: dict[str, Any], defaults: _RC) -> _RC:
    return type(defaults)(
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


def discover_config_files(repo_path: Path | None = None) -> list[Path]:
    """Return config files in load order: user config first, then project config.

    User config: ``~/.config/ralph-plus-plus/config.yaml`` or ``~/.ralph++.yaml``.
    Project config: ``<repo>/.ralph/ralph++.yaml``, ``<repo>/ralph++.yaml``,
    or ``<repo>/ralph++.yml`` (first found).
    """
    paths: list[Path] = []

    # Layer 1: user config
    xdg_config = Path("~/.config/ralph-plus-plus/config.yaml").expanduser()
    legacy_user = Path("~/.ralph++.yaml").expanduser()
    if xdg_config.is_file():
        paths.append(xdg_config)
    elif legacy_user.is_file():
        paths.append(legacy_user)

    # Layer 2: project config (relative to repo_path, not CWD)
    repo = (repo_path or Path.cwd()).resolve()
    project_candidates = [
        repo / ".ralph" / "ralph++.yaml",
        repo / "ralph++.yaml",
        repo / "ralph++.yml",
    ]
    for candidate in project_candidates:
        if candidate.is_file():
            paths.append(candidate)
            break

    return paths


def _check_sandbox(path: Path) -> None:
    """Verify that a sandbox directory contains ``bin/ralph-sandbox``."""
    wrapper = path / "bin" / "ralph-sandbox"
    if not wrapper.is_file():
        raise FileNotFoundError(f"ralph-sandbox wrapper not found at {wrapper}")


def resolve_sandbox_dir(config: Config) -> Path:
    """Resolve the ralph-sandbox checkout directory.

    Resolution order:
    1. ``config.ralph.sandbox_dir`` (explicit config / CLI)
    2. ``RALPH_SANDBOX_DIR`` environment variable
    3. ``ralph-sandbox`` on ``PATH`` (via ``shutil.which``)
    4. Sibling checkout relative to ``config.repo_path``
    """
    # 1. Explicit config value
    if config.ralph.sandbox_dir:
        resolved = Path(config.ralph.sandbox_dir).expanduser().resolve()
        _check_sandbox(resolved)
        return resolved

    # 2. Environment variable
    env_dir = os.environ.get("RALPH_SANDBOX_DIR")
    if env_dir:
        resolved = Path(env_dir).expanduser().resolve()
        _check_sandbox(resolved)
        return resolved

    # 3. PATH lookup
    which_result = shutil.which("ralph-sandbox")
    if which_result:
        # ralph-sandbox lives at <sandbox_dir>/bin/ralph-sandbox
        resolved = Path(which_result).resolve().parent.parent
        if (resolved / "bin" / "ralph-sandbox").is_file():
            return resolved

    # 4. Sibling checkout (dev superproject layout)
    sibling = (config.repo_path / ".." / "ralph-sandbox").resolve()
    if (sibling / "bin" / "ralph-sandbox").is_file():
        return sibling

    raise FileNotFoundError(
        "Could not find ralph-sandbox. Set one of:\n"
        "  - ralph.sandbox_dir in config\n"
        "  - RALPH_SANDBOX_DIR environment variable\n"
        "  - Add ralph-sandbox/bin to PATH\n"
        "  - Place ralph-sandbox as a sibling of the repo"
    )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base*. Lists and scalars are replaced."""
    result = base.copy()
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = _deep_merge(cast(dict[str, Any], existing), cast(dict[str, Any], value))
        else:
            result[key] = value
    return result


def load_config(
    paths: list[Path] | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
    """Load config from one or more YAML files with optional CLI overrides.

    Files are merged in order (later wins). *overrides* are applied last.
    """
    if paths is None:
        path_list: list[Path] = []
    elif isinstance(paths, Path):
        path_list = [paths]
    else:
        path_list = list(paths)

    data: dict[str, Any] = {}
    for p in path_list:
        if p and p.exists():
            with open(p) as f:
                layer: dict[str, Any] = yaml.safe_load(f) or {}
            data = _deep_merge(data, layer)

    if overrides:
        cleaned = {k: v for k, v in overrides.items() if v is not None}
        data = _deep_merge(data, cleaned)

    cfg = Config()

    if "repo_path" in data:
        cfg.repo_path = _expand(data["repo_path"])
    if "claude_config_dir" in data:
        cfg.claude_config_dir = _expand(data["claude_config_dir"])
    if "codex_config_dir" in data:
        cfg.codex_config_dir = _expand(data["codex_config_dir"])

    cfg.branch_prefix = data.get("branch_prefix", cfg.branch_prefix)
    cfg.branch_suffix_length = int(data.get("branch_suffix_length", cfg.branch_suffix_length))
    cfg.prd_tool = data.get("prd_tool", cfg.prd_tool)

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
            max_iteration_retries=int(
                o.get("max_iteration_retries", defaults.max_iteration_retries)
            ),
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

    if cfg.prd_tool not in cfg.tools:
        errors.append(f"prd_tool={cfg.prd_tool!r} not in tools {list(cfg.tools)}")

    if cfg.ralph.sandbox_tool not in cfg.tools:
        errors.append(
            f"ralph.sandbox_tool={cfg.ralph.sandbox_tool!r} not in tools {list(cfg.tools)}"
        )

    for attr in ("coder", "reviewer", "fixer"):
        name = getattr(cfg.orchestrated, attr)
        if name not in cfg.tools:
            errors.append(f"orchestrated.{attr}={name!r} not in tools {list(cfg.tools)}")

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
            errors.append(f"{stage_name}.fixer={review_cfg.fixer!r} not in tools {list(cfg.tools)}")

    if not isinstance(cfg.orchestrated.test_commands, list):
        errors.append(
            "orchestrated.test_commands must be a list, "
            f"got {type(cfg.orchestrated.test_commands).__name__}"
        )
    elif cfg.orchestrated.run_tests_between_steps:
        for i, cmd in enumerate(cfg.orchestrated.test_commands):
            if not isinstance(cmd, str) or not cmd.strip():
                errors.append(f"orchestrated.test_commands[{i}] is empty or not a string")

    if errors:
        raise ValueError("Config validation failed:\n  " + "\n  ".join(errors))


def detect_test_commands(repo_path: Path) -> list[str]:
    """Auto-detect test commands for common project types.

    Returns an empty list when detection is ambiguous or nothing is found.
    """
    commands: list[str] = []

    # Makefile with a 'test' target takes priority
    makefile = repo_path / "Makefile"
    if makefile.is_file():
        try:
            text = makefile.read_text()
            if "\ntest:" in text or "\ntest :" in text or text.startswith("test:"):
                commands.append("make test")
        except OSError:
            pass

    if not commands:
        # Python
        if (repo_path / "pytest.ini").is_file() or (repo_path / "setup.cfg").is_file():
            commands.append("pytest")
        elif (repo_path / "pyproject.toml").is_file():
            try:
                text = (repo_path / "pyproject.toml").read_text()
                if "[tool.pytest" in text:
                    commands.append("pytest")
            except OSError:
                pass

        # Node
        if (repo_path / "package.json").is_file():
            commands.append("npm test")

        # Rust
        if (repo_path / "Cargo.toml").is_file():
            commands.append("cargo test")

        # Go
        if (repo_path / "go.mod").is_file():
            commands.append("go test ./...")

    return commands


def format_effective_config(config: Config) -> str:
    """Return a human-readable YAML dump of the effective config."""
    import dataclasses

    def _convert(obj: Any) -> Any:
        if isinstance(obj, Path):
            return str(obj)
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return {f.name: _convert(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
        if isinstance(obj, dict):
            return {str(k): _convert(v) for k, v in cast(dict[str, Any], obj).items()}
        if isinstance(obj, list):
            return [_convert(v) for v in cast(list[Any], obj)]
        return obj

    data = _convert(config)
    return yaml.dump(data, default_flow_style=False, sort_keys=False)  # type: ignore[arg-type]
