"""Tests for review loop tool failure handling in prd.py and post_review.py."""

from unittest.mock import MagicMock, patch

import pytest
from ralph_pp.config import (
    Config,
    PostReviewConfig,
    PrdJsonReviewConfig,
    PrdReviewConfig,
    ToolConfig,
)
from ralph_pp.steps.post_review import post_review_loop
from ralph_pp.steps.prd import (
    MaxCyclesAbort,
    prompt_max_cycles,
    review_prd_json_loop,
    review_prd_loop,
)
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

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch("ralph_pp.steps.prd.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.prd.get_diff", return_value="(no diff)"),
        ):
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
            patch("ralph_pp.steps.prd.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.prd.get_diff", return_value="(no diff)"),
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
            patch("ralph_pp.steps.prd.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.prd.get_diff", return_value="(no diff)"),
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
                side_effect=lambda *a, **kw: next(prompt_responses),
            ),
            patch("ralph_pp.steps.prd.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.prd.get_diff", return_value="(no diff)"),
        ):
            reviewer_mock = MagicMock()
            # Different findings each cycle so the #118 convergence detector
            # doesn't short-circuit before we reach the user prompt.
            reviewer_mock.run.side_effect = [
                _ok_result("1. severity: major\nproblem: alpha beta gamma needs work"),
                _ok_result("1. severity: major\nproblem: delta epsilon zeta different issue"),
            ]
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
            patch("ralph_pp.steps.prd.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.prd.get_diff", return_value="(no diff)"),
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

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch("ralph_pp.steps.prd.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.prd.get_diff", return_value="(no diff)"),
        ):
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
        prd_json.write_text('{"userStories": []}')

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
        prd_json.write_text('{"userStories": []}')

        with (
            patch("ralph_pp.steps.post_review.make_tool") as mock_make,
            patch("ralph_pp.steps.post_review.make_tool_with_permissions") as mock_make_aug,
            patch("ralph_pp.steps.post_review.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.post_review.get_diff", return_value="(no diff)"),
            patch(
                "ralph_pp.steps.post_review.run_test_commands_with_output",
                return_value=(True, ""),
            ),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("Issues found")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _fail_result()
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )
            mock_make_aug.side_effect = lambda name, cfg, cmds: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            with pytest.raises(RuntimeError, match="Post-run fixer failed"):
                post_review_loop(tmp_path, config)


class TestPostReviewLoopMaxCycles:
    def test_quit_raises_max_cycles_abort(self, tmp_path):
        config = _make_config(post_review=_review_cfg(cls=PostReviewConfig, max_cycles=1))
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        with (
            patch("ralph_pp.steps.post_review.make_tool") as mock_make,
            patch("ralph_pp.steps.post_review.make_tool_with_permissions") as mock_make_aug,
            patch("ralph_pp.steps.post_review.prompt_max_cycles", return_value="quit"),
            patch("ralph_pp.steps.post_review.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.post_review.get_diff", return_value="(no diff)"),
            patch(
                "ralph_pp.steps.post_review.run_test_commands_with_output",
                return_value=(True, ""),
            ),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: major\nproblem: bad")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )
            mock_make_aug.side_effect = lambda name, cfg, cmds: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            with pytest.raises(MaxCyclesAbort):
                post_review_loop(tmp_path, config)

    def test_continue_returns_normally(self, tmp_path):
        config = _make_config(post_review=_review_cfg(cls=PostReviewConfig, max_cycles=1))
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        with (
            patch("ralph_pp.steps.post_review.make_tool") as mock_make,
            patch("ralph_pp.steps.post_review.make_tool_with_permissions") as mock_make_aug,
            patch("ralph_pp.steps.post_review.prompt_max_cycles", return_value="continue"),
            patch("ralph_pp.steps.post_review.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.post_review.get_diff", return_value="(no diff)"),
            patch(
                "ralph_pp.steps.post_review.run_test_commands_with_output",
                return_value=(True, ""),
            ),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: major\nproblem: bad")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )
            mock_make_aug.side_effect = lambda name, cfg, cmds: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            post_review_loop(tmp_path, config)


class TestPromptMaxCycles:
    """Interactive-path tests for ``prompt_max_cycles``.

    The pytest environment has no TTY, so by default
    :func:`is_non_interactive` returns True and the function short-circuits
    the click prompt. Each test here patches ``is_non_interactive`` to
    ``False`` to exercise the real stdin path.
    """

    def test_choice_1_returns_quit(self):
        with (
            patch("ralph_pp.steps.prd.is_non_interactive", return_value=False),
            patch("ralph_pp.steps.prd.click.prompt", return_value="1"),
        ):
            assert prompt_max_cycles("PRD", 3) == "quit"

    def test_choice_2_returns_retry(self):
        with (
            patch("ralph_pp.steps.prd.is_non_interactive", return_value=False),
            patch("ralph_pp.steps.prd.click.prompt", return_value="2"),
        ):
            assert prompt_max_cycles("PRD", 3) == "retry"

    def test_choice_3_returns_continue(self):
        with (
            patch("ralph_pp.steps.prd.is_non_interactive", return_value=False),
            patch("ralph_pp.steps.prd.click.prompt", return_value="3"),
        ):
            assert prompt_max_cycles("PRD", 3) == "continue"

    def test_custom_continue_label(self, capsys):
        with (
            patch("ralph_pp.steps.prd.is_non_interactive", return_value=False),
            patch("ralph_pp.steps.prd.click.prompt", return_value="3"),
        ):
            result = prompt_max_cycles(
                "Post-run", 3, continue_label="Accept — finish without reviewer approval"
            )
        assert result == "continue"


class TestPromptMaxCyclesNonInteractive:
    """Non-interactive path for ``prompt_max_cycles`` — issue #128.

    In unattended runs (no TTY, RALPH_NON_INTERACTIVE=1, or
    non_interactive.enabled=True) the function must NOT read stdin.
    """

    def test_skips_stdin_and_applies_continue_policy(self):
        # Default stdin patching: pytest has no TTY, so non-interactive path runs.
        with patch("ralph_pp.steps.prd.click.prompt") as mock_click:
            action = prompt_max_cycles("PRD", 3, policy="continue")
        assert action == "continue"
        mock_click.assert_not_called()

    def test_abort_policy_returns_quit(self):
        with patch("ralph_pp.steps.prd.click.prompt") as mock_click:
            action = prompt_max_cycles("PRD", 3, policy="abort")
        assert action == "quit"
        mock_click.assert_not_called()

    def test_retry_once_returns_retry_then_continue(self):
        with patch("ralph_pp.steps.prd.click.prompt") as mock_click:
            first = prompt_max_cycles("PRD", 3, policy="retry-once", retry_used=False)
            second = prompt_max_cycles("PRD", 3, policy="retry-once", retry_used=True)
        assert first == "retry"
        assert second == "continue"
        mock_click.assert_not_called()

    def test_env_var_forces_non_interactive(self, monkeypatch):
        from ralph_pp.steps.prd import is_non_interactive

        monkeypatch.setenv("RALPH_NON_INTERACTIVE", "1")
        # Even with a fake TTY, env var wins.
        with patch("ralph_pp.steps.prd.sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            assert is_non_interactive() is True

    def test_config_enabled_forces_non_interactive(self):
        from ralph_pp.config import NonInteractiveConfig
        from ralph_pp.steps.prd import is_non_interactive

        with patch("ralph_pp.steps.prd.sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            assert is_non_interactive(NonInteractiveConfig(enabled=True)) is True

    def test_tty_without_overrides_is_interactive(self, monkeypatch):
        from ralph_pp.config import NonInteractiveConfig
        from ralph_pp.steps.prd import is_non_interactive

        monkeypatch.delenv("RALPH_NON_INTERACTIVE", raising=False)
        with patch("ralph_pp.steps.prd.sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            assert is_non_interactive(NonInteractiveConfig(enabled=False)) is False

    def test_prd_review_loop_continues_unattended(self, tmp_path):
        """End-to-end: PRD review hitting max cycles under non-interactive default."""
        config = _make_config(prd_review=_review_cfg(max_cycles=1))
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch("ralph_pp.steps.prd.click.prompt") as mock_click,
            patch("ralph_pp.steps.prd.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.prd.get_diff", return_value="(no diff)"),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: major\nproblem: bad")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            # Must return cleanly, not raise or hang.
            review_prd_loop(prd_file, tmp_path, config)

        mock_click.assert_not_called()

    def test_post_review_loop_continues_unattended(self, tmp_path):
        """End-to-end: post-run review hitting max cycles under non-interactive default."""
        config = _make_config(post_review=_review_cfg(cls=PostReviewConfig, max_cycles=1))
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        with (
            patch("ralph_pp.steps.post_review.make_tool") as mock_make,
            patch("ralph_pp.steps.prd.click.prompt") as mock_click,
            patch("ralph_pp.steps.post_review.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.post_review.get_diff", return_value="(no diff)"),
            patch(
                "ralph_pp.steps.post_review.run_test_commands_with_output",
                return_value=(True, ""),
            ),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: major\nproblem: bad")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            result = post_review_loop(tmp_path, config)
        assert result.outcome == "accepted"
        mock_click.assert_not_called()

    def test_prd_review_loop_aborts_when_policy_abort(self, tmp_path):
        """Config-level abort policy should raise MaxCyclesAbort without prompting."""
        config = _make_config(prd_review=_review_cfg(max_cycles=1))
        config.non_interactive.on_max_cycles_prd = "abort"
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch("ralph_pp.steps.prd.click.prompt") as mock_click,
            patch("ralph_pp.steps.prd.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.prd.get_diff", return_value="(no diff)"),
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

        mock_click.assert_not_called()


class TestPostReviewFixer:
    def test_fixer_gets_test_command_permissions(self, tmp_path):
        """When test_commands are configured, both reviewer and fixer get augmented permissions."""
        from ralph_pp.config import OrchestratedConfig

        config = _make_config(post_review=_review_cfg(cls=PostReviewConfig, max_cycles=1))
        config.orchestrated = OrchestratedConfig(
            test_commands=["hatch run ci", "pytest"],
            auto_allow_test_commands=True,
        )
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        with (
            patch("ralph_pp.steps.post_review.make_tool") as mock_make,
            patch("ralph_pp.steps.post_review.make_tool_with_permissions") as mock_make_augmented,
            patch("ralph_pp.steps.post_review.prompt_max_cycles", return_value="quit"),
            patch("ralph_pp.steps.post_review.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.post_review.get_diff", return_value="(no diff)"),
            patch(
                "ralph_pp.steps.post_review.run_test_commands_with_output",
                return_value=(True, ""),
            ),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: major\nproblem: bad")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make_augmented.side_effect = lambda name, cfg, cmds: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            with pytest.raises(MaxCyclesAbort):
                post_review_loop(tmp_path, config)

        # Both reviewer and fixer use augmented make_tool_with_permissions
        assert mock_make_augmented.call_count == 2
        mock_make.assert_not_called()

    def test_fixer_not_augmented_when_disabled(self, tmp_path):
        """When auto_allow_test_commands is False, both use plain make_tool."""
        from ralph_pp.config import OrchestratedConfig

        config = _make_config(post_review=_review_cfg(cls=PostReviewConfig, max_cycles=1))
        config.orchestrated = OrchestratedConfig(
            test_commands=["hatch run ci"],
            auto_allow_test_commands=False,
        )
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        with (
            patch("ralph_pp.steps.post_review.make_tool") as mock_make,
            patch("ralph_pp.steps.post_review.make_tool_with_permissions") as mock_make_augmented,
            patch("ralph_pp.steps.post_review.prompt_max_cycles", return_value="quit"),
            patch("ralph_pp.steps.post_review.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.post_review.get_diff", return_value="(no diff)"),
            patch(
                "ralph_pp.steps.post_review.run_test_commands_with_output",
                return_value=(True, ""),
            ),
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

        mock_make_augmented.assert_not_called()


class TestPostReviewTestCommandsGuidance:
    """Post-run reviewer prompt includes test command guidance when configured."""

    def test_post_reviewer_prompt_includes_test_commands(self, tmp_path):
        from ralph_pp.config import OrchestratedConfig

        config = _make_config(
            post_review=PostReviewConfig(
                reviewer="codex",
                fixer="claude",
                max_cycles=1,
            ),
        )
        config.orchestrated = OrchestratedConfig(test_commands=["hatch run ci"])

        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        with (
            patch("ralph_pp.steps.post_review.make_tool") as mock_make,
            patch("ralph_pp.steps.post_review.make_tool_with_permissions") as mock_make_aug,
            patch("ralph_pp.steps.post_review.prompt_max_cycles", return_value="quit"),
            patch("ralph_pp.steps.post_review.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.post_review.get_diff", return_value="(no diff)"),
            patch(
                "ralph_pp.steps.post_review.run_test_commands_with_output",
                return_value=(True, ""),
            ),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: minor\nfoo")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )
            mock_make_aug.side_effect = lambda name, cfg, cmds: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            with pytest.raises(MaxCyclesAbort):
                post_review_loop(tmp_path, config)

        # Check the reviewer was called with test commands guidance
        prompt = reviewer_mock.run.call_args[1].get(
            "prompt", reviewer_mock.run.call_args[0][0] if reviewer_mock.run.call_args[0] else ""
        )
        assert "hatch run ci" in prompt
        assert "Do NOT run bare pytest" in prompt


class TestPostReviewDiff:
    """Post-run reviewer prompt includes diff when .base-sha is available."""

    def test_diff_included_when_base_sha_exists(self, tmp_path):
        from ralph_pp.config import OrchestratedConfig

        config = _make_config(
            post_review=PostReviewConfig(
                reviewer="codex",
                fixer="claude",
                max_cycles=1,
            ),
        )
        config.orchestrated = OrchestratedConfig()

        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        # Write a .base-sha file
        base_sha_file = tmp_path / "scripts" / "ralph" / ".base-sha"
        base_sha_file.write_text("abc1234")

        with (
            patch("ralph_pp.steps.post_review.make_tool") as mock_make,
            patch("ralph_pp.steps.post_review.prompt_max_cycles", return_value="quit"),
            patch("ralph_pp.steps.post_review.get_head_sha", return_value="def5678"),
            patch(
                "ralph_pp.steps.post_review.get_diff",
                return_value="diff --git a/foo b/foo\n+bar",
            ),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: minor\nfoo")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            with pytest.raises(MaxCyclesAbort):
                post_review_loop(tmp_path, config)

        prompt = reviewer_mock.run.call_args[1].get(
            "prompt", reviewer_mock.run.call_args[0][0] if reviewer_mock.run.call_args[0] else ""
        )
        assert "diff --git a/foo b/foo" in prompt
        assert "all changes since run start" in prompt

    def test_incremental_diff_on_subsequent_cycles(self, tmp_path):
        """#94: cycle 2+ should NOT include the full diff, only the fixer's
        incremental diff via the previous_findings context block."""
        from ralph_pp.config import OrchestratedConfig

        config = _make_config(
            post_review=PostReviewConfig(
                reviewer="codex",
                fixer="claude",
                max_cycles=3,
            ),
        )
        config.orchestrated = OrchestratedConfig()

        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')
        base_sha_file = tmp_path / "scripts" / "ralph" / ".base-sha"
        base_sha_file.write_text("abc1234")

        full_diff_str = "diff --git a/foo b/foo\n+the full original diff\n" * 50
        fixer_diff_str = "diff --git a/foo b/foo\n+small fix\n"
        prompts_seen: list[str] = []

        # First get_diff call returns the full diff; subsequent calls
        # return the fixer's incremental diff.
        diff_calls = {"n": 0}

        def fake_get_diff(*args, **kwargs):
            diff_calls["n"] += 1
            return full_diff_str if diff_calls["n"] == 1 else fixer_diff_str

        returns = [
            _ok_result("1. severity: major\nproblem: thing-a"),
            _ok_result("1. severity: major\nproblem: thing-b"),
            _ok_result("LGTM"),
        ]

        def reviewer_run(prompt, cwd):
            prompts_seen.append(prompt)
            return returns.pop(0)

        with (
            patch("ralph_pp.steps.post_review.make_tool") as mock_make,
            patch("ralph_pp.steps.post_review.get_head_sha", return_value="def5678"),
            patch("ralph_pp.steps.post_review.get_diff", side_effect=fake_get_diff),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.side_effect = reviewer_run
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            post_review_loop(tmp_path, config)

        assert len(prompts_seen) == 3, f"expected 3 reviewer calls, got {len(prompts_seen)}"

        # Cycle 1 includes the full diff
        assert "the full original diff" in prompts_seen[0]
        assert "all changes since run start" in prompts_seen[0]

        # Cycle 2+ omits the full diff
        assert "the full original diff" not in prompts_seen[1], (
            "cycle 2 must NOT include the full diff (#94)"
        )
        assert "the full original diff" not in prompts_seen[2]
        # ...but includes the fixer's incremental diff via the context block
        assert "small fix" in prompts_seen[1]
        # ...and the previous findings
        assert "thing-a" in prompts_seen[1]
        # ...and the explicit "omitted" marker
        assert "omitted on this cycle" in prompts_seen[1]

    def test_no_diff_when_base_sha_missing(self, tmp_path):
        from ralph_pp.config import OrchestratedConfig

        config = _make_config(
            post_review=PostReviewConfig(
                reviewer="codex",
                fixer="claude",
                max_cycles=1,
            ),
        )
        config.orchestrated = OrchestratedConfig()

        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        # No .base-sha file

        with (
            patch("ralph_pp.steps.post_review.make_tool") as mock_make,
            patch("ralph_pp.steps.post_review.prompt_max_cycles", return_value="quit"),
            patch("ralph_pp.steps.post_review.get_head_sha", return_value="def5678"),
            patch("ralph_pp.steps.post_review.get_diff", return_value="(no diff)"),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: minor\nfoo")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            with pytest.raises(MaxCyclesAbort):
                post_review_loop(tmp_path, config)

        prompt = reviewer_mock.run.call_args[1].get(
            "prompt", reviewer_mock.run.call_args[0][0] if reviewer_mock.run.call_args[0] else ""
        )
        assert "all changes since run start" not in prompt


class TestPostReviewRespectsTestFlag:
    """Post-review loop must respect run_tests_between_steps flag (#14)."""

    def test_tests_skipped_when_flag_false(self, tmp_path):
        from ralph_pp.config import OrchestratedConfig

        config = _make_config(
            post_review=PostReviewConfig(
                reviewer="codex",
                fixer="claude",
                max_cycles=1,
            ),
        )
        config.orchestrated = OrchestratedConfig(
            test_commands=["pytest"],
            run_tests_between_steps=False,
        )

        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        with (
            patch("ralph_pp.steps.post_review.make_tool") as mock_make,
            patch("ralph_pp.steps.post_review.make_tool_with_permissions") as mock_make_aug,
            patch("ralph_pp.steps.post_review.prompt_max_cycles", return_value="quit"),
            patch("ralph_pp.steps.post_review.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.post_review.get_diff", return_value="(no diff)"),
            patch(
                "ralph_pp.steps.post_review.run_test_commands_with_output",
            ) as mock_run_tests,
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: minor\nfoo")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )
            mock_make_aug.side_effect = lambda name, cfg, cmds: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            with pytest.raises(MaxCyclesAbort):
                post_review_loop(tmp_path, config)

        mock_run_tests.assert_not_called()

    def test_tests_run_when_flag_true(self, tmp_path):
        from ralph_pp.config import OrchestratedConfig

        config = _make_config(
            post_review=PostReviewConfig(
                reviewer="codex",
                fixer="claude",
                max_cycles=1,
            ),
        )
        config.orchestrated = OrchestratedConfig(
            test_commands=["pytest"],
            run_tests_between_steps=True,
        )

        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        with (
            patch("ralph_pp.steps.post_review.make_tool") as mock_make,
            patch("ralph_pp.steps.post_review.make_tool_with_permissions") as mock_make_aug,
            patch("ralph_pp.steps.post_review.prompt_max_cycles", return_value="quit"),
            patch("ralph_pp.steps.post_review.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.post_review.get_diff", return_value="(no diff)"),
            patch(
                "ralph_pp.steps.post_review.run_test_commands_with_output",
                return_value=(True, "all passed"),
            ) as mock_run_tests,
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: minor\nfoo")
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )
            mock_make_aug.side_effect = lambda name, cfg, cmds: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            with pytest.raises(MaxCyclesAbort):
                post_review_loop(tmp_path, config)

        mock_run_tests.assert_called()


class TestPrdReviewSeverityGating:
    """PRD review accepts when only minor findings remain (#111)."""

    def test_minor_only_findings_accepted(self, tmp_path):
        config = _make_config(prd_review=_review_cfg(max_cycles=3))
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch("ralph_pp.steps.prd.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.prd.get_diff", return_value="some diff"),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: minor\n   problem: style nit")
            fixer_mock = MagicMock()
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            review_prd_loop(prd_file, tmp_path, config)

        # Accepted on first cycle — fixer never called
        assert reviewer_mock.run.call_count == 1
        fixer_mock.run.assert_not_called()

    def test_major_findings_not_auto_accepted(self, tmp_path):
        config = _make_config(prd_review=_review_cfg(max_cycles=1))
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch("ralph_pp.steps.prd.prompt_max_cycles", return_value="continue"),
            patch("ralph_pp.steps.prd.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.prd.get_diff", return_value="some diff"),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result(
                "1. severity: major\n   problem: contract mismatch"
            )
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            review_prd_loop(prd_file, tmp_path, config)

        # Major finding triggers fixer
        fixer_mock.run.assert_called_once()


class TestPrdReviewNoDiffWarning:
    """PRD review warns when fixer produces no changes (#110)."""

    def test_no_diff_warning_shown(self, tmp_path, capsys):
        config = _make_config(prd_review=_review_cfg(max_cycles=1))
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch("ralph_pp.steps.prd.prompt_max_cycles", return_value="continue"),
            patch("ralph_pp.steps.prd.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.prd.get_diff", return_value="(no diff)"),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result(
                "1. severity: major\n   problem: infeasible"
            )
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            review_prd_loop(prd_file, tmp_path, config)

        # When fixer produces no diff, the next reviewer call should NOT get
        # "(no diff)" as the diff context — it should be empty
        if reviewer_mock.run.call_count > 1:
            second_prompt = reviewer_mock.run.call_args_list[1].kwargs.get(
                "prompt", str(reviewer_mock.run.call_args_list[1])
            )
            assert "(no diff)" not in second_prompt


class TestPrdJsonReviewMaxCycles:
    """prd.json review prompts on max_cycles like PRD review (#116)."""

    def _prd_json_review_cfg(self, max_cycles: int = 2) -> PrdJsonReviewConfig:
        return PrdJsonReviewConfig(
            reviewer="codex",
            fixer="claude",
            reviewer_prompt="Review {prd_file} {prd_json_file} {repo_path}",
            fixer_prompt="Fix {prd_json_file} {prd_file} {findings}",
            max_cycles=max_cycles,
            enabled=True,
        )

    def test_quit_raises_max_cycles_abort(self, tmp_path):
        config = _make_config()
        config.prd_json_review = self._prd_json_review_cfg(max_cycles=1)
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch("ralph_pp.steps.prd.prompt_max_cycles", return_value="quit"),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result(
                "1. severity: major\nproblem: criteria drift"
            )
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            with pytest.raises(MaxCyclesAbort):
                review_prd_json_loop(prd_file, prd_json, tmp_path, config)

    def test_continue_returns_normally(self, tmp_path):
        config = _make_config()
        config.prd_json_review = self._prd_json_review_cfg(max_cycles=1)
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch("ralph_pp.steps.prd.prompt_max_cycles", return_value="continue"),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result(
                "1. severity: major\nproblem: criteria drift"
            )
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            # Should return without raising
            review_prd_json_loop(prd_file, prd_json, tmp_path, config)

    def test_retry_runs_another_batch(self, tmp_path):
        config = _make_config()
        config.prd_json_review = self._prd_json_review_cfg(max_cycles=1)
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        prompt_responses = iter(["retry", "continue"])

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch(
                "ralph_pp.steps.prd.prompt_max_cycles",
                side_effect=lambda *a, **kw: next(prompt_responses),
            ),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result(
                "1. severity: major\nproblem: criteria drift"
            )
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = _ok_result("fixed")
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            review_prd_json_loop(prd_file, prd_json, tmp_path, config)

        # max_cycles=1, retry once + continue = 2 review calls
        assert reviewer_mock.run.call_count == 2

    def test_minor_only_findings_auto_accepted(self, tmp_path):
        config = _make_config()
        config.prd_json_review = self._prd_json_review_cfg(max_cycles=3)
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = _ok_result("1. severity: minor\nproblem: style nit")
            fixer_mock = MagicMock()
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            review_prd_json_loop(prd_file, prd_json, tmp_path, config)

        # Accepted on first cycle — fixer never called
        assert reviewer_mock.run.call_count == 1
        fixer_mock.run.assert_not_called()
