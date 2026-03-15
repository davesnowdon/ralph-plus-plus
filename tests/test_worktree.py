"""Tests for branch name generation."""

from ralph_pp.config import load_config
from ralph_pp.steps.worktree import make_branch_name


def test_branch_name_slugified():
    cfg = load_config(None)
    name = make_branch_name("Add User Authentication", cfg)
    assert name.startswith("ralph/")
    assert "add-user-authentication" in name
    # suffix appended
    parts = name.split("-")
    assert len(parts[-1]) == cfg.branch_suffix_length


def test_branch_name_unique():
    cfg = load_config(None)
    names = {make_branch_name("same feature", cfg) for _ in range(10)}
    # With random suffixes, all 10 should be unique
    assert len(names) == 10


def test_branch_name_special_chars():
    cfg = load_config(None)
    name = make_branch_name("Fix: user@email.com validation!", cfg)
    # Should not contain special chars
    assert "@" not in name
    assert "!" not in name
    assert ":" not in name
