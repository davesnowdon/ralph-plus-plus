"""Tests for sandbox command construction, helpers, and delegated mode."""

import tempfile
from pathlib import Path

from ralph_pp.config import load_config
from ralph_pp.steps._prompts import render_prompt
from ralph_pp.steps.sandbox import (
    _build_sandbox_command,
    _run_delegated,
)


def _make_config_with_sandbox_dir(sandbox_dir: str, mode: str = "delegated"):
    """Helper: create a config with sandbox_dir pointing to a real directory."""
    import yaml

    data = {
        "ralph": {
            "mode": mode,
            "sandbox_dir": sandbox_dir,
            "sandbox_tool": "claude",
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        tmp_path = Path(f.name)
    cfg = load_config(tmp_path)
    tmp_path.unlink()
    return cfg


def test_build_sandbox_command_delegated(tmp_path):
    """Delegated mode command has no --session-runner."""
    # Create a fake bin/ralph-sandbox
    sandbox_dir = tmp_path / "ralph-sandbox"
    (sandbox_dir / "bin").mkdir(parents=True)
    wrapper = sandbox_dir / "bin" / "ralph-sandbox"
    wrapper.write_text("#!/bin/bash\necho fake")
    wrapper.chmod(0o755)
    (sandbox_dir / "docker-compose.yml").write_text("version: '3'\n")

    cfg = _make_config_with_sandbox_dir(str(sandbox_dir))
    worktree = tmp_path / "project"
    worktree.mkdir()

    cmd = _build_sandbox_command(
        worktree,
        cfg,
        tool="claude",
        ralph_args=["20"],
    )

    assert str(wrapper) in cmd
    assert "--project-dir" in cmd
    assert str(worktree) in cmd
    assert "--tool" in cmd
    assert "claude" in cmd
    assert "--session-runner" not in cmd
    assert "20" in cmd


def test_build_sandbox_command_with_session_runner(tmp_path):
    """Orchestrated mode command includes --session-runner."""
    sandbox_dir = tmp_path / "ralph-sandbox"
    (sandbox_dir / "bin").mkdir(parents=True)
    wrapper = sandbox_dir / "bin" / "ralph-sandbox"
    wrapper.write_text("#!/bin/bash\necho fake")
    wrapper.chmod(0o755)
    (sandbox_dir / "docker-compose.yml").write_text("version: '3'\n")

    cfg = _make_config_with_sandbox_dir(str(sandbox_dir), mode="orchestrated")
    worktree = tmp_path / "project"
    worktree.mkdir()

    runner = tmp_path / "runner.sh"
    runner.write_text("#!/bin/bash\necho run")

    cmd = _build_sandbox_command(
        worktree,
        cfg,
        tool="codex",
        session_runner=runner,
        ralph_args=["1"],
    )

    assert "--session-runner" in cmd
    assert str(runner) in cmd
    assert "codex" in cmd
    assert "1" in cmd


def test_build_sandbox_command_passes_config_dirs(tmp_path):
    """Both claude and codex config dirs are passed."""
    sandbox_dir = tmp_path / "ralph-sandbox"
    (sandbox_dir / "bin").mkdir(parents=True)
    wrapper = sandbox_dir / "bin" / "ralph-sandbox"
    wrapper.write_text("#!/bin/bash\necho fake")
    wrapper.chmod(0o755)
    (sandbox_dir / "docker-compose.yml").write_text("version: '3'\n")

    cfg = _make_config_with_sandbox_dir(str(sandbox_dir))
    worktree = tmp_path / "project"
    worktree.mkdir()

    cmd = _build_sandbox_command(worktree, cfg, tool="claude")

    assert "--claude-config-dir" in cmd
    assert "--codex-config-dir" in cmd


def testrender_prompt():
    """Prompt template placeholders are substituted."""
    template = "Review {diff} against {prd_file}"
    result = render_prompt(template, diff="my diff", prd_file="/path/prd.json")
    assert result == "Review my diff against /path/prd.json"


def testrender_prompt_missing_placeholder(caplog):
    """Missing placeholders are left as-is and a warning is emitted."""
    import logging

    template = "Review {diff} and {unknown}"
    with caplog.at_level(logging.WARNING, logger="ralph_pp.steps.sandbox"):
        result = render_prompt(template, diff="changes")
    assert result == "Review changes and {unknown}"
    assert "unsubstituted placeholders" in caplog.text
    assert "{unknown}" in caplog.text


def test_delegated_mode_integration(tmp_path):
    """Integration test: delegated mode invokes the wrapper with correct args."""
    sandbox_dir = tmp_path / "ralph-sandbox"
    (sandbox_dir / "bin").mkdir(parents=True)
    wrapper = sandbox_dir / "bin" / "ralph-sandbox"
    # Fake wrapper that records its args and exits 0
    wrapper.write_text(
        '#!/bin/bash\necho "ARGS: $@" > "$(dirname "$0")/../invocation.log"\nexit 0\n'
    )
    wrapper.chmod(0o755)
    (sandbox_dir / "docker-compose.yml").write_text("version: '3'\n")

    cfg = _make_config_with_sandbox_dir(str(sandbox_dir))
    worktree = tmp_path / "project"
    worktree.mkdir()

    result = _run_delegated(worktree, cfg)

    assert result is True
    log = (sandbox_dir / "invocation.log").read_text()
    assert "--project-dir" in log
    assert str(worktree) in log
    assert "--tool" in log
    assert "claude" in log
    assert "20" in log  # default max_iterations


def test_delegated_mode_returns_false_on_failure(tmp_path):
    """Delegated mode returns False when wrapper exits nonzero."""
    sandbox_dir = tmp_path / "ralph-sandbox"
    (sandbox_dir / "bin").mkdir(parents=True)
    wrapper = sandbox_dir / "bin" / "ralph-sandbox"
    wrapper.write_text("#!/bin/bash\nexit 1\n")
    wrapper.chmod(0o755)
    (sandbox_dir / "docker-compose.yml").write_text("version: '3'\n")

    cfg = _make_config_with_sandbox_dir(str(sandbox_dir))
    worktree = tmp_path / "project"
    worktree.mkdir()

    result = _run_delegated(worktree, cfg)

    assert result is False
