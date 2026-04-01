"""Tests for skill availability checking and installation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ralph_pp.config import Config, ToolConfig
from ralph_pp.skills import (
    _PLUGIN_NAME,
    check_skills,
    ensure_prd_skills,
    find_skill,
    install_skills_plugin,
    is_claude_tool,
)


class TestIsClaudeTool:
    def test_claude_command(self):
        assert is_claude_tool(ToolConfig(command="claude")) is True

    def test_claude_interactive_command(self):
        assert is_claude_tool(ToolConfig(command="claude-code")) is True

    def test_codex_command(self):
        assert is_claude_tool(ToolConfig(command="codex")) is False

    def test_custom_command(self):
        assert is_claude_tool(ToolConfig(command="my-wrapper")) is False


class TestFindSkill:
    def test_finds_skill_in_plugin_dir(self, tmp_path: Path):
        skill_file = tmp_path / "my-plugin" / "skills" / "prd" / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("# PRD Skill")

        result = find_skill("prd", [tmp_path])
        assert result == skill_file

    def test_returns_none_when_missing(self, tmp_path: Path):
        result = find_skill("prd", [tmp_path])
        assert result is None

    def test_searches_multiple_dirs(self, tmp_path: Path):
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()

        skill_file = dir2 / "my-plugin" / "skills" / "ralph" / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("# Ralph Skill")

        result = find_skill("ralph", [dir1, dir2])
        assert result == skill_file

    def test_ignores_nonexistent_dirs(self):
        result = find_skill("prd", [Path("/nonexistent/dir")])
        assert result is None


class TestCheckSkills:
    def test_all_present(self, tmp_path: Path):
        claude_dir = tmp_path / ".claude"
        plugins = claude_dir / "plugins" / "marketplaces" / "test" / "plugins"
        for name in ("prd", "ralph"):
            skill = plugins / "my-plugin" / "skills" / name / "SKILL.md"
            skill.parent.mkdir(parents=True, exist_ok=True)
            skill.write_text(f"# {name}")

        result = check_skills(["prd", "ralph"], claude_dir)
        assert result["prd"] is not None
        assert result["ralph"] is not None

    def test_some_missing(self, tmp_path: Path):
        claude_dir = tmp_path / ".claude"
        plugins = claude_dir / "plugins" / "marketplaces" / "test" / "plugins"
        skill = plugins / "my-plugin" / "skills" / "prd" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("# prd")

        result = check_skills(["prd", "ralph"], claude_dir)
        assert result["prd"] is not None
        assert result["ralph"] is None

    def test_all_missing(self, tmp_path: Path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        result = check_skills(["prd", "ralph"], claude_dir)
        assert result["prd"] is None
        assert result["ralph"] is None


class TestInstallSkillsPlugin:
    def test_creates_plugin_structure(self, tmp_path: Path):
        # Use the real bundled plugin as source
        from ralph_pp.skills import _BUNDLED_PLUGIN_DIR

        target_dir = tmp_path / "local"
        plugin_dir = install_skills_plugin(target_dir=target_dir, source_dir=_BUNDLED_PLUGIN_DIR)

        assert plugin_dir == target_dir / "plugins" / _PLUGIN_NAME
        assert (plugin_dir / ".claude-plugin" / "plugin.json").is_file()
        assert (plugin_dir / "skills" / "prd" / "SKILL.md").is_file()
        assert (plugin_dir / "skills" / "ralph" / "SKILL.md").is_file()

    def test_creates_marketplace_json(self, tmp_path: Path):
        from ralph_pp.skills import _BUNDLED_PLUGIN_DIR

        target_dir = tmp_path / "local"
        install_skills_plugin(target_dir=target_dir, source_dir=_BUNDLED_PLUGIN_DIR)

        marketplace_json = target_dir / ".claude-plugin" / "marketplace.json"
        assert marketplace_json.is_file()
        data = json.loads(marketplace_json.read_text())
        assert data["name"] == "local"

    def test_overwrites_existing_plugin(self, tmp_path: Path):
        from ralph_pp.skills import _BUNDLED_PLUGIN_DIR

        target_dir = tmp_path / "local"
        # Install twice — should not error
        install_skills_plugin(target_dir=target_dir, source_dir=_BUNDLED_PLUGIN_DIR)
        install_skills_plugin(target_dir=target_dir, source_dir=_BUNDLED_PLUGIN_DIR)

        assert (target_dir / "plugins" / _PLUGIN_NAME / "skills" / "prd" / "SKILL.md").is_file()

    def test_raises_when_source_missing(self, tmp_path: Path):
        with pytest.raises(RuntimeError, match="not found"):
            install_skills_plugin(
                target_dir=tmp_path / "local", source_dir=tmp_path / "nonexistent"
            )

    def test_preserves_existing_marketplace_json(self, tmp_path: Path):
        from ralph_pp.skills import _BUNDLED_PLUGIN_DIR

        target_dir = tmp_path / "local"
        meta = target_dir / ".claude-plugin"
        meta.mkdir(parents=True)
        (meta / "marketplace.json").write_text('{"name": "custom", "plugins": []}')

        install_skills_plugin(target_dir=target_dir, source_dir=_BUNDLED_PLUGIN_DIR)

        data = json.loads((meta / "marketplace.json").read_text())
        assert data["name"] == "custom"  # Not overwritten


class TestEnsurePrdSkills:
    def _make_config(self, tmp_path: Path, tool_command: str = "claude") -> Config:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(exist_ok=True)
        return Config(
            claude_config_dir=claude_dir,
            tools={
                "claude-interactive": ToolConfig(
                    command=tool_command,
                    args=["-p", "{prompt}"],
                    interactive=True,
                ),
            },
        )

    def test_skips_non_claude_tool(self, tmp_path: Path):
        config = self._make_config(tmp_path, tool_command="codex")
        # Should not raise, even though skills are missing
        ensure_prd_skills(config, tmp_path)

    def test_noop_when_skills_present(self, tmp_path: Path):
        config = self._make_config(tmp_path)
        # Install skills so they're found
        plugins_dir = config.claude_config_dir / "plugins" / "marketplaces" / "test" / "plugins"
        for name in ("prd", "ralph"):
            skill = plugins_dir / "my-plugin" / "skills" / name / "SKILL.md"
            skill.parent.mkdir(parents=True, exist_ok=True)
            skill.write_text(f"# {name}")

        # Should not raise or prompt
        ensure_prd_skills(config, tmp_path)

    @patch("ralph_pp.skills.sys")
    @patch("ralph_pp.skills.Confirm.ask", return_value=True)
    @patch("ralph_pp.skills.install_skills_plugin")
    @patch("ralph_pp.skills._update_settings")
    def test_prompts_and_installs_when_missing(
        self, mock_settings, mock_install, mock_ask, mock_sys, tmp_path: Path
    ):
        mock_sys.stdin.isatty.return_value = True
        config = self._make_config(tmp_path)
        mock_install.return_value = tmp_path / "installed"

        ensure_prd_skills(config, tmp_path)

        mock_ask.assert_called_once()
        mock_install.assert_called_once()
        mock_settings.assert_called_once_with(config.claude_config_dir)

    @patch("ralph_pp.skills.sys")
    @patch("ralph_pp.skills.Confirm.ask", return_value=False)
    def test_exits_when_user_declines(self, mock_ask, mock_sys, tmp_path: Path):
        mock_sys.stdin.isatty.return_value = True
        config = self._make_config(tmp_path)

        with pytest.raises(SystemExit):
            ensure_prd_skills(config, tmp_path)

    @patch("ralph_pp.skills.sys")
    def test_raises_in_non_interactive(self, mock_sys, tmp_path: Path):
        mock_sys.stdin.isatty.return_value = False
        config = self._make_config(tmp_path)

        with pytest.raises(RuntimeError, match="not installed"):
            ensure_prd_skills(config, tmp_path)
