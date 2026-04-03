"""Tests for severity parsing helpers in ralph_pp.tools.base."""

from ralph_pp.tools.base import parse_max_severity, severity_at_or_above


class TestParseMaxSeverity:
    def test_critical(self):
        assert parse_max_severity("severity: critical\nsome detail") == "critical"

    def test_major(self):
        assert parse_max_severity("1. severity: major\nfile: foo.py") == "major"

    def test_minor_only(self):
        assert parse_max_severity("- severity: minor\nfile: bar.py") == "minor"

    def test_mixed_returns_highest(self):
        text = (
            "1. severity: minor\nfile: a.py\n"
            "2. severity: major\nfile: b.py\n"
            "3. severity: minor\nfile: c.py"
        )
        assert parse_max_severity(text) == "major"

    def test_mixed_with_critical(self):
        text = "severity: minor\nseverity: critical\nseverity: major"
        assert parse_max_severity(text) == "critical"

    def test_no_labels_returns_none(self):
        assert parse_max_severity("LGTM") is None
        assert parse_max_severity("everything looks fine") is None

    def test_case_insensitive(self):
        assert parse_max_severity("Severity: MINOR") == "minor"
        assert parse_max_severity("SEVERITY: Major") == "major"
        assert parse_max_severity("severity: CRITICAL") == "critical"

    def test_numbered_list_format(self):
        text = "1. severity: major\n   file: `foo.py`\n   problem: something"
        assert parse_max_severity(text) == "major"

    def test_empty_string(self):
        assert parse_max_severity("") is None


class TestSeverityAtOrAbove:
    def test_minor_at_minor(self):
        assert severity_at_or_above("minor", "minor") is True

    def test_major_at_minor(self):
        assert severity_at_or_above("major", "minor") is True

    def test_critical_at_minor(self):
        assert severity_at_or_above("critical", "minor") is True

    def test_minor_at_major(self):
        assert severity_at_or_above("minor", "major") is False

    def test_major_at_major(self):
        assert severity_at_or_above("major", "major") is True

    def test_critical_at_major(self):
        assert severity_at_or_above("critical", "major") is True

    def test_minor_at_critical(self):
        assert severity_at_or_above("minor", "critical") is False

    def test_major_at_critical(self):
        assert severity_at_or_above("major", "critical") is False

    def test_critical_at_critical(self):
        assert severity_at_or_above("critical", "critical") is True

    def test_case_insensitive(self):
        assert severity_at_or_above("MAJOR", "minor") is True
        assert severity_at_or_above("Minor", "MAJOR") is False
