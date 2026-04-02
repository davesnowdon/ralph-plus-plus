"""Tests for review loop tool failure handling in prd.py and post_review.py."""

from unittest.mock import MagicMock, patch

import pytest

from ralph_pp.config import Config, PostReviewConfig, PrdReviewConfig, ToolConfig
from ralph_pp.steps.post_review import post_review_loop
from ralph_pp.steps.prd import MaxCyclesAbort, prompt_max_cycles, review_prd_loop
from ralph_pp.tools.base import ToolResult


def _make_config(
    prd_review: PrdReviewConfig | None = None,
    post_review: PostReviewConfig | None = None,
) -> Config:
    cfg = Config(
        tools={
            "claude": ToolConfig(
                command="claude",
                args=["--print"],
                stdin="{prompt}",
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash(git:*)"],
            ),
            "codex": ToolConfig(command="codex", args=["{prompt}"]),
        }
    )
    if prd_review:
        cfg.prd_review = prd_review
    if post_review:
        cfg.post_review = post_review
    return cfg


def _ok_result(output: str = "LGTM") -> ToolResult:
    return ToolResult(output=output, exit_code=0, success=True)


def _fail_result(output: str = "segfault") -> ToolResult:
    return ToolResult(output=output, exit_code=1, success=False)


def _review_cfg(
    cls: type = PrdReviewConfig,
    max_cycles: int = 3,
) -> PrdReviewConfig | PostReviewConfig:
    return cls(
        reviewer="codex",
        fixer="claude",
        reviewer_prompt="Review {prd_file}{previous_findings}",
        fixer_prompt="Fix {prd_file} {findings}",
        max_cycles=max_cycles,
        enabled=True,
    )


class TestPrdReviewLoopToolFailures:
    def test_reviewer_failure_raises(self, tmp_path):
        config = _make_config(prd_review=_review_cfg())
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _fail_result()
            fixer_mock = MagicMock()
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            with pytest.raises(RuntimeError, match="PRD reviewer failed"):
                review_prd_loop(prd_file, tmp_path, config)

        fixer_mock.run.assert_not_called()

    def test_fixer_failure_raises(self, tmp_path):
        config = _make_config(prd_review=_review_cfg())
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("Issues found here")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _fail_result()
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            with pytest.raises(RuntimeError, match="PRD fixer failed"):
                review_prd_loop(prd_file, tmp_path, config)


class TestPrdReviewLoopMaxCycles:
    def test_quit_raises_max_cycles_abort(self, tmp_path):
        config = _make_config(prd_review=_review_cfg(max_cycles=1))
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch("ralph_pp.steps.prd.prompt_max_cycles", return_value="quit"),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: major\nproblem: bad")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            with pytest.raises(MaxCyclesAbort):
                review_prd_loop(prd_file, tmp_path, config)

    def test_continue_returns_normally(self, tmp_path):
        config = _make_config(prd_review=_review_cfg(max_cycles=1))
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch("ralph_pp.steps.prd.prompt_max_cycles", return_value="continue"),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: major\nproblem: bad")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            # Should return without raising
            review_prd_loop(prd_file, tmp_path, config)

    def test_retry_runs_another_batch(self, tmp_path):
        config = _make_config(prd_review=_review_cfg(max_cycles=1))
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        prompt_responses = iter(["retry", "continue"])

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch(
                "ralph_pp.steps.prd.prompt_max_cycles",
                side_effect=lambda *a: next(prompt_responses),
            ),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: major\nproblem: bad")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            review_prd_loop(prd_file, tmp_path, config)

        # max_cycles=1, retry once + continue = 2 review calls
        assert reviewer_mock.run.call_count == 2
        assert fixer_mock.run.call_count == 2

    def test_lgtm_on_retry_exits_early(self, tmp_path):
        config = _make_config(prd_review=_review_cfg(max_cycles=1))
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch("ralph_pp.steps.prd.prompt_max_cycles", return_value="retry"),
        ):
            reviewer_mock = MagicMock()
            # First call: issues, second call: LGTM
            reviewer_mock.run.side_effect = [
                _ok_result("1. severity: major\nproblem: bad"),
                _ok_result("LGTM"),
            ]
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            review_prd_loop(prd_file, tmp_path, config)

        assert reviewer_mock.run.call_count == 2
        assert fixer_mock.run.call_count == 1  # fixer only called for the first issue


class TestPrdReviewPreviousFindings:
    def test_previous_findings_passed_to_reviewer(self, tmp_path):
        config = _make_config(prd_review=_review_cfg(max_cycles=2))
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            reviewer_mock = MagicMock()
            reviewer_mock.run.side_effect = [
                _ok_result("1. severity: major\nproblem: ordering unclear"),
                _ok_result("LGTM"),
            ]
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            review_prd_loop(prd_file, tmp_path, config)

        # Second reviewer call should contain the previous findings
        second_call_prompt = reviewer_mock.run.call_args_list[1].kwargs.get(
            "prompt", reviewer_mock.run.call_args_list[1][1].get("prompt", "")
        )
        if not second_call_prompt:
            second_call_prompt = str(reviewer_mock.run.call_args_list[1])
        assert "ordering unclear" in second_call_prompt


class TestPostReviewLoopToolFailures:
    def test_reviewer_failure_raises(self, tmp_path):
        config = _make_config(post_review=_review_cfg(cls=PostReviewConfig))
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text("{}")

        with patch("ralph_pp.steps.post_review.make_tool") as mock_make:
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _fail_result()
            fixer_mock = MagicMock()
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            with pytest.raises(RuntimeError, match="Post-run reviewer failed"):
                post_review_loop(tmp_path, config)

        fixer_mock.run.assert_not_called()

    def test_fixer_failure_raises(self, tmp_path):
        config = _make_config(post_review=_review_cfg(cls=PostReviewConfig))
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text("{}")

        with patch("ralph_pp.steps.post_review.make_tool") as mock_make:
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("Issues found")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _fail_result()
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            with pytest.raises(RuntimeError, match="Post-run fixer failed"):
                post_review_loop(tmp_path, config)


class TestPostReviewLoopMaxCycles:
    def test_quit_raises_max_cycles_abort(self, tmp_path):
        config = _make_config(post_review=_review_cfg(cls=PostReviewConfig, max_cycles=1))
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text("{}")

        with (
            patch("ralph_pp.steps.post_review.make_tool") as mock_make,
            patch("ralph_pp.steps.post_review.prompt_max_cycles", return_value="quit"),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: major\nproblem: bad")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            with pytest.raises(MaxCyclesAbort):
                post_review_loop(tmp_path, config)

    def test_continue_returns_normally(self, tmp_path):
        config = _make_config(post_review=_review_cfg(cls=PostReviewConfig, max_cycles=1))
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text("{}")

        with (
            patch("ralph_pp.steps.post_review.make_tool") as mock_make,
            patch("ralph_pp.steps.post_review.prompt_max_cycles", return_value="continue"),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: major\nproblem: bad")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            post_review_loop(tmp_path, config)


class TestPromptMaxCycles:
    def test_choice_1_returns_quit(self):
        with patch("ralph_pp.steps.prd.click.prompt", return_value="1"):
            assert prompt_max_cycles("PRD", 3) == "quit"

    def test_choice_2_returns_retry(self):
        with patch("ralph_pp.steps.prd.click.prompt", return_value="2"):
            assert prompt_max_cycles("PRD", 3) == "retry"

    def test_choice_3_returns_continue(self):
        with patch("ralph_pp.steps.prd.click.prompt", return_value="3"):
            assert prompt_max_cycles("PRD", 3) == "continue"
