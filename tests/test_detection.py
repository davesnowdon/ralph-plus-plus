"""Tests for test command auto-detection."""

import tempfile
from pathlib import Path

import yaml

from ralph_pp.config import load_config
from ralph_pp.detection import detect_test_commands


def test_detect_test_commands_makefile(tmp_path):
    """Makefile with 'test:' target should be detected."""
    (tmp_path / "Makefile").write_text("all:\n\techo hi\n\ntest:\n\tpytest\n")
    assert detect_test_commands(tmp_path) == ["make test"]


def test_detect_test_commands_pytest(tmp_path):
    """pyproject.toml with [tool.pytest] should detect pytest."""
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    assert detect_test_commands(tmp_path) == ["pytest"]


def test_detect_test_commands_pytest_ini(tmp_path):
    """pytest.ini should detect pytest."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    assert detect_test_commands(tmp_path) == ["pytest"]


def test_detect_test_commands_setup_cfg(tmp_path):
    """setup.cfg should detect pytest."""
    (tmp_path / "setup.cfg").write_text("[tool:pytest]\n")
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


def test_detect_test_commands_polyglot_first_match(tmp_path):
    """Polyglot repo should return only the first matched ecosystem."""
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "package.json").write_text('{"name": "test"}')
    result = detect_test_commands(tmp_path)
    assert result == ["pytest"], f"Expected first-match ['pytest'], got {result}"


def test_detect_test_commands_polyglot_node_and_rust(tmp_path):
    """Node + Rust repo should return only npm test (first match)."""
    (tmp_path / "package.json").write_text('{"name": "test"}')
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    result = detect_test_commands(tmp_path)
    assert result == ["npm test"]


def test_detect_test_commands_wired_into_load_config(tmp_path):
    """load_config should auto-populate test_commands when detection succeeds."""
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
    data = {
        "repo_path": str(tmp_path),
        "orchestrated": {"run_tests_between_steps": True},
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        config_path = Path(f.name)

    cfg = load_config(config_path)
    assert cfg.orchestrated.test_commands == ["make test"]
    config_path.unlink()


def test_detect_test_commands_not_wired_when_disabled(tmp_path):
    """load_config should NOT auto-populate when both triggers are disabled."""
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
    data = {
        "repo_path": str(tmp_path),
        "orchestrated": {
            "run_tests_between_steps": False,
            "auto_allow_test_commands": False,
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        config_path = Path(f.name)

    cfg = load_config(config_path)
    assert cfg.orchestrated.test_commands == []
    config_path.unlink()


def test_detect_test_commands_not_overridden_when_configured(tmp_path):
    """Explicit test_commands should not be replaced by auto-detection."""
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
    data = {
        "repo_path": str(tmp_path),
        "orchestrated": {
            "run_tests_between_steps": True,
            "test_commands": ["custom-test"],
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        config_path = Path(f.name)

    cfg = load_config(config_path)
    assert cfg.orchestrated.test_commands == ["custom-test"]
    config_path.unlink()
