"""Auto-detection of test commands for common project types."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def detect_test_commands(repo_path: Path) -> list[str]:
    """Auto-detect test commands for common project types.

    Uses a first-match strategy among language ecosystems to avoid noisy
    failures in polyglot repos.  A Makefile with a ``test`` target always
    takes priority over language-specific detection.

    Returns an empty list when nothing is found.
    """
    commands: list[str] = []

    # Makefile with a 'test' target takes priority
    makefile = repo_path / "Makefile"
    if makefile.is_file():
        try:
            text = makefile.read_text()
            if "\ntest:" in text or "\ntest :" in text or text.startswith("test:"):
                commands.append("make test")
        except OSError:
            pass

    if not commands:
        # Language-specific detectors in priority order; first match wins.
        detectors: list[tuple[Path, str | None, str]] = [
            # (marker file, extra-content check substring, command)
            (repo_path / "pytest.ini", None, "pytest"),
            (repo_path / "setup.cfg", None, "pytest"),
            (repo_path / "pyproject.toml", "[tool.pytest", "pytest"),
            (repo_path / "package.json", None, "npm test"),
            (repo_path / "Cargo.toml", None, "cargo test"),
            (repo_path / "go.mod", None, "go test ./..."),
        ]

        matched: list[str] = []
        for marker, content_check, cmd in detectors:
            if not marker.is_file():
                continue
            if content_check is not None:
                try:
                    text = marker.read_text()
                    if content_check not in text:
                        continue
                except OSError:
                    continue
            matched.append(cmd)

        if matched:
            commands.append(matched[0])
            if len(matched) > 1:
                skipped = matched[1:]
                logger.warning(
                    "Polyglot repo detected — using %r, skipping %s. "
                    "Set orchestrated.test_commands explicitly to override.",
                    matched[0],
                    skipped,
                )

    return commands
