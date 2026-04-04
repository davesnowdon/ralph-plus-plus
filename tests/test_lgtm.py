"""Tests for ToolResult.is_lgtm semantics."""

from ralph_pp.tools.base import ToolResult


def _result(output: str, stderr: str = "") -> ToolResult:
    return ToolResult(output=output, exit_code=0, success=True, stderr=stderr)


class TestIsLgtm:
    def test_exact_lgtm(self):
        assert _result("LGTM").is_lgtm is True

    def test_lgtm_with_whitespace(self):
        assert _result("  LGTM  ").is_lgtm is True

    def test_lgtm_with_trailing_newline(self):
        assert _result("LGTM\n").is_lgtm is True

    def test_lgtm_first_line_multiline(self):
        assert _result("LGTM\nGood work on this iteration.").is_lgtm is True

    def test_not_lgtm(self):
        assert _result("not LGTM").is_lgtm is False

    def test_lgtm_question(self):
        assert _result("LGTM? no").is_lgtm is False

    def test_lgtm_substring_in_prose(self):
        assert _result("This is LGTM approved").is_lgtm is False

    def test_empty_output(self):
        assert _result("").is_lgtm is False

    def test_lgtm_in_middle_of_line(self):
        assert _result("The code is LGTM").is_lgtm is False

    def test_multiline_lgtm_not_first_line(self):
        assert _result("Reviewed changes.\nLGTM").is_lgtm is False

    # ── Case-insensitive matching ──────────────────────────────────────

    def test_lowercase_lgtm(self):
        assert _result("lgtm").is_lgtm is True

    def test_mixed_case_lgtm(self):
        assert _result("Lgtm").is_lgtm is True

    # ── Common model variations ────────────────────────────────────────

    def test_lgtm_exclamation(self):
        assert _result("LGTM!").is_lgtm is True

    def test_lgtm_period(self):
        assert _result("LGTM.").is_lgtm is True

    # ── stderr must not interfere ──────────────────────────────────────

    def test_lgtm_with_stderr_noise(self):
        assert _result("LGTM", stderr="Warning: deprecated flag").is_lgtm is True

    def test_stderr_containing_lgtm_doesnt_match(self):
        assert _result("findings here", stderr="LGTM").is_lgtm is False
