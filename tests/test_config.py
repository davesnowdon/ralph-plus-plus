"""Tests for config loading."""

from pathlib import Path
import tempfile
import yaml

from ralph_auto.config import load_config, Config


def test_load_empty_config():
    """Loading with no file returns sensible defaults."""
    cfg = load_config(None)
    assert isinstance(cfg, Config)
    assert cfg.branch_prefix == "ralph/"
    assert cfg.branch_suffix_length == 4
    assert cfg.ralph.max_iterations == 20
    assert "codex" in cfg.tools
    assert "claude" in cfg.tools


def test_load_config_from_file():
    """Values from YAML file are loaded correctly."""
    data = {
        "branch_prefix": "feature/",
        "branch_suffix_length": 6,
        "ralph": {"max_iterations": 30, "sandbox_image": "my-sandbox"},
        "hooks": {"post_worktree_create": ["echo hello"]},
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        tmp_path = Path(f.name)

    cfg = load_config(tmp_path)
    assert cfg.branch_prefix == "feature/"
    assert cfg.branch_suffix_length == 6
    assert cfg.ralph.max_iterations == 30
    assert cfg.ralph.sandbox_image == "my-sandbox"
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
