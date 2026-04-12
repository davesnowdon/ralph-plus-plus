"""Tests for sandbox discovery and validation."""

from pathlib import Path

import pytest
from ralph_pp.config import Config, RalphConfig, ToolConfig
from ralph_pp.sandbox import resolve_sandbox_dir


def _make_fake_sandbox(base: Path) -> Path:
    """Create a minimal fake ralph-sandbox directory structure."""
    sandbox = base / "ralph-sandbox"
    (sandbox / "bin").mkdir(parents=True)
    wrapper = sandbox / "bin" / "ralph-sandbox"
    wrapper.write_text("#!/bin/sh\necho fake")
    wrapper.chmod(0o755)
    (sandbox / "docker-compose.yml").write_text("version: '3'\n")
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


def test_sandbox_resolution_rejects_standalone_install(tmp_path, monkeypatch):
    """PATH resolution should skip a standalone install without docker-compose.yml."""
    # Create a fake standalone binary (no docker-compose.yml)
    standalone = tmp_path / "usr" / "local" / "bin"
    standalone.mkdir(parents=True)
    wrapper = standalone / "ralph-sandbox"
    wrapper.write_text("#!/bin/sh\necho fake")
    wrapper.chmod(0o755)

    monkeypatch.delenv("RALPH_SANDBOX_DIR", raising=False)
    monkeypatch.setattr(
        "shutil.which",
        lambda name: str(wrapper) if name == "ralph-sandbox" else None,
    )
    cfg = Config(
        repo_path=tmp_path,
        tools={"claude": ToolConfig(), "codex": ToolConfig()},
        ralph=RalphConfig(sandbox_dir=""),
    )
    with pytest.raises(FileNotFoundError, match="Could not find ralph-sandbox"):
        resolve_sandbox_dir(cfg)
