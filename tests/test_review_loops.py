"""Tests for review loop tool failure handling in prd.py and post_review.py."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from ralph_pp.config import Config, ToolConfig, ReviewConfig
from ralph_pp.tools.base import ToolResult
from ralph_pp.steps.prd import review_prd_loop
from ralph_pp.steps.post_review import post_review_loop


def _make_config(review_cfg: ReviewConfig | None = None) -> Config:
    cfg = Config(tools={
        "claude": ToolConfig(command="claude", args=["--print"], stdin="{prompt}"),
        "codex": ToolConfig(command="codex", args=["{prompt}"]),
    })
    if review_cfg:
        cfg.prd_review = review_cfg
        cfg.post_review = review_cfg
    return cfg


def _ok_result(output: str = "LGTM") -> ToolResult:
    return ToolResult(output=output, exit_code=0, success=True)


def _fail_result(output: str = "segfault") -> ToolResult:
    return ToolResult(output=output, exit_code=1, success=False)


class TestPrdReviewLoopToolFailures:
    def test_reviewer_failure_raises(self, tmp_path):
        review_cfg = ReviewConfig(
            reviewer="codex", fixer="claude",
            reviewer_prompt="Review {prd_file}", fixer_prompt="Fix {prd_file} {findings}",
            max_cycles=3, enabled=True,
        )
        config = _make_config(review_cfg)
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _fail_result()
            fixer_mock = MagicMock()
            mock_make.side_effect = lambda name, cfg: reviewer_mock if name == "codex" else fixer_mock

            with pytest.raises(RuntimeError, match="PRD reviewer failed"):
                review_prd_loop(prd_file, tmp_path, config)

        fixer_mock.run.assert_not_called()

    def test_fixer_failure_raises(self, tmp_path):
        review_cfg = ReviewConfig(
            reviewer="codex", fixer="claude",
            reviewer_prompt="Review {prd_file}", fixer_prompt="Fix {prd_file} {findings}",
            max_cycles=3, enabled=True,
        )
        config = _make_config(review_cfg)
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("Issues found here")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _fail_result()
            mock_make.side_effect = lambda name, cfg: reviewer_mock if name == "codex" else fixer_mock

            with pytest.raises(RuntimeError, match="PRD fixer failed"):
                review_prd_loop(prd_file, tmp_path, config)


class TestPostReviewLoopToolFailures:
    def test_reviewer_failure_raises(self, tmp_path):
        review_cfg = ReviewConfig(
            reviewer="codex", fixer="claude",
            reviewer_prompt="Review {prd_file}", fixer_prompt="Fix {prd_file} {findings}",
            max_cycles=3, enabled=True,
        )
        config = _make_config(review_cfg)
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{}')

        with patch("ralph_pp.steps.post_review.make_tool") as mock_make:
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _fail_result()
            fixer_mock = MagicMock()
            mock_make.side_effect = lambda name, cfg: reviewer_mock if name == "codex" else fixer_mock

            with pytest.raises(RuntimeError, match="Post-run reviewer failed"):
                post_review_loop(tmp_path, config)

        fixer_mock.run.assert_not_called()

    def test_fixer_failure_raises(self, tmp_path):
        review_cfg = ReviewConfig(
            reviewer="codex", fixer="claude",
            reviewer_prompt="Review {prd_file}", fixer_prompt="Fix {prd_file} {findings}",
            max_cycles=3, enabled=True,
        )
        config = _make_config(review_cfg)
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{}')

        with patch("ralph_pp.steps.post_review.make_tool") as mock_make:
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("Issues found")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _fail_result()
            mock_make.side_effect = lambda name, cfg: reviewer_mock if name == "codex" else fixer_mock

            with pytest.raises(RuntimeError, match="Post-run fixer failed"):
                post_review_loop(tmp_path, config)
