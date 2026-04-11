"""Tests for config loading."""

import tempfile
from pathlib import Path

import pytest
import yaml

from ralph_pp.config import (
    Config,
    ConfigProvenance,
    OrchestratedConfig,
    PostReviewConfig,
    PrdReviewConfig,
    RalphConfig,
    ToolConfig,
    _deep_merge,
    _parse_bool,
    discover_config_files,
    format_effective_config,
    load_config,
    load_config_with_provenance,
    validate_config,
)


def test_load_empty_config():
    """Loading with no file returns sensible defaults."""
    cfg = load_config(None)
    assert isinstance(cfg, Config)
    assert cfg.branch_prefix == "ralph/"
    assert cfg.branch_suffix_length == 4
    assert cfg.ralph.max_iterations == 20
    assert cfg.ralph.mode == "delegated"
    assert cfg.ralph.sandbox_tool == "claude"
    assert "codex" in cfg.tools
    assert "claude" in cfg.tools
    assert "claude-interactive" in cfg.tools
    assert cfg.tools["claude-interactive"].interactive is True
    assert cfg.tools["claude-interactive"].allowed_tools == [
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "Bash(git:*)",
    ]
    assert "-p" not in cfg.tools["claude-interactive"].args
    assert cfg.tools["claude"].interactive is False
    assert "--dangerously-skip-permissions" not in cfg.tools["claude"].args
    assert cfg.tools["claude"].allowed_tools == [
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "Bash(git:*)",
    ]


def test_load_config_from_file():
    """Values from YAML file are loaded correctly."""
    data = {
        "branch_prefix": "feature/",
        "branch_suffix_length": 6,
        "ralph": {
            "max_iterations": 30,
            "mode": "orchestrated",
            "sandbox_dir": "/path/to/sandbox",
            "sandbox_tool": "codex",
        },
        "hooks": {"post_worktree_create": ["echo hello"]},
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        tmp_path = Path(f.name)

    cfg = load_config(tmp_path)
    assert cfg.branch_prefix == "feature/"
    assert cfg.branch_suffix_length == 6
    assert cfg.ralph.max_iterations == 30
    assert cfg.ralph.mode == "orchestrated"
    assert cfg.ralph.sandbox_dir == "/path/to/sandbox"
    assert cfg.ralph.sandbox_tool == "codex"
    assert cfg.hooks["post_worktree_create"] == ["echo hello"]
    tmp_path.unlink()


def test_cli_overrides_take_precedence():
    """CLI overrides win over file values."""
    data = {"branch_prefix": "file/"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        tmp_path = Path(f.name)

    cfg = load_config(tmp_path, overrides={"branch_prefix": "cli/"})
    assert cfg.branch_prefix == "cli/"
    tmp_path.unlink()


def test_get_tool_raises_on_unknown():
    """get_tool raises ValueError for unknown tool names."""
    cfg = load_config(None)
    try:
        cfg.get_tool("nonexistent")
        assert False, "Should have raised"
    except ValueError:
        pass


def test_default_mode_is_delegated():
    """Default workflow mode is delegated."""
    cfg = load_config(None)
    assert cfg.ralph.mode == "delegated"


def test_default_orchestrated_config():
    """OrchestratedConfig has sensible defaults."""
    cfg = load_config(None)
    orch = cfg.orchestrated
    assert isinstance(orch, OrchestratedConfig)
    assert orch.coder == "claude"
    assert orch.reviewer == "codex"
    assert orch.fixer == "claude"
    assert orch.max_iteration_retries == 2
    assert orch.run_tests_between_steps is False
    # test_commands may be auto-detected from the repo Makefile
    assert isinstance(orch.test_commands, list)
    assert orch.backout_on_failure is True
    assert orch.auto_allow_test_commands is True
    assert "{diff}" in orch.review_prompt
    assert "{findings}" in orch.fix_prompt
    assert orch.prompt_template is None


def test_load_orchestrated_config():
    """Orchestrated config section is parsed from YAML."""
    data = {
        "orchestrated": {
            "coder": "codex",
            "reviewer": "claude",
            "fixer": "codex",
            "max_iteration_retries": 5,
            "run_tests_between_steps": True,
            "test_commands": ["pytest", "mypy ."],
            "backout_on_failure": False,
            "review_prompt": "custom review: {diff}",
            "fix_prompt": "custom fix: {findings}",
            "prompt_template": "iteration {iteration}",
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        tmp_path = Path(f.name)

    cfg = load_config(tmp_path)
    orch = cfg.orchestrated
    assert orch.coder == "codex"
    assert orch.reviewer == "claude"
    assert orch.fixer == "codex"
    assert orch.max_iteration_retries == 5
    assert orch.run_tests_between_steps is True
    assert orch.test_commands == ["pytest", "mypy ."]
    assert orch.backout_on_failure is False
    assert orch.review_prompt == "custom review: {diff}"
    assert orch.fix_prompt == "custom fix: {findings}"
    assert orch.prompt_template == "iteration {iteration}"
    tmp_path.unlink()


# ── _parse_bool tests ──────────────────────────────────────────────────


def test_parse_bool_native_true():
    assert _parse_bool(True, False) is True


def test_parse_bool_native_false():
    assert _parse_bool(False, True) is False


def test_parse_bool_string_false():
    """Quoted YAML 'false' must not become True."""
    assert _parse_bool("false", True) is False


def test_parse_bool_string_true():
    assert _parse_bool("true", False) is True


def test_parse_bool_string_yes():
    assert _parse_bool("yes", False) is True


def test_parse_bool_string_no():
    assert _parse_bool("no", True) is False


def test_parse_bool_invalid_string():
    with pytest.raises(ValueError, match="Invalid boolean"):
        _parse_bool("maybe", False)


# ── validate_config tests ─────────────────────────────────────────────


def test_validate_config_bad_mode():
    import pytest

    cfg = Config(
        tools={"claude": ToolConfig(), "codex": ToolConfig(), "claude-interactive": ToolConfig()},
        ralph=RalphConfig(mode="invalid"),
    )
    with pytest.raises(ValueError, match="ralph.mode"):
        validate_config(cfg)


def test_validate_config_bad_sandbox_tool():
    import pytest

    cfg = Config(
        tools={"claude": ToolConfig(), "claude-interactive": ToolConfig()},
        ralph=RalphConfig(sandbox_tool="nonexistent"),
    )
    with pytest.raises(ValueError, match="ralph.sandbox_tool"):
        validate_config(cfg)


def test_validate_config_bad_orchestrated_coder():
    import pytest

    cfg = Config(
        tools={"claude": ToolConfig(), "codex": ToolConfig(), "claude-interactive": ToolConfig()},
        orchestrated=OrchestratedConfig(coder="nonexistent"),
    )
    with pytest.raises(ValueError, match="orchestrated.coder"):
        validate_config(cfg)


def test_validate_config_valid():
    """A valid config should not raise."""
    cfg = load_config(None)
    validate_config(cfg)  # should not raise


def test_load_config_string_false_boolean():
    """Quoted 'false' in YAML should parse as False, not True."""
    data = {
        "orchestrated": {
            "backout_on_failure": "false",
            "run_tests_between_steps": "false",
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        tmp_path = Path(f.name)

    cfg = load_config(tmp_path)
    assert cfg.orchestrated.backout_on_failure is False
    assert cfg.orchestrated.run_tests_between_steps is False
    tmp_path.unlink()


def test_auto_allow_test_commands_from_file():
    """auto_allow_test_commands should be parseable from YAML."""
    data = {
        "orchestrated": {
            "auto_allow_test_commands": False,
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        tmp_path = Path(f.name)

    cfg = load_config(tmp_path)
    assert cfg.orchestrated.auto_allow_test_commands is False
    tmp_path.unlink()


def test_validate_config_empty_test_command():
    import pytest

    cfg = Config(
        tools={"claude": ToolConfig(), "codex": ToolConfig(), "claude-interactive": ToolConfig()},
        orchestrated=OrchestratedConfig(
            run_tests_between_steps=True,
            test_commands=["pytest", ""],
        ),
    )
    with pytest.raises(ValueError, match="test_commands"):
        validate_config(cfg)


def test_validate_config_test_commands_not_checked_when_disabled():
    """Empty test_commands entries are fine when run_tests_between_steps is False."""
    cfg = Config(
        tools={"claude": ToolConfig(), "codex": ToolConfig(), "claude-interactive": ToolConfig()},
        orchestrated=OrchestratedConfig(
            run_tests_between_steps=False,
            test_commands=[""],
        ),
    )
    validate_config(cfg)  # should not raise


# ── review config split tests ────────────────────────────────────────


def test_default_review_prompts_are_nonempty():
    """Both prd_review and post_review should have non-empty default prompts."""
    cfg = load_config(None)
    assert cfg.prd_review.reviewer_prompt, "prd_review.reviewer_prompt should not be empty"
    assert cfg.prd_review.fixer_prompt, "prd_review.fixer_prompt should not be empty"
    assert cfg.post_review.reviewer_prompt, "post_review.reviewer_prompt should not be empty"
    assert cfg.post_review.fixer_prompt, "post_review.fixer_prompt should not be empty"


def test_prd_and_post_review_have_different_prompts():
    """PRD and post-review stages should have distinct default prompts."""
    cfg = load_config(None)
    assert cfg.prd_review.reviewer_prompt != cfg.post_review.reviewer_prompt
    assert cfg.prd_review.fixer_prompt != cfg.post_review.fixer_prompt


def test_review_config_types():
    """prd_review and post_review should be the correct subclass types."""
    cfg = load_config(None)
    assert isinstance(cfg.prd_review, PrdReviewConfig)
    assert isinstance(cfg.post_review, PostReviewConfig)


def test_load_prd_review_from_file():
    """prd_review parsed from YAML should be a PrdReviewConfig."""
    data = {
        "prd_review": {
            "reviewer_prompt": "custom prd prompt {prd_file}",
            "max_cycles": 5,
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        tmp_path = Path(f.name)

    cfg = load_config(tmp_path)
    assert isinstance(cfg.prd_review, PrdReviewConfig)
    assert cfg.prd_review.reviewer_prompt == "custom prd prompt {prd_file}"
    assert cfg.prd_review.max_cycles == 5
    tmp_path.unlink()


def test_load_post_review_from_file():
    """post_review parsed from YAML should be a PostReviewConfig."""
    data = {
        "post_review": {
            "reviewer_prompt": "custom post prompt {prd_file}",
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        tmp_path = Path(f.name)

    cfg = load_config(tmp_path)
    assert isinstance(cfg.post_review, PostReviewConfig)
    assert cfg.post_review.reviewer_prompt == "custom post prompt {prd_file}"
    tmp_path.unlink()


# ── prd_tool tests ───────────────────────────────────────────────────


def test_prd_tool_default():
    """Default prd_tool should be 'claude-interactive'."""
    cfg = load_config(None)
    assert cfg.prd_tool == "claude-interactive"


def test_prd_tool_from_file():
    """prd_tool should be loadable from YAML."""
    data = {"prd_tool": "codex"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        tmp_path = Path(f.name)

    cfg = load_config(tmp_path)
    assert cfg.prd_tool == "codex"
    tmp_path.unlink()


def test_validate_config_bad_prd_tool():
    import pytest

    cfg = Config(
        tools={"claude": ToolConfig(), "codex": ToolConfig(), "claude-interactive": ToolConfig()},
        prd_tool="nonexistent",
    )
    with pytest.raises(ValueError, match="prd_tool"):
        validate_config(cfg)


# ── prd_json_tool tests ──────────────────────────────────────────────


def test_prd_json_tool_default():
    """Default prd_json_tool should be 'claude'."""
    cfg = load_config(None)
    assert cfg.prd_json_tool == "claude"


def test_prd_json_tool_from_file():
    """prd_json_tool should be loadable from YAML."""
    data = {"prd_json_tool": "codex"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        tmp_path = Path(f.name)

    cfg = load_config(tmp_path)
    assert cfg.prd_json_tool == "codex"
    tmp_path.unlink()


def test_validate_config_bad_prd_json_tool():
    import pytest

    cfg = Config(
        tools={"claude": ToolConfig(), "codex": ToolConfig(), "claude-interactive": ToolConfig()},
        prd_json_tool="nonexistent",
    )
    with pytest.raises(ValueError, match="prd_json_tool"):
        validate_config(cfg)


def test_interactive_field_from_yaml():
    """interactive and allowed_tools fields in tools YAML should be parsed correctly."""
    data = {
        "tools": {
            "claude": {"command": "claude", "args": ["--print"]},
            "claude-interactive": {
                "command": "claude",
                "args": ["{prompt}"],
                "interactive": True,
                "allowed_tools": ["Read", "Write", "Edit"],
            },
            "codex": {"command": "codex", "args": ["{prompt}"]},
            "batch-tool": {
                "command": "batch-cli",
                "args": ["{prompt}"],
            },
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        tmp_path = Path(f.name)

    cfg = load_config(tmp_path)
    assert cfg.tools["claude-interactive"].interactive is True
    assert cfg.tools["claude-interactive"].allowed_tools == ["Read", "Write", "Edit"]
    assert cfg.tools["batch-tool"].interactive is False
    assert cfg.tools["batch-tool"].allowed_tools == []
    assert cfg.tools["claude"].interactive is False
    tmp_path.unlink()


# ── deep merge tests ─────────────────────────────────────────────────


def test_deep_merge_nested():
    """Nested dict keys should merge, not replace the whole dict."""
    base = {"ralph": {"mode": "delegated", "max_iterations": 20}}
    override = {"ralph": {"mode": "orchestrated"}}
    result = _deep_merge(base, override)
    assert result == {"ralph": {"mode": "orchestrated", "max_iterations": 20}}


def test_deep_merge_list_replaces():
    """Lists should be replaced entirely, not appended."""
    base = {"test_commands": ["make test", "ruff check ."]}
    override = {"test_commands": ["pytest"]}
    result = _deep_merge(base, override)
    assert result == {"test_commands": ["pytest"]}


def test_deep_merge_new_keys():
    """Keys in override not in base should be added."""
    base = {"a": 1}
    override = {"b": 2}
    result = _deep_merge(base, override)
    assert result == {"a": 1, "b": 2}


def test_deep_merge_scalar_replaces():
    """Scalar values in override should replace base."""
    base = {"branch_prefix": "ralph/"}
    override = {"branch_prefix": "feature/"}
    result = _deep_merge(base, override)
    assert result == {"branch_prefix": "feature/"}


# ── multi-path config loading tests ──────────────────────────────────


def test_load_config_multiple_paths():
    """Later files should override earlier ones, both contribute non-overlapping keys."""
    user_data = {
        "branch_prefix": "user/",
        "ralph": {"sandbox_dir": "/usr/local/sandbox", "mode": "delegated"},
    }
    project_data = {
        "ralph": {"mode": "orchestrated"},
        "orchestrated": {"coder": "codex"},
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f1:
        yaml.dump(user_data, f1)
        user_path = Path(f1.name)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f2:
        yaml.dump(project_data, f2)
        project_path = Path(f2.name)

    cfg = load_config([user_path, project_path])

    # project overrides mode
    assert cfg.ralph.mode == "orchestrated"
    # user sandbox_dir preserved (not in project)
    assert cfg.ralph.sandbox_dir == "/usr/local/sandbox"
    # user branch_prefix preserved
    assert cfg.branch_prefix == "user/"
    # project orchestrated.coder applied
    assert cfg.orchestrated.coder == "codex"

    user_path.unlink()
    project_path.unlink()


def test_load_config_single_path_backward_compat():
    """Passing a single Path (not list) should still work."""
    data = {"branch_prefix": "compat/"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        tmp_path = Path(f.name)

    cfg = load_config(tmp_path)
    assert cfg.branch_prefix == "compat/"
    tmp_path.unlink()


def test_load_config_none_returns_defaults():
    """Passing None should return defaults (backward compat)."""
    cfg = load_config(None)
    assert cfg.branch_prefix == "ralph/"


# ── discover_config_files tests ──────────────────────────────────────


def test_discover_config_files_user_xdg(tmp_path, monkeypatch):
    """XDG-style user config should be discovered."""
    xdg_config = tmp_path / ".config" / "ralph-plus-plus" / "config.yaml"
    xdg_config.parent.mkdir(parents=True)
    xdg_config.write_text("branch_prefix: xdg/\n")
    monkeypatch.setattr(Path, "expanduser", lambda self: tmp_path / str(self).lstrip("~/"))

    paths = discover_config_files(repo_path=tmp_path)
    assert any("config.yaml" in str(p) for p in paths)


def test_discover_config_files_legacy_user(tmp_path, monkeypatch):
    """Legacy ~/.ralph++.yaml should be discovered when XDG doesn't exist."""
    legacy = tmp_path / ".ralph++.yaml"
    legacy.write_text("branch_prefix: legacy/\n")
    monkeypatch.setattr(Path, "expanduser", lambda self: tmp_path / str(self).lstrip("~/"))

    paths = discover_config_files(repo_path=tmp_path)
    assert any(".ralph++.yaml" in str(p) for p in paths)


def test_discover_config_files_project_relative(tmp_path):
    """Project config should be found relative to repo_path, not CWD."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    project_config = repo / "ralph++.yaml"
    project_config.write_text("branch_prefix: project/\n")

    paths = discover_config_files(repo_path=repo)
    assert project_config.resolve() in [p.resolve() for p in paths]


def test_discover_config_files_project_dot_ralph(tmp_path):
    """Project config in .ralph/ subdir should be discovered."""
    repo = tmp_path / "myrepo"
    (repo / ".ralph").mkdir(parents=True)
    project_config = repo / ".ralph" / "ralph++.yaml"
    project_config.write_text("branch_prefix: dotralph/\n")

    paths = discover_config_files(repo_path=repo)
    assert project_config.resolve() in [p.resolve() for p in paths]


def test_discover_config_files_no_config(tmp_path):
    """No config files should return empty list."""
    paths = discover_config_files(repo_path=tmp_path)
    # May include user config if it exists on the machine, but no project config
    for p in paths:
        assert tmp_path not in p.parents


def test_discover_config_files_order(tmp_path, monkeypatch):
    """User config should come before project config."""
    # Set up user config
    user_config = tmp_path / ".ralph++.yaml"
    user_config.write_text("branch_prefix: user/\n")
    monkeypatch.setattr(Path, "expanduser", lambda self: tmp_path / str(self).lstrip("~/"))

    # Set up project config
    repo = tmp_path / "myrepo"
    repo.mkdir()
    project_config = repo / "ralph++.yaml"
    project_config.write_text("branch_prefix: project/\n")

    paths = discover_config_files(repo_path=repo)
    assert len(paths) == 2
    assert ".ralph++.yaml" in str(paths[0])
    assert "myrepo" in str(paths[1])


# ── format_effective_config tests ────────────────────────────────────


def test_format_effective_config():
    """format_effective_config should produce valid YAML."""
    cfg = load_config(None)
    output = format_effective_config(cfg)
    assert isinstance(output, str)
    parsed = yaml.safe_load(output)
    assert parsed["branch_prefix"] == "ralph/"
    assert parsed["prd_tool"] == "claude-interactive"
    assert parsed["prd_json_tool"] == "claude"
    assert "claude" in parsed["tools"]


# ── provenance tests ────────────────────────────────────────────────


def test_provenance_tracks_file_layers(tmp_path):
    """Provenance should record which file set each key."""
    user_data = {"branch_prefix": "user/", "ralph": {"mode": "delegated"}}
    project_data = {"ralph": {"mode": "orchestrated"}}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, dir=tmp_path) as f1:
        yaml.dump(user_data, f1)
        user_path = Path(f1.name)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, dir=tmp_path) as f2:
        yaml.dump(project_data, f2)
        project_path = Path(f2.name)

    cfg, prov = load_config_with_provenance([user_path, project_path])
    assert prov.sources["branch_prefix"] == user_path.name
    assert prov.sources["ralph.mode"] == project_path.name
    assert cfg.ralph.mode == "orchestrated"
    assert cfg.branch_prefix == "user/"
    user_path.unlink()
    project_path.unlink()


def test_provenance_cli_overrides(tmp_path):
    """CLI overrides should show 'cli' as the source."""
    data = {"branch_prefix": "file/"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        tmp = Path(f.name)

    cfg, prov = load_config_with_provenance(tmp, overrides={"branch_prefix": "cli/"})
    assert prov.sources["branch_prefix"] == "cli"
    assert cfg.branch_prefix == "cli/"
    tmp.unlink()


def test_setup_cmd_prepends_to_post_worktree_create():
    """--setup-cmd values are prepended to post_worktree_create hooks."""
    cfg = load_config(None)
    cfg.hooks["post_worktree_create"] = ["existing-hook"]

    setup_cmd = ("uv sync", "make install")
    existing = cfg.hooks.get("post_worktree_create", [])
    cfg.hooks["post_worktree_create"] = list(setup_cmd) + existing

    assert cfg.hooks["post_worktree_create"] == [
        "uv sync",
        "make install",
        "existing-hook",
    ]


def test_setup_cmd_empty_leaves_hooks_unchanged():
    """When no --setup-cmd is given, hooks are unchanged."""
    cfg = load_config(None)
    cfg.hooks["post_worktree_create"] = ["existing-hook"]

    setup_cmd: tuple[str, ...] = ()
    if setup_cmd:
        existing = cfg.hooks.get("post_worktree_create", [])
        cfg.hooks["post_worktree_create"] = list(setup_cmd) + existing

    assert cfg.hooks["post_worktree_create"] == ["existing-hook"]


def test_setup_cmd_creates_hook_when_none_configured():
    """--setup-cmd works even when no post_worktree_create hooks exist."""
    cfg = load_config(None)
    assert "post_worktree_create" not in cfg.hooks

    setup_cmd = ("uv sync --group dev",)
    existing = cfg.hooks.get("post_worktree_create", [])
    cfg.hooks["post_worktree_create"] = list(setup_cmd) + existing

    assert cfg.hooks["post_worktree_create"] == ["uv sync --group dev"]


def test_provenance_defaults_not_in_sources():
    """Keys that were never set by any layer should show as 'default'."""
    cfg, prov = load_config_with_provenance(None)
    assert "branch_prefix" not in prov.sources
    output = prov.format(cfg)
    assert "branch_prefix: 'ralph/'  (default)" in output


def test_provenance_format_output():
    """Format output should include key, value, and source."""
    prov = ConfigProvenance(sources={"branch_prefix": "myfile.yaml"})
    cfg = load_config(None)
    cfg.branch_prefix = "test/"
    output = prov.format(cfg)
    assert "branch_prefix: 'test/'  (myfile.yaml)" in output


def test_load_max_consecutive_infra_failures(tmp_path):
    """orchestrated.max_consecutive_infra_failures is parsed from YAML."""
    config_file = tmp_path / "ralph++.yaml"
    config_file.write_text(yaml.dump({"orchestrated": {"max_consecutive_infra_failures": 5}}))
    cfg = load_config(config_file)
    assert cfg.orchestrated.max_consecutive_infra_failures == 5


def test_max_consecutive_infra_failures_default():
    cfg = load_config(None)
    assert cfg.orchestrated.max_consecutive_infra_failures == 3


def test_max_consecutive_infra_failures_negative_rejected(tmp_path):
    config_file = tmp_path / "ralph++.yaml"
    config_file.write_text(yaml.dump({"orchestrated": {"max_consecutive_infra_failures": -1}}))
    with pytest.raises(ValueError, match="max_consecutive_infra_failures"):
        load_config(config_file)


def test_load_non_interactive_defaults(tmp_path):
    config_file = tmp_path / "ralph++.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "non_interactive": {
                    "enabled": True,
                    "on_max_cycles_prd": "abort",
                    "on_max_cycles_prd_json": "retry-once",
                    "on_max_cycles_post": "continue",
                }
            }
        )
    )
    cfg = load_config(config_file)
    assert cfg.non_interactive.enabled is True
    assert cfg.non_interactive.on_max_cycles_prd == "abort"
    assert cfg.non_interactive.on_max_cycles_prd_json == "retry-once"
    assert cfg.non_interactive.on_max_cycles_post == "continue"


def test_non_interactive_defaults_when_absent():
    cfg = load_config(None)
    assert cfg.non_interactive.enabled is False
    assert cfg.non_interactive.on_max_cycles_prd == "continue"
    assert cfg.non_interactive.on_max_cycles_prd_json == "continue"
    assert cfg.non_interactive.on_max_cycles_post == "continue"


def test_non_interactive_invalid_policy_rejected(tmp_path):
    config_file = tmp_path / "ralph++.yaml"
    config_file.write_text(yaml.dump({"non_interactive": {"on_max_cycles_prd": "bogus"}}))
    with pytest.raises(ValueError, match="on_max_cycles"):
        load_config(config_file)


def test_on_retry_exhaustion_default():
    cfg = load_config(None)
    assert cfg.orchestrated.on_retry_exhaustion == "skip-story"


def test_on_retry_exhaustion_from_yaml(tmp_path):
    config_file = tmp_path / "ralph++.yaml"
    config_file.write_text(yaml.dump({"orchestrated": {"on_retry_exhaustion": "abort"}}))
    cfg = load_config(config_file)
    assert cfg.orchestrated.on_retry_exhaustion == "abort"


def test_on_retry_exhaustion_invalid_rejected(tmp_path):
    config_file = tmp_path / "ralph++.yaml"
    config_file.write_text(yaml.dump({"orchestrated": {"on_retry_exhaustion": "bogus"}}))
    with pytest.raises(ValueError, match="on_retry_exhaustion"):
        load_config(config_file)


def test_design_stance_defaults():
    cfg = load_config(None)
    assert cfg.design_stance.implementation_scope == "unspecified"
    assert cfg.design_stance.backward_compatibility == "unspecified"
    assert cfg.design_stance.existing_tests == "unspecified"
    assert cfg.design_stance.api_stability == "unspecified"
    assert cfg.design_stance.notes == ""


def test_design_stance_from_yaml(tmp_path):
    config_file = tmp_path / "ralph++.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "design_stance": {
                    "implementation_scope": "single_pass",
                    "backward_compatibility": "required",
                    "existing_tests": "must_pass",
                    "api_stability": "extend_only",
                    "notes": "no rocket science",
                }
            }
        )
    )
    cfg = load_config(config_file)
    assert cfg.design_stance.implementation_scope == "single_pass"
    assert cfg.design_stance.backward_compatibility == "required"
    assert cfg.design_stance.existing_tests == "must_pass"
    assert cfg.design_stance.api_stability == "extend_only"
    assert cfg.design_stance.notes == "no rocket science"


def test_design_stance_invalid_value_rejected(tmp_path):
    config_file = tmp_path / "ralph++.yaml"
    config_file.write_text(yaml.dump({"design_stance": {"implementation_scope": "bogus"}}))
    with pytest.raises(ValueError, match="implementation_scope"):
        load_config(config_file)


# ── worktree_root (#151 / #153) ─────────────────────────────────────────────


def test_worktree_root_defaults_to_none():
    """With no worktree_root in config, the field defaults to None so
    create_worktree falls back to repo_path.parent."""
    cfg = load_config(None)
    assert cfg.worktree_root is None


def test_worktree_root_loaded_from_file(tmp_path):
    """A worktree_root YAML value is loaded and expanded to an absolute path."""
    config_file = tmp_path / "ralph++.yaml"
    custom_root = tmp_path / "my-worktrees"
    config_file.write_text(yaml.dump({"worktree_root": str(custom_root)}))

    cfg = load_config(config_file)
    assert cfg.worktree_root == custom_root.resolve()


def test_worktree_root_expands_home(tmp_path, monkeypatch):
    """A `~`-prefixed worktree_root is expanded via the shared _expand helper."""
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    config_file = tmp_path / "ralph++.yaml"
    config_file.write_text(yaml.dump({"worktree_root": "~/ralph-wts"}))

    cfg = load_config(config_file)
    assert cfg.worktree_root == (fake_home / "ralph-wts").resolve()


def test_worktree_root_explicit_null_keeps_default(tmp_path):
    """An explicit null in YAML means "use default", not "crash"."""
    config_file = tmp_path / "ralph++.yaml"
    config_file.write_text("worktree_root: null\n")

    cfg = load_config(config_file)
    assert cfg.worktree_root is None
