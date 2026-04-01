"""Configuration loading and validation for ralph++."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from .detection import detect_test_commands

logger = logging.getLogger(__name__)

# ── default prompt constants ────────────────────────────────────────

_PRD_REVIEWER_PROMPT = """\
Read the PRD at {prd_file}.

Evaluate whether it is:
- complete
- unambiguous
- implementable as a sequence of small independent user stories
- testable with concrete acceptance criteria
- explicit about constraints, edge cases, and non-goals where relevant

If the PRD is fully satisfactory, output exactly:
LGTM

Otherwise, output a numbered list of issues.

For each issue include:
- severity: critical | major | minor
- section: the relevant PRD section or story
- problem: what is unclear, missing, inconsistent, or too broad
- consequence: why this would cause implementation or review problems
- recommended fix: the smallest concrete change that would resolve it

Do not rewrite the PRD yourself. Only review it."""

_PRD_FIXER_PROMPT = """\
The following issues were found in the PRD at {prd_file}:

{findings}

Revise the PRD in place to resolve these issues.

Requirements:
- preserve the original feature intent unless the findings require clarification
- keep stories small, implementation-ready, and independently testable
- add or tighten acceptance criteria where needed
- make ambiguities explicit rather than leaving them implied
- keep the document concise and structured
- do not add speculative scope that is not justified by the feature request or findings

Do not output a summary instead of making the edits. Update the PRD itself."""

_POST_REVIEWER_PROMPT = """\
Read the implementation and the requirements in {prd_file}.

Review whether the code fully satisfies every required story and acceptance criterion.

Check for:
- missing functionality
- incorrect behavior
- regressions
- edge-case handling
- obvious design or contract violations
- missing or inadequate tests
- mismatches between the implementation and the PRD

If everything looks correct, output exactly:
LGTM

Otherwise, output a numbered list of findings.

For each finding include:
- severity: critical | major | minor
- file: exact path(s) if applicable
- problem: what is wrong or missing
- evidence: the concrete mismatch, bug risk, or regression
- recommended fix: the smallest reasonable corrective action

Be specific. Do not give vague style feedback unless it affects
correctness or maintainability materially."""

_POST_FIXER_PROMPT = """\
The following issues were found in the final implementation against {prd_file}:

{findings}

Fix these issues in the codebase.

Requirements:
- address every finding unless two findings are duplicates
- preserve already-correct behavior
- avoid unrelated refactors
- add or update tests where needed
- keep changes minimal but sufficient
- ensure the implementation remains aligned with the PRD

Do not just describe the fixes. Make the code changes."""

_ORCHESTRATED_REVIEW_PROMPT = """\
Review the latest iteration against the requirements in {prd_file}.

Start from this git diff:

{diff}

You may inspect the changed files and nearby code as needed.

Check for:
- requirement mismatches
- broken or incomplete behavior
- regressions
- missing edge-case handling
- unsafe assumptions
- missing tests or inadequate test updates

If the iteration is acceptable, output exactly:
LGTM

Otherwise, output a numbered list of findings.

For each finding include:
- severity: critical | major | minor
- file: exact path(s) if applicable
- problem: what is wrong, risky, or incomplete
- evidence: what in the diff or code supports the finding
- recommended fix: the smallest reasonable corrective action

Only report findings that materially affect correctness, completeness, or reliability."""

_ORCHESTRATED_FIX_PROMPT = """\
The following issues were found in the latest code changes against {prd_file}:

{findings}

Fix these issues in the repository.

Requirements:
- resolve each finding concretely
- preserve correct existing changes
- avoid unrelated edits
- keep the patch as small as possible while fully fixing the problems
- update tests if needed
- do not claim success without making the changes

If some finding is invalid or already resolved, handle that
conservatively and focus on the remaining real issues."""


@dataclass
class ToolConfig:
    type: str = "cli"  # cli | shell
    command: str = ""
    args: list[str] = field(default_factory=lambda: list[str]())
    stdin: str | None = None  # template string sent via stdin
    env: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    interactive: bool = False  # if True, stdin/stdout pass through to terminal
    allowed_tools: list[str] = field(default_factory=lambda: list[str]())  # --allowedTools


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
    reviewer_prompt: str = _PRD_REVIEWER_PROMPT
    fixer_prompt: str = _PRD_FIXER_PROMPT


@dataclass
class PostReviewConfig(ReviewConfig):
    reviewer_prompt: str = _POST_REVIEWER_PROMPT
    fixer_prompt: str = _POST_FIXER_PROMPT


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
    review_prompt: str = _ORCHESTRATED_REVIEW_PROMPT
    fix_prompt: str = _ORCHESTRATED_FIX_PROMPT
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
    prd_tool: str = "claude-interactive"  # tool for PRD generation and conversion

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
            interactive=_parse_bool(cfg.get("interactive", False), False),
            allowed_tools=cfg.get("allowed_tools", []),
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


@dataclass
class ConfigProvenance:
    """Tracks which config layer set each key (dot-separated paths)."""

    sources: dict[str, str] = field(default_factory=lambda: dict[str, str]())

    def record_layer(self, data: dict[str, Any], label: str, prefix: str = "") -> None:
        """Record that *label* set every leaf key in *data*."""
        for key, value in data.items():
            full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
            if isinstance(value, dict):
                self.record_layer(cast(dict[str, Any], value), label, full_key)
            else:
                self.sources[full_key] = label

    def format(self, config: Config) -> str:
        """Format provenance as a human-readable table."""
        import dataclasses

        lines: list[str] = []

        def _walk(obj: Any, prefix: str = "") -> None:
            if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                for f in dataclasses.fields(obj):
                    key = f"{prefix}.{f.name}" if prefix else f.name
                    _walk(getattr(obj, f.name), key)
            elif isinstance(obj, dict):
                for k, v in cast(dict[str, Any], obj).items():
                    _walk(v, f"{prefix}.{k}" if prefix else str(k))
            else:
                source = self.sources.get(prefix, "default")
                lines.append(f"{prefix}: {obj!r}  ({source})")

        _walk(config)
        return "\n".join(lines)


def load_config_with_provenance(
    paths: list[Path] | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> tuple[Config, ConfigProvenance]:
    """Like load_config but also returns provenance info."""
    provenance = ConfigProvenance()

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
            provenance.record_layer(layer, p.name)
            data = _deep_merge(data, layer)

    if overrides:
        cleaned = {k: v for k, v in overrides.items() if v is not None}
        provenance.record_layer(cleaned, "cli")
        data = _deep_merge(data, cleaned)

    cfg = _build_config(data)
    return cfg, provenance


def _merge_layers(
    paths: list[Path] | Path | None,
    overrides: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[Path]]:
    """Merge YAML files and overrides into a single data dict."""
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

    return data, path_list


def _build_config(data: dict[str, Any]) -> Config:
    """Construct a Config from a merged data dict."""
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
            "codex": ToolConfig(
                type="cli",
                command="codex",
                args=["exec", "--full-auto", "{prompt}"],
            ),
            "claude": ToolConfig(
                type="cli",
                command="claude",
                args=["--dangerously-skip-permissions", "--print"],
                stdin="{prompt}",
            ),
            "claude-interactive": ToolConfig(
                type="cli",
                command="claude",
                args=["-p", "{prompt}"],
                interactive=True,
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash(git:*)"],
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

    # Auto-detect test commands when enabled but not configured
    if cfg.orchestrated.run_tests_between_steps and not cfg.orchestrated.test_commands:
        detected = detect_test_commands(cfg.repo_path)
        if detected:
            cfg.orchestrated.test_commands = detected
            logger.info("Auto-detected test commands: %s", detected)
        else:
            logger.warning(
                "run_tests_between_steps is enabled but no test commands "
                "configured or detected for %s",
                cfg.repo_path,
            )

    validate_config(cfg)
    return cfg


def load_config(
    paths: list[Path] | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
    """Load config from one or more YAML files with optional CLI overrides.

    Files are merged in order (later wins). *overrides* are applied last.
    """
    data, _ = _merge_layers(paths, overrides)
    return _build_config(data)


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
