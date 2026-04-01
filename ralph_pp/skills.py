"""Skill availability checking and installation for Claude Code plugins."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.prompt import Confirm

from .config import Config, ToolConfig

console = Console()

# Skills required for PRD generation and conversion.
REQUIRED_SKILLS = ("prd", "ralph")

# Bundled plugin directory (relative to this file).
_BUNDLED_PLUGIN_DIR = Path(__file__).parent / "plugin"

# Default local marketplace location.
_LOCAL_MARKETPLACE = Path("~/.claude/plugins/local").expanduser()

# Plugin name used in the local marketplace.
_PLUGIN_NAME = "ralph-skills"


def is_claude_tool(tool_config: ToolConfig) -> bool:
    """Return True if the tool uses a Claude CLI command."""
    return tool_config.command.startswith("claude")


def _plugin_search_dirs(claude_config_dir: Path) -> list[Path]:
    """Return all directories that may contain plugins (marketplace and local)."""
    plugins_dir = claude_config_dir / "plugins"
    dirs: list[Path] = []

    # Marketplace plugins
    marketplaces = plugins_dir / "marketplaces"
    if marketplaces.is_dir():
        for marketplace in marketplaces.iterdir():
            if not marketplace.is_dir():
                continue
            for sub in ("plugins", "external_plugins"):
                candidate = marketplace / sub
                if candidate.is_dir():
                    dirs.append(candidate)

    # Local marketplace
    local_plugins = _LOCAL_MARKETPLACE / "plugins"
    if local_plugins.is_dir() and local_plugins not in dirs:
        dirs.append(local_plugins)

    return dirs


def find_skill(name: str, plugin_dirs: list[Path]) -> Path | None:
    """Search plugin directories for a skill by name.

    Returns the path to SKILL.md if found, None otherwise.
    """
    for plugins_root in plugin_dirs:
        if not plugins_root.is_dir():
            continue
        for plugin in plugins_root.iterdir():
            skill_file = plugin / "skills" / name / "SKILL.md"
            if skill_file.is_file():
                return skill_file
    return None


def check_skills(
    names: list[str],
    claude_config_dir: Path,
) -> dict[str, Path | None]:
    """Check whether each named skill is installed.

    Returns a mapping of skill name to found path (or None if missing).
    """
    plugin_dirs = _plugin_search_dirs(claude_config_dir)
    return {name: find_skill(name, plugin_dirs) for name in names}


def install_skills_plugin(
    target_dir: Path | None = None,
    source_dir: Path | None = None,
) -> Path:
    """Install the bundled ralph-skills plugin into the local marketplace.

    Creates the local marketplace structure if needed and copies the
    bundled plugin into it.  Returns the installed plugin directory.
    """
    source = source_dir or _BUNDLED_PLUGIN_DIR
    target = (target_dir or _LOCAL_MARKETPLACE) / "plugins" / _PLUGIN_NAME

    if not source.is_dir():
        raise RuntimeError(f"Bundled plugin directory not found: {source}")

    # Create local marketplace structure if needed.
    marketplace_meta = (target_dir or _LOCAL_MARKETPLACE) / ".claude-plugin"
    marketplace_meta.mkdir(parents=True, exist_ok=True)
    marketplace_json = marketplace_meta / "marketplace.json"
    if not marketplace_json.exists():
        marketplace_json.write_text(
            json.dumps(
                {
                    "name": "local",
                    "description": "Locally installed Claude Code plugins",
                    "owner": {"name": "local"},
                    "plugins": [
                        {
                            "name": _PLUGIN_NAME,
                            "description": "PRD generation and conversion skills for Ralph",
                            "source": f"./plugins/{_PLUGIN_NAME}",
                        }
                    ],
                },
                indent=2,
            )
            + "\n"
        )

    # Copy plugin tree.
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)

    return target


def _update_settings(claude_config_dir: Path) -> None:
    """Add the local marketplace and plugin to Claude settings if not present."""
    settings_file = claude_config_dir / "settings.json"
    settings: dict[str, Any] = {}
    if settings_file.exists():
        settings = json.loads(settings_file.read_text())

    changed = False

    # Register the local marketplace.
    extra: dict[str, Any] = settings.setdefault("extraKnownMarketplaces", {})
    if "local" not in extra:
        extra["local"] = {
            "source": {
                "source": "directory",
                "path": str(_LOCAL_MARKETPLACE),
            }
        }
        changed = True

    # Enable the plugin.
    enabled: dict[str, Any] = settings.setdefault("enabledPlugins", {})
    plugin_key = f"{_PLUGIN_NAME}@local"
    if plugin_key not in enabled:
        enabled[plugin_key] = True
        changed = True

    if changed:
        settings_file.write_text(json.dumps(settings, indent=4) + "\n")


def ensure_prd_skills(config: Config, project_path: Path) -> None:
    """Ensure the prd and ralph skills are installed for the PRD tool.

    If the prd_tool is not Claude-based, this is a no-op.
    If skills are missing, prompts the user to install them or exits.
    """
    prd_tool_cfg = config.get_tool(config.prd_tool)
    if not is_claude_tool(prd_tool_cfg):
        return

    status = check_skills(list(REQUIRED_SKILLS), config.claude_config_dir)
    missing = [name for name, path in status.items() if path is None]

    if not missing:
        return

    console.print(
        f"[yellow]The following required Claude skills are not installed: "
        f"{', '.join(missing)}[/yellow]"
    )

    if not sys.stdin.isatty():
        raise RuntimeError(
            f"Required Claude skills not installed: {', '.join(missing)}. "
            "Run ralph++ interactively to install them, or install the "
            "ralph-skills plugin manually."
        )

    if not Confirm.ask("Install the ralph-skills plugin now?", default=True):
        raise SystemExit(1)

    plugin_dir = install_skills_plugin()
    _update_settings(config.claude_config_dir)
    console.print(f"[green]Installed ralph-skills plugin to {plugin_dir}[/green]")
    console.print(
        "[dim]Note: you may need to restart Claude Code or run /reload-plugins "
        "for the skills to take effect.[/dim]"
    )
