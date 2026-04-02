"""Tests for ralph_pp.tools.permissions."""

from ralph_pp.tools.permissions import bash_permissions_from_commands


class TestBashPermissionsFromCommands:
    def test_single_command(self) -> None:
        assert bash_permissions_from_commands(["pytest"]) == ["Bash(pytest:*)"]

    def test_multi_word_command(self) -> None:
        assert bash_permissions_from_commands(["hatch run ci"]) == ["Bash(hatch:*)"]

    def test_multiple_commands(self) -> None:
        result = bash_permissions_from_commands(["hatch run ci", "pytest", "ruff check ."])
        assert result == ["Bash(hatch:*)", "Bash(pytest:*)", "Bash(ruff:*)"]

    def test_deduplication(self) -> None:
        result = bash_permissions_from_commands(["make test", "make lint", "make check"])
        assert result == ["Bash(make:*)"]

    def test_empty_list(self) -> None:
        assert bash_permissions_from_commands([]) == []

    def test_whitespace_only_commands_skipped(self) -> None:
        assert bash_permissions_from_commands(["", "  ", "pytest"]) == ["Bash(pytest:*)"]

    def test_preserves_order(self) -> None:
        result = bash_permissions_from_commands(["npm test", "eslint .", "jest"])
        assert result == ["Bash(npm:*)", "Bash(eslint:*)", "Bash(jest:*)"]
