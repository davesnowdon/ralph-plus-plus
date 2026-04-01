"""Tests for config loading."""

import tempfile
from pathlib import Path

import pytest
import yaml

from ralph_pp.config import (
    Config,
    OrchestratedConfig,
    PostReviewConfig,
    PrdReviewConfig,
    RalphConfig,
    ToolConfig,
    _deep_merge,
    _parse_bool,
    detect_test_commands,
    discover_config_files,
    format_effective_config,
    load_config,
    resolve_sandbox_dir,
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
    assert orch.test_commands == []
    assert orch.backout_on_failure is True
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
        tools={"claude": ToolConfig(), "codex": ToolConfig()},
        ralph=RalphConfig(mode="invalid"),
    )
    with pytest.raises(ValueError, match="ralph.mode"):
        validate_config(cfg)


def test_validate_config_bad_sandbox_tool():
    import pytest

    cfg = Config(
        tools={"claude": ToolConfig()},
        ralph=RalphConfig(sandbox_tool="nonexistent"),
    )
    with pytest.raises(ValueError, match="ralph.sandbox_tool"):
        validate_config(cfg)


def test_validate_config_bad_orchestrated_coder():
    import pytest

    cfg = Config(
        tools={"claude": ToolConfig(), "codex": ToolConfig()},
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


def test_validate_config_empty_test_command():
    import pytest

    cfg = Config(
        tools={"claude": ToolConfig(), "codex": ToolConfig()},
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
        tools={"claude": ToolConfig(), "codex": ToolConfig()},
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
    """Default prd_tool should be 'claude'."""
    cfg = load_config(None)
    assert cfg.prd_tool == "claude"


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
        tools={"claude": ToolConfig(), "codex": ToolConfig()},
        prd_tool="nonexistent",
    )
    with pytest.raises(ValueError, match="prd_tool"):
        validate_config(cfg)


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


# ── resolve_sandbox_dir tests ────────────────────────────────────────


def _make_fake_sandbox(base: Path) -> Path:
    """Create a minimal fake ralph-sandbox directory structure."""
    sandbox = base / "ralph-sandbox"
    (sandbox / "bin").mkdir(parents=True)
    wrapper = sandbox / "bin" / "ralph-sandbox"
    wrapper.write_text("#!/bin/sh\necho fake")
    wrapper.chmod(0o755)
    return sandbox


def test_sandbox_resolution_from_config(tmp_path):
    """Explicit sandbox_dir in config should be used."""
    sandbox = _make_fake_sandbox(tmp_path)
    cfg = Config(
        tools={"claude": ToolConfig(), "codex": ToolConfig()},
        ralph=RalphConfig(sandbox_dir=str(sandbox)),
    )
    assert resolve_sandbox_dir(cfg) == sandbox


def test_sandbox_resolution_env_var(tmp_path, monkeypatch):
    """RALPH_SANDBOX_DIR env var should be used when config is empty."""
    sandbox = _make_fake_sandbox(tmp_path)
    monkeypatch.setenv("RALPH_SANDBOX_DIR", str(sandbox))
    cfg = Config(
        tools={"claude": ToolConfig(), "codex": ToolConfig()},
        ralph=RalphConfig(sandbox_dir=""),
    )
    assert resolve_sandbox_dir(cfg) == sandbox


def test_sandbox_resolution_which(tmp_path, monkeypatch):
    """PATH lookup via shutil.which should resolve sandbox."""
    sandbox = _make_fake_sandbox(tmp_path)
    wrapper = sandbox / "bin" / "ralph-sandbox"
    monkeypatch.delenv("RALPH_SANDBOX_DIR", raising=False)
    monkeypatch.setattr(
        "shutil.which", lambda name: str(wrapper) if name == "ralph-sandbox" else None
    )
    cfg = Config(
        tools={"claude": ToolConfig(), "codex": ToolConfig()},
        ralph=RalphConfig(sandbox_dir=""),
    )
    assert resolve_sandbox_dir(cfg) == sandbox


def test_sandbox_resolution_sibling(tmp_path, monkeypatch):
    """Sibling checkout should be discovered."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    sandbox = _make_fake_sandbox(tmp_path)
    monkeypatch.delenv("RALPH_SANDBOX_DIR", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    cfg = Config(
        repo_path=repo,
        tools={"claude": ToolConfig(), "codex": ToolConfig()},
        ralph=RalphConfig(sandbox_dir=""),
    )
    assert resolve_sandbox_dir(cfg) == sandbox.resolve()


def test_sandbox_resolution_fails_cleanly(tmp_path, monkeypatch):
    """Should raise FileNotFoundError with clear guidance."""
    monkeypatch.delenv("RALPH_SANDBOX_DIR", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    cfg = Config(
        repo_path=tmp_path,
        tools={"claude": ToolConfig(), "codex": ToolConfig()},
        ralph=RalphConfig(sandbox_dir=""),
    )
    with pytest.raises(FileNotFoundError, match="Could not find ralph-sandbox"):
        resolve_sandbox_dir(cfg)


# ── detect_test_commands tests ───────────────────────────────────────


def test_detect_test_commands_makefile(tmp_path):
    """Makefile with 'test:' target should be detected."""
    (tmp_path / "Makefile").write_text("all:\n\techo hi\n\ntest:\n\tpytest\n")
    assert detect_test_commands(tmp_path) == ["make test"]


def test_detect_test_commands_pytest(tmp_path):
    """pyproject.toml with [tool.pytest] should detect pytest."""
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    assert detect_test_commands(tmp_path) == ["pytest"]


def test_detect_test_commands_node(tmp_path):
    """package.json should detect npm test."""
    (tmp_path / "package.json").write_text('{"name": "test"}')
    assert detect_test_commands(tmp_path) == ["npm test"]


def test_detect_test_commands_rust(tmp_path):
    """Cargo.toml should detect cargo test."""
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert detect_test_commands(tmp_path) == ["cargo test"]


def test_detect_test_commands_go(tmp_path):
    """go.mod should detect go test."""
    (tmp_path / "go.mod").write_text("module example.com/foo\n")
    assert detect_test_commands(tmp_path) == ["go test ./..."]


def test_detect_test_commands_none(tmp_path):
    """Empty directory should return no commands."""
    assert detect_test_commands(tmp_path) == []


def test_detect_test_commands_makefile_priority(tmp_path):
    """Makefile test target should take priority over language-specific detection."""
    (tmp_path / "Makefile").write_text("test:\n\tpytest && ruff check .\n")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    assert detect_test_commands(tmp_path) == ["make test"]


# ── format_effective_config tests ────────────────────────────────────


def test_format_effective_config():
    """format_effective_config should produce valid YAML."""
    cfg = load_config(None)
    output = format_effective_config(cfg)
    assert isinstance(output, str)
    parsed = yaml.safe_load(output)
    assert parsed["branch_prefix"] == "ralph/"
    assert parsed["prd_tool"] == "claude"
    assert "claude" in parsed["tools"]
