"""Configuration loading and validation for ralph++."""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from .detection import detect_test_commands

logger = logging.getLogger(__name__)

# ── default prompt constants ────────────────────────────────────────

TEST_COMMANDS_GUIDANCE = """
IMPORTANT: This project uses specific CI commands for testing and linting.
Always use these commands instead of running tools directly:
{commands}
Do NOT run bare pytest, mypy, ruff, or other tools outside these commands —
the project may require specific virtual environments or configurations."""

_PRD_REVIEWER_PROMPT = """\
Read the PRD at {prd_file}.

Before evaluating the PRD as a document, perform a feasibility check
against the actual codebase at {repo_path}:

1. Identify every type, class, schema, or interface the PRD references
   (e.g., dataclasses, Protocol classes, SQLite DDL, database models).
2. Read the source files where those types are defined.
3. For each field in the PRD's canonical contracts, verify:
   - The corresponding field in the existing codebase type has a
     compatible type (e.g., nullable vs non-nullable)
   - Any schema constraints (NOT NULL, foreign keys, defaults) are
     compatible with the PRD's stated behavioral guarantees
   - The PRD's migration/compatibility claims (e.g., "no schema
     migration needed") are true given the actual schema
4. For each acceptance criterion that asserts exact round-trip behavior,
   verify the underlying storage layer can represent all values in the
   contract's type (e.g., can the column store NULL if the field is
   Optional?)

Flag any feasibility issue as severity: critical.

Then evaluate the PRD as a document:
- complete
- unambiguous
- implementable as a sequence of small independent user stories
- testable with concrete acceptance criteria
- explicit about constraints, edge cases, and non-goals where relevant
{previous_findings}
If the PRD is fully satisfactory, output exactly:
LGTM

If only minor issues remain (nits, style, non-blocking suggestions), output:
LGTM
Then list the minor observations below for informational purposes.

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

_PRD_JSON_REVIEW_PROMPT = """\
You are reviewing a generated prd.json against the original PRD and
the codebase to catch criteria that were sharpened, invented, or made
infeasible during the conversion from PRD markdown to structured JSON.

Inputs:
- Original PRD: {prd_file}
- Generated prd.json: {prd_json_file}
- Codebase root: {repo_path}

For each user story in prd.json:

1. **Traceability**: Verify every acceptance criterion traces back to
   a requirement in the original PRD. Flag any criterion that was
   invented during conversion and has no basis in the PRD.

2. **Faithfulness**: Verify the criterion accurately represents the
   PRD's intent. Flag any criterion that tightens a constraint beyond
   what the PRD specifies (e.g., "all fields match exactly" when the
   PRD allows semantic equivalence for certain fields).

3. **Feasibility**: For criteria that reference codebase types or
   schemas, verify the criterion is satisfiable given the actual types.
   Read the relevant source files if needed.

4. **Independence**: Verify the story can be implemented without
   depending on stories with higher priority numbers.

If prd.json is faithful, feasible, and traceable, output exactly:
LGTM

Otherwise, output a numbered list of issues.

For each issue include:
- severity: critical | major | minor
- story: the story ID (e.g., US-004)
- criterion: the specific acceptance criterion
- problem: what is wrong
- recommended fix: how to adjust the criterion in prd.json

Do not rewrite prd.json yourself. Only review it."""

_PRD_JSON_FIXER_PROMPT = """\
Fix the following issues in {prd_json_file} based on the original PRD
at {prd_file}.

{findings}

Update prd.json in place. Do not modify the PRD markdown.
Only adjust acceptance criteria to resolve the flagged issues.
Preserve story IDs, priorities, and overall structure."""

_POST_REVIEWER_PROMPT = """\
Review the implementation against ONLY the completed user stories listed below.
Do NOT evaluate against stories that are not listed here.

## Completed stories to review

{stories_under_review}
{incomplete_stories_note}
{diff}
{previous_findings}
Review whether the code fully satisfies every listed story and its acceptance criteria.

Check for:
- missing functionality
- incorrect behavior
- regressions
- edge-case handling
- obvious design or contract violations
- missing or inadequate tests
- mismatches between the implementation and the stories above

If everything looks correct, output exactly:
LGTM

If only minor issues remain (nits, style, non-blocking suggestions), output:
LGTM
Then list the minor observations below for informational purposes.

Otherwise, output a numbered list of findings.

For each finding include:
- severity: critical | major | minor
- file: exact path(s) if applicable
- problem: what is wrong or missing
- evidence: the concrete mismatch, bug risk, or regression
- recommended fix: the smallest reasonable corrective action

Be specific. Do not give vague style feedback unless it affects
correctness or maintainability materially.
{test_commands_guidance}
{test_results}"""

_POST_FIXER_PROMPT = """\
The following issues were found in the final implementation.

## Stories under review

{stories_under_review}

## Findings to fix

{findings}

Fix these issues in the codebase.

Requirements:
- address every finding unless two findings are duplicates
- only fix issues related to the stories listed above
- preserve already-correct behavior
- avoid unrelated refactors
- add or update tests where needed
- keep changes minimal but sufficient

Do not just describe the fixes. Make the code changes."""

_ORCHESTRATED_REVIEW_PROMPT = """\
Review the latest iteration against ONLY the user stories listed below.
Do NOT evaluate against stories that are not listed here.

## Stories under review

{stories_under_review}

## Git diff

{diff}
{previous_findings}
You may inspect the changed files and nearby code as needed.

Check for:
- requirement mismatches against the acceptance criteria above
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

Only report findings that materially affect correctness, completeness, or reliability.
{test_commands_guidance}
{test_results}"""

_ORCHESTRATED_FIX_PROMPT = """\
The following issues were found in the latest code changes.

## Stories under review

{stories_under_review}

## Findings to fix

{findings}

Fix these issues in the repository.

Requirements:
- resolve each finding concretely
- only fix issues related to the stories listed above
- preserve correct existing changes
- avoid unrelated edits
- keep the patch as small as possible while fully fixing the problems
- update tests if needed
- do not claim success without making the changes

If some finding is invalid or already resolved, handle that
conservatively and focus on the remaining real issues."""

_ORCHESTRATED_REVIEW_FIRST_PROMPT = """\
Review the latest iteration against ONLY the user stories listed below.
Do NOT evaluate against stories that are not listed here.

## Stories under review

{stories_under_review}

## Feasibility pre-check

Before reviewing the diff, check whether each acceptance criterion above
is structurally satisfiable:
1. Read the original types/schemas of the files touched by the diff
2. If any criterion requires exact round-trip of a value that the
   underlying storage cannot represent (e.g., None in a NOT NULL column),
   flag it as: severity: critical, problem: "criterion unsatisfiable"

## Git diff

{diff}
{previous_findings}
You may inspect the changed files and nearby code as needed.

Check for:
- requirement mismatches against the acceptance criteria above
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

Only report findings that materially affect correctness, completeness, or reliability.
{test_commands_guidance}
{test_results}"""


@dataclass
class ToolConfig:
    type: str = "cli"  # cli | shell
    command: str = ""
    args: list[str] = field(default_factory=lambda: list[str]())
    stdin: str | None = None  # template string sent via stdin
    env: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    interactive: bool = False  # if True, stdin/stdout pass through to terminal
    allowed_tools: list[str] = field(default_factory=lambda: list[str]())  # --allowedTools
    timeout: int = 0  # seconds; 0 means no timeout


@dataclass
class PrdReviewConfig:
    """Review config for PRD review loop."""

    reviewer: str = "codex"
    reviewer_prompt: str = _PRD_REVIEWER_PROMPT
    fixer: str = "claude"
    fixer_prompt: str = _PRD_FIXER_PROMPT
    max_cycles: int = 3
    enabled: bool = True


@dataclass
class PrdJsonReviewConfig:
    """Review config for prd.json validation after conversion."""

    reviewer: str = "codex"
    reviewer_prompt: str = _PRD_JSON_REVIEW_PROMPT
    fixer: str = "claude"
    fixer_prompt: str = _PRD_JSON_FIXER_PROMPT
    max_cycles: int = 2
    enabled: bool = True


@dataclass
class PostReviewConfig:
    """Review config for post-run review loop."""

    reviewer: str = "codex"
    reviewer_prompt: str = _POST_REVIEWER_PROMPT
    fixer: str = "claude"
    fixer_prompt: str = _POST_FIXER_PROMPT
    max_cycles: int = 3
    enabled: bool = True


Mode = Literal["delegated", "orchestrated"]
Severity = Literal["minor", "major", "critical"]

_VALID_MODES: set[str] = {"delegated", "orchestrated"}
_VALID_SEVERITIES: set[str] = {"minor", "major", "critical"}


def parse_mode(value: str) -> Mode:
    """Validate and narrow a string to a ``Mode`` literal."""
    if value not in _VALID_MODES:
        raise ValueError(f"Invalid mode: {value!r} (expected one of {_VALID_MODES})")
    return cast(Mode, value)


def parse_severity(value: str) -> Severity:
    """Validate and narrow a string to a ``Severity`` literal."""
    if value not in _VALID_SEVERITIES:
        raise ValueError(f"Invalid severity: {value!r} (expected one of {_VALID_SEVERITIES})")
    return cast(Severity, value)


@dataclass
class RalphConfig:
    max_iterations: int = 20
    mode: Mode = "delegated"
    sandbox_dir: str = ""  # path to ralph-sandbox checkout
    sandbox_tool: str = "claude"  # tool for sandbox (delegated mode): claude | codex
    session_runner: str = "scripts/ralph-single-step.sh"  # session runner for orchestrated mode


OnMaxCycles = Literal["continue", "abort", "retry-once"]
_VALID_ON_MAX_CYCLES: set[str] = {"continue", "abort", "retry-once"}


def parse_on_max_cycles(value: str) -> OnMaxCycles:
    """Validate and narrow a string to an ``OnMaxCycles`` literal."""
    if value not in _VALID_ON_MAX_CYCLES:
        raise ValueError(
            f"Invalid on_max_cycles: {value!r} (expected one of {_VALID_ON_MAX_CYCLES})"
        )
    return cast(OnMaxCycles, value)


@dataclass
class NonInteractiveConfig:
    """Defaults applied when running unattended (no TTY or --non-interactive).

    Each field selects the automatic action taken when a review gate reaches
    its max cycle count without an LGTM. Values:

    - ``continue``: log a warning and proceed without reviewer approval
    - ``abort``:    raise ``MaxCyclesAbort`` to stop the workflow
    - ``retry-once``: run one more batch of cycles, then ``continue``
    """

    enabled: bool = False  # True => force non-interactive even in a TTY
    on_max_cycles_prd: OnMaxCycles = "continue"
    on_max_cycles_prd_json: OnMaxCycles = "continue"
    on_max_cycles_post: OnMaxCycles = "continue"


OnRetryExhaustion = Literal["abort", "skip-story"]
_VALID_ON_RETRY_EXHAUSTION: set[str] = {"abort", "skip-story"}


def parse_on_retry_exhaustion(value: str) -> OnRetryExhaustion:
    """Validate and narrow a string to an ``OnRetryExhaustion`` literal."""
    if value not in _VALID_ON_RETRY_EXHAUSTION:
        raise ValueError(
            f"Invalid on_retry_exhaustion: {value!r} (expected one of {_VALID_ON_RETRY_EXHAUSTION})"
        )
    return cast(OnRetryExhaustion, value)


@dataclass
class OrchestratedConfig:
    coder: str = "claude"
    reviewer: str = "codex"
    fixer: str = "claude"
    max_iteration_retries: int = 2
    run_tests_between_steps: bool = False
    test_commands: list[str] = field(default_factory=lambda: list[str]())
    backout_on_failure: bool = True
    backout_severity_threshold: Severity = "major"
    auto_allow_test_commands: bool = True
    max_idle_iterations: int = 2
    # Abort the run after this many consecutive coder iterations that fail
    # with an infra/process error (OAuth expiry, network, timeout). Counter
    # resets on any successful coder run. 0 disables the circuit-breaker.
    max_consecutive_infra_failures: int = 3
    # Behavior when an iteration exhausts all retries / fix cycles for a
    # single story (#127):
    #   "abort"      — stop the backlog and advance to post-review (legacy)
    #   "skip-story" — mark the failing story as skipped, continue the loop
    #                  so independent downstream stories still get a chance
    on_retry_exhaustion: OnRetryExhaustion = "skip-story"
    # #126: when the reviewer rejects retry N+1 with findings that are
    # essentially the same as retry N, the coder has converged on a wrong
    # interpretation. Stop wasting cycles after this many consecutive
    # same-finding rejections. 0 disables convergence detection.
    max_same_finding_retries: int = 2
    # #126: Jaccard similarity threshold (0.0–1.0) for "same finding"
    # detection. Two reviewer outputs are considered the same finding when
    # their normalized token sets overlap by at least this much.
    same_finding_similarity_threshold: float = 0.75
    coder_timeout: int = 1800  # seconds (30 min default)
    reviewer_timeout: int = 300  # seconds (5 min default)
    fixer_timeout: int = 600  # seconds (10 min default)
    review_prompt: str = _ORCHESTRATED_REVIEW_PROMPT
    first_review_prompt: str = _ORCHESTRATED_REVIEW_FIRST_PROMPT
    fix_prompt: str = _ORCHESTRATED_FIX_PROMPT
    prompt_template: str | None = None
    story_filter: list[str] = field(default_factory=lambda: list[str]())
    max_diff_chars: int = 50_000  # truncate diffs exceeding this size


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
    prd_json_tool: str = "claude"  # tool for PRD-to-JSON conversion (non-interactive)

    # Review stages
    prd_review: PrdReviewConfig = field(default_factory=PrdReviewConfig)
    prd_json_review: PrdJsonReviewConfig = field(default_factory=PrdJsonReviewConfig)
    post_review: PostReviewConfig = field(default_factory=PostReviewConfig)

    # Ralph
    ralph: RalphConfig = field(default_factory=RalphConfig)

    # Orchestrated mode
    orchestrated: OrchestratedConfig = field(default_factory=OrchestratedConfig)

    # Non-interactive defaults (applied when stdin is not a TTY or when
    # non_interactive.enabled is True / RALPH_NON_INTERACTIVE is set)
    non_interactive: NonInteractiveConfig = field(default_factory=NonInteractiveConfig)

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
    return default


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
            timeout=int(cfg.get("timeout", 0)),
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
    cfg.prd_json_tool = data.get("prd_json_tool", cfg.prd_json_tool)

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
                args=["--print"],
                stdin="{prompt}",
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash(git:*)"],
            ),
            "claude-interactive": ToolConfig(
                type="cli",
                command="claude",
                args=["{prompt}"],
                interactive=True,
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash(git:*)"],
            ),
        }

    if "prd_review" in data:
        cfg.prd_review = _parse_review(data["prd_review"], cfg.prd_review)
    if "prd_json_review" in data:
        cfg.prd_json_review = _parse_review(data["prd_json_review"], cfg.prd_json_review)
    if "post_review" in data:
        cfg.post_review = _parse_review(data["post_review"], cfg.post_review)

    if "ralph" in data:
        r = data["ralph"]
        cfg.ralph = RalphConfig(
            max_iterations=int(r.get("max_iterations", 20)),
            mode=parse_mode(r.get("mode", "delegated")),
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
            backout_severity_threshold=parse_severity(
                o.get("backout_severity_threshold", defaults.backout_severity_threshold)
            ),
            auto_allow_test_commands=_parse_bool(
                o.get("auto_allow_test_commands", defaults.auto_allow_test_commands),
                defaults.auto_allow_test_commands,
            ),
            max_idle_iterations=int(o.get("max_idle_iterations", defaults.max_idle_iterations)),
            max_consecutive_infra_failures=int(
                o.get(
                    "max_consecutive_infra_failures",
                    defaults.max_consecutive_infra_failures,
                )
            ),
            on_retry_exhaustion=parse_on_retry_exhaustion(
                o.get("on_retry_exhaustion", defaults.on_retry_exhaustion)
            ),
            max_same_finding_retries=int(
                o.get("max_same_finding_retries", defaults.max_same_finding_retries)
            ),
            same_finding_similarity_threshold=float(
                o.get(
                    "same_finding_similarity_threshold",
                    defaults.same_finding_similarity_threshold,
                )
            ),
            coder_timeout=int(o.get("coder_timeout", defaults.coder_timeout)),
            reviewer_timeout=int(o.get("reviewer_timeout", defaults.reviewer_timeout)),
            fixer_timeout=int(o.get("fixer_timeout", defaults.fixer_timeout)),
            review_prompt=o.get("review_prompt", defaults.review_prompt),
            first_review_prompt=o.get("first_review_prompt", defaults.first_review_prompt),
            fix_prompt=o.get("fix_prompt", defaults.fix_prompt),
            prompt_template=o.get("prompt_template", defaults.prompt_template),
            story_filter=o.get("story_filter", defaults.story_filter),
            max_diff_chars=int(o.get("max_diff_chars", defaults.max_diff_chars)),
        )

    if "non_interactive" in data:
        ni = data["non_interactive"]
        ni_defaults = NonInteractiveConfig()
        cfg.non_interactive = NonInteractiveConfig(
            enabled=_parse_bool(ni.get("enabled", ni_defaults.enabled), ni_defaults.enabled),
            on_max_cycles_prd=parse_on_max_cycles(
                ni.get("on_max_cycles_prd", ni_defaults.on_max_cycles_prd)
            ),
            on_max_cycles_prd_json=parse_on_max_cycles(
                ni.get("on_max_cycles_prd_json", ni_defaults.on_max_cycles_prd_json)
            ),
            on_max_cycles_post=parse_on_max_cycles(
                ni.get("on_max_cycles_post", ni_defaults.on_max_cycles_post)
            ),
        )

    cfg.hooks = data.get("hooks", {})

    # Auto-detect test commands when needed but not explicitly configured
    needs_detection = not cfg.orchestrated.test_commands and (
        cfg.orchestrated.run_tests_between_steps or cfg.orchestrated.auto_allow_test_commands
    )
    if needs_detection:
        detected = detect_test_commands(cfg.repo_path)
        if detected:
            cfg.orchestrated.test_commands = detected
            logger.info("Auto-detected test commands: %s", detected)
        elif cfg.orchestrated.run_tests_between_steps:
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

    if cfg.ralph.mode not in _VALID_MODES:
        errors.append(f"ralph.mode={cfg.ralph.mode!r} not in {_VALID_MODES}")

    if cfg.prd_tool not in cfg.tools:
        errors.append(f"prd_tool={cfg.prd_tool!r} not in tools {list(cfg.tools)}")

    if cfg.prd_json_tool not in cfg.tools:
        errors.append(f"prd_json_tool={cfg.prd_json_tool!r} not in tools {list(cfg.tools)}")

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

    if cfg.orchestrated.backout_severity_threshold not in _VALID_SEVERITIES:
        errors.append(
            f"orchestrated.backout_severity_threshold="
            f"{cfg.orchestrated.backout_severity_threshold!r} not in {_VALID_SEVERITIES}"
        )

    if cfg.orchestrated.max_consecutive_infra_failures < 0:
        errors.append(
            "orchestrated.max_consecutive_infra_failures must be >= 0, "
            f"got {cfg.orchestrated.max_consecutive_infra_failures}"
        )

    if cfg.orchestrated.on_retry_exhaustion not in _VALID_ON_RETRY_EXHAUSTION:
        errors.append(
            f"orchestrated.on_retry_exhaustion="
            f"{cfg.orchestrated.on_retry_exhaustion!r} not in {_VALID_ON_RETRY_EXHAUSTION}"
        )

    for attr in (
        "on_max_cycles_prd",
        "on_max_cycles_prd_json",
        "on_max_cycles_post",
    ):
        val = getattr(cfg.non_interactive, attr)
        if val not in _VALID_ON_MAX_CYCLES:
            errors.append(f"non_interactive.{attr}={val!r} not in {_VALID_ON_MAX_CYCLES}")

    for attr in ("coder_timeout", "reviewer_timeout", "fixer_timeout"):
        val = getattr(cfg.orchestrated, attr)
        if val < 0:
            errors.append(f"orchestrated.{attr} must be >= 0, got {val}")

    if cfg.orchestrated.max_same_finding_retries < 0:
        errors.append(
            "orchestrated.max_same_finding_retries must be >= 0, "
            f"got {cfg.orchestrated.max_same_finding_retries}"
        )

    sim = cfg.orchestrated.same_finding_similarity_threshold
    if not 0.0 <= sim <= 1.0:
        errors.append(
            f"orchestrated.same_finding_similarity_threshold must be in [0.0, 1.0], got {sim}"
        )

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
