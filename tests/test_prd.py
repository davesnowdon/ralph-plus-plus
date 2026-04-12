"""Tests for PRD generation, conversion, and prd.json review."""

from unittest.mock import MagicMock, patch

import pytest
from ralph_pp.steps.prd import (
    convert_prd_to_json,
    feature_to_slug,
    generate_prd,
    review_prd_json_loop,
)
from ralph_pp.tools.base import ToolResult


def _make_config():
    """Minimal config with default tools."""
    from ralph_pp.config import Config, ToolConfig

    return Config(
        tools={
            "claude-interactive": ToolConfig(
                command="claude",
                args=["{prompt}"],
                interactive=True,
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash(git:*)"],
            ),
            "codex": ToolConfig(command="codex", args=["{prompt}"]),
            "claude": ToolConfig(
                command="claude",
                args=["--print"],
                stdin="{prompt}",
            ),
        }
    )


class TestFeatureToSlug:
    def test_simple(self):
        assert feature_to_slug("test feature") == "test-feature"

    def test_mixed_case_and_punctuation(self):
        result = feature_to_slug("Freeze Canonical Memory Contracts")
        assert result == "freeze-canonical-memory-contracts"

    def test_special_characters(self):
        assert feature_to_slug("add foo/bar support!") == "add-foobar-support"

    def test_multiple_spaces_and_dashes(self):
        assert feature_to_slug("  lots   of   spaces  ") == "lots-of-spaces"


class TestGeneratePrd:
    def test_raises_when_file_missing_after_success(self, tmp_path):
        """Exit 0 but tasks/prd-*.md not created should raise."""
        config = _make_config()
        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            with pytest.raises(RuntimeError, match="was not created"):
                generate_prd("test feature", tmp_path, config)

    def test_succeeds_when_file_exists(self, tmp_path):
        """Exit 0 with tasks/prd-test-feature.md present should succeed."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd-test-feature.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD\nSome content")

        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            result = generate_prd("test feature", tmp_path, config)
            assert result == prd_file

    def test_raises_on_tool_failure(self, tmp_path):
        """Non-zero exit should raise before file check."""
        config = _make_config()
        fake_result = ToolResult(output="Error", exit_code=1, success=False)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            with pytest.raises(RuntimeError, match="PRD generation failed"):
                generate_prd("test feature", tmp_path, config)

    def test_prd_prompt_used_when_provided(self, tmp_path):
        """When prd_prompt is given, it should appear in the prompt instead of feature."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd-short-name.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            generate_prd(
                "short-name",
                tmp_path,
                config,
                prd_prompt="Unify the dual memory systems behind a single canonical contract",
            )

            call_kwargs = mock_tool.run.call_args[1]
            assert "Unify the dual memory systems" in call_kwargs["prompt"]
            # The short feature name should NOT be in the prompt body
            # (it's only used for the filename)
            assert "Create a PRD for the following feature: short-name" not in call_kwargs["prompt"]

    def test_feature_used_when_prd_prompt_absent(self, tmp_path):
        """When prd_prompt is None, feature is used as the prompt."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd-my-feature.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            generate_prd("my-feature", tmp_path, config)

            call_kwargs = mock_tool.run.call_args[1]
            assert "Create a PRD for the following feature: my-feature" in call_kwargs["prompt"]

    def test_prd_prompt_does_not_affect_filename(self, tmp_path):
        """Filename should be derived from feature, not prd_prompt."""
        config = _make_config()
        # The file that should be created uses the feature slug
        prd_file = tmp_path / "tasks" / "prd-short-name.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            result = generate_prd(
                "short-name",
                tmp_path,
                config,
                prd_prompt="A very long description that would make a terrible filename",
            )

            assert result.name == "prd-short-name.md"


class TestConvertPrdToJson:
    def test_raises_when_json_missing_after_success(self, tmp_path):
        """Exit 0 but scripts/ralph/prd.json not created should raise."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            with pytest.raises(RuntimeError, match="was not created"):
                convert_prd_to_json(prd_file, tmp_path, config)

    def test_succeeds_when_json_exists(self, tmp_path):
        """Exit 0 with prd.json present should succeed."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"stories": []}')

        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            result = convert_prd_to_json(prd_file, tmp_path, config)
            assert result == prd_json

    def test_raises_when_json_invalid(self, tmp_path):
        """Exit 0 with malformed prd.json should raise."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text("this is not json {{{")

        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            with pytest.raises(RuntimeError, match="not valid JSON"):
                convert_prd_to_json(prd_file, tmp_path, config)

    def test_raises_when_json_empty(self, tmp_path):
        """Exit 0 with empty prd.json should raise."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text("")

        fake_result = ToolResult(output="Done", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.return_value = fake_result
            mock_make.return_value = mock_tool

            with pytest.raises(RuntimeError, match="not valid JSON"):
                convert_prd_to_json(prd_file, tmp_path, config)


class TestReviewPrdJsonLoop:
    def test_lgtm_on_first_cycle(self, tmp_path):
        """LGTM from reviewer on first cycle returns immediately."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        lgtm_result = ToolResult(output="LGTM", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_reviewer = MagicMock()
            mock_reviewer.run.return_value = lgtm_result
            mock_fixer = MagicMock()
            mock_make.side_effect = [mock_reviewer, mock_fixer]

            review_prd_json_loop(prd_file, prd_json, tmp_path, config)

            assert mock_reviewer.run.call_count == 1
            mock_fixer.run.assert_not_called()

    def test_issues_trigger_fixer_then_re_review(self, tmp_path):
        """Non-LGTM triggers fixer, then re-reviews."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        issue_result = ToolResult(
            output="1. severity: major\n   problem: criterion infeasible",
            exit_code=0,
            success=True,
        )
        lgtm_result = ToolResult(output="LGTM", exit_code=0, success=True)
        fix_result = ToolResult(output="Fixed", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_reviewer = MagicMock()
            mock_reviewer.run.side_effect = [issue_result, lgtm_result]
            mock_fixer = MagicMock()
            mock_fixer.run.return_value = fix_result
            mock_make.side_effect = [mock_reviewer, mock_fixer]

            review_prd_json_loop(prd_file, prd_json, tmp_path, config)

            assert mock_reviewer.run.call_count == 2
            assert mock_fixer.run.call_count == 1

    def test_max_cycles_exhaustion_prompts_user(self, tmp_path):
        """Max cycles reached without LGTM prompts user (quit/retry/continue)."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        issue_result = ToolResult(
            output="1. severity: major\n   problem: still broken",
            exit_code=0,
            success=True,
        )
        fix_result = ToolResult(output="Attempted fix", exit_code=0, success=True)

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch("ralph_pp.steps.prd.prompt_max_cycles", return_value="continue"),
        ):
            mock_reviewer = MagicMock()
            mock_reviewer.run.return_value = issue_result
            mock_fixer = MagicMock()
            mock_fixer.run.return_value = fix_result
            mock_make.side_effect = [mock_reviewer, mock_fixer]

            # Should not raise — user chose "continue"
            review_prd_json_loop(prd_file, prd_json, tmp_path, config)

            # Default max_cycles is 2
            assert mock_reviewer.run.call_count == 2
            assert mock_fixer.run.call_count == 2

    def test_disabled_skips_review(self, tmp_path):
        """Disabled config skips the review entirely."""
        from ralph_pp.config import PrdJsonReviewConfig

        config = _make_config()
        config.prd_json_review = PrdJsonReviewConfig(enabled=False)

        prd_file = tmp_path / "tasks" / "prd.md"
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            review_prd_json_loop(prd_file, prd_json, tmp_path, config)
            mock_make.assert_not_called()

    def test_reviewer_prompt_includes_repo_path(self, tmp_path):
        """Reviewer prompt should include the codebase path."""
        config = _make_config()
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")
        prd_json = tmp_path / "scripts" / "ralph" / "prd.json"
        prd_json.parent.mkdir(parents=True)
        prd_json.write_text('{"userStories": []}')

        lgtm_result = ToolResult(output="LGTM", exit_code=0, success=True)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_reviewer = MagicMock()
            mock_reviewer.run.return_value = lgtm_result
            mock_fixer = MagicMock()
            mock_make.side_effect = [mock_reviewer, mock_fixer]

            review_prd_json_loop(prd_file, prd_json, tmp_path, config)

            call_kwargs = mock_reviewer.run.call_args[1]
            assert str(tmp_path) in call_kwargs["prompt"]


# ── Issue #117: codebase context appended to PRD generation prompt ─────


class TestCodebaseContextInPrdGeneration:
    def test_repo_path_appended_to_prompt(self, tmp_path):
        from ralph_pp.steps.prd import generate_prd as gen

        config = _make_config()
        captured = {}

        def fake_run(prompt, cwd):
            captured["prompt"] = prompt
            (tmp_path / "tasks").mkdir(exist_ok=True)
            (tmp_path / "tasks" / "prd-test-feat.md").write_text("# PRD")
            return ToolResult(success=True, output="ok", exit_code=0)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.side_effect = fake_run
            mock_make.return_value = mock_tool

            gen(
                "test feat",
                tmp_path,
                config,
                prd_prompt="some description",
                repo_path=tmp_path / "fake-repo",
            )

        prompt = captured["prompt"]
        assert "Read the existing codebase first" in prompt
        assert str(tmp_path / "fake-repo") in prompt

    def test_manual_mode_does_not_append_codebase_block(self, tmp_path):
        from ralph_pp.steps.prd import generate_prd as gen

        config = _make_config()
        captured = {}

        def fake_run(prompt, cwd):
            captured["prompt"] = prompt
            (tmp_path / "tasks").mkdir(exist_ok=True)
            (tmp_path / "tasks" / "prd-test-feat.md").write_text("# PRD")
            return ToolResult(success=True, output="ok", exit_code=0)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.side_effect = fake_run
            mock_make.return_value = mock_tool

            gen("test feat", tmp_path, config, manual=True, repo_path=tmp_path / "fake-repo")

        # Manual mode uses a minimal prompt that does not include the
        # codebase context block.
        assert "Read the existing codebase first" not in captured["prompt"]


# ── Issue #121: design-stance constraints injected into prompt ─────────


class TestDesignStanceInPrdGeneration:
    def test_design_stance_constraints_appended(self, tmp_path):
        from ralph_pp.config import DesignStanceConfig
        from ralph_pp.steps.prd import generate_prd as gen

        config = _make_config()
        config.design_stance = DesignStanceConfig(
            implementation_scope="single_pass",
            backward_compatibility="required",
            existing_tests="must_pass",
            api_stability="extend_only",
            notes="prefer pure-python solutions",
        )
        captured = {}

        def fake_run(prompt, cwd):
            captured["prompt"] = prompt
            (tmp_path / "tasks").mkdir(exist_ok=True)
            (tmp_path / "tasks" / "prd-test-feat.md").write_text("# PRD")
            return ToolResult(success=True, output="ok", exit_code=0)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.side_effect = fake_run
            mock_make.return_value = mock_tool

            gen("test feat", tmp_path, config, prd_prompt="desc", repo_path=tmp_path)

        prompt = captured["prompt"]
        assert "Design constraints" in prompt
        assert "SINGLE implementation pass" in prompt
        assert "Backward compatibility is REQUIRED" in prompt
        assert "ALL existing tests must continue to pass" in prompt
        assert "may only EXTEND with optional parameters" in prompt
        assert "prefer pure-python solutions" in prompt

    def test_design_stance_unspecified_omits_block(self, tmp_path):
        from ralph_pp.steps.prd import generate_prd as gen

        config = _make_config()  # Default DesignStanceConfig: all unspecified
        captured = {}

        def fake_run(prompt, cwd):
            captured["prompt"] = prompt
            (tmp_path / "tasks").mkdir(exist_ok=True)
            (tmp_path / "tasks" / "prd-test-feat.md").write_text("# PRD")
            return ToolResult(success=True, output="ok", exit_code=0)

        with patch("ralph_pp.steps.prd.make_tool") as mock_make:
            mock_tool = MagicMock()
            mock_tool.run.side_effect = fake_run
            mock_make.return_value = mock_tool

            gen("test feat", tmp_path, config, prd_prompt="desc", repo_path=tmp_path)

        assert "Design constraints" not in captured["prompt"]

    def test_build_design_stance_block_partial(self):
        from ralph_pp.config import DesignStanceConfig
        from ralph_pp.steps.prd import _build_design_stance_block

        # Only one field set
        stance = DesignStanceConfig(implementation_scope="incremental")
        block = _build_design_stance_block(stance)
        assert "incrementally" in block
        assert "Backward compatibility" not in block

    def test_build_design_stance_block_empty(self):
        from ralph_pp.config import DesignStanceConfig
        from ralph_pp.steps.prd import _build_design_stance_block

        assert _build_design_stance_block(None) == ""
        assert _build_design_stance_block(DesignStanceConfig()) == ""


# ── Issue #118: PRD review diminishing-returns detection ───────────────


class TestPrdReviewConvergence:
    def test_findings_jaccard_basic(self):
        from ralph_pp.steps.prd import findings_jaccard

        assert findings_jaccard("alpha beta gamma", "alpha beta gamma") == 1.0
        assert findings_jaccard("alpha beta", "delta epsilon") == 0.0
        assert findings_jaccard("", "anything") == 0.0
        # Partial overlap
        result = findings_jaccard("alpha beta gamma", "alpha beta delta")
        assert 0 < result < 1

    def test_review_loop_accepts_on_convergence(self, tmp_path):
        """Two consecutive cycles producing similar findings should auto-accept."""
        from ralph_pp.config import PrdReviewConfig
        from ralph_pp.steps.prd import review_prd_loop

        config = _make_config()
        config.prd_review = PrdReviewConfig(
            reviewer="codex",
            fixer="claude",
            max_cycles=4,
        )
        prd_file = tmp_path / "tasks" / "prd.md"
        prd_file.parent.mkdir(parents=True)
        prd_file.write_text("# PRD")

        # Same findings text on cycles 1 and 2 → 100% Jaccard → accept
        same_findings = "1. severity: major\nproblem: ambiguous validation requirement at boundary"

        with (
            patch("ralph_pp.steps.prd.make_tool") as mock_make,
            patch("ralph_pp.steps.prd.get_head_sha", return_value="abc1234"),
            patch("ralph_pp.steps.prd.get_diff", return_value="(no diff)"),
        ):
            reviewer_mock = MagicMock()
            reviewer_mock.run.return_value = ToolResult(
                success=True, output=same_findings, exit_code=0
            )
            fixer_mock = MagicMock()
            fixer_mock.run.return_value = ToolResult(success=True, output="fixed", exit_code=0)
            mock_make.side_effect = lambda name, cfg: (
                reviewer_mock if name == "codex" else fixer_mock
            )

            review_prd_loop(prd_file, tmp_path, config)

        # Should converge after cycle 2 — only 2 review calls
        assert reviewer_mock.run.call_count == 2
