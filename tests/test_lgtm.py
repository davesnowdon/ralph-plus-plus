"""Tests for ToolResult.is_lgtm exact-match semantics."""

from ralph_pp.tools.base import ToolResult


def _result(output: str) -> ToolResult:
    return ToolResult(output=output, exit_code=0, success=True)


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

    def test_lowercase_lgtm(self):
        assert _result("lgtm").is_lgtm is False

    def test_empty_output(self):
        assert _result("").is_lgtm is False

    def test_lgtm_in_middle_of_line(self):
        assert _result("The code is LGTM").is_lgtm is False

    def test_multiline_lgtm_not_first_line(self):
        assert _result("Reviewed changes.\nLGTM").is_lgtm is False
