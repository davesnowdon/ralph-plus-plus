"""Tests for config loading."""

from pathlib import Path
import tempfile
import yaml

from ralph_pp.config import load_config, Config, OrchestratedConfig


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
