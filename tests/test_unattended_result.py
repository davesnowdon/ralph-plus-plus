"""Phase 2 tests for unattended mode (#163): the JSON result file.

Covers:
- ``RunResult`` schema shape / serialisation
- atomic write behaviour (temp file + rename, no partial result observable)
- ``resolve_result_path`` precedence (explicit > worktree > cwd fallback)
- ``Orchestrator.run`` writes a result file on success
- ``Orchestrator.run`` writes a result file when a step raises (after re-raise)
- artifacts and commits surfaced in the result come from the worktree
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from ralph_pp.orchestrator import Orchestrator
from ralph_pp.unattended import (
    SCHEMA_VERSION,
    ErrorInfo,
    RunResult,
    collect_artifacts,
    collect_commits,
    resolve_result_path,
    write_atomically,
)

ISO_8601_MS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


# ── RunResult / write_atomically unit tests ──────────────────────────────


def _sample_result(**overrides: object) -> RunResult:
    base: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": "01HXYZ" + "0" * 20,
        "status": "succeeded",
        "exit_code": 0,
        "started_at": "2026-04-26T10:00:00.000Z",
        "finished_at": "2026-04-26T10:00:01.000Z",
        "duration_seconds": 1.0,
        "feature": "test-feat",
        "mode": "delegated",
        "repo": "/repo",
        "branch": None,
        "worktree": None,
        "iterations": 0,
    }
    base.update(overrides)
    return RunResult(**base)  # type: ignore[arg-type]


class TestRunResultSchema:
    def test_minimal_shape(self) -> None:
        result = _sample_result()
        data = result.to_dict()
        for key in (
            "schema_version",
            "run_id",
            "status",
            "exit_code",
            "started_at",
            "finished_at",
            "duration_seconds",
            "feature",
            "mode",
            "repo",
            "branch",
            "worktree",
            "iterations",
            "artifacts",
            "commits",
            "error",
        ):
            assert key in data, f"missing key {key!r}"

    def test_error_serialises_to_dict(self) -> None:
        result = _sample_result(
            status="failed",
            exit_code=1,
            error=ErrorInfo(category="sandbox", message="boom", retriable=True),
        )
        data = result.to_dict()
        assert data["error"] == {
            "category": "sandbox",
            "message": "boom",
            "retriable": True,
        }

    def test_error_null_when_succeeded(self) -> None:
        result = _sample_result()
        assert result.to_dict()["error"] is None


class TestWriteAtomically:
    def test_writes_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "result.json"
        write_atomically(path, _sample_result())
        loaded = json.loads(path.read_text())
        assert loaded["schema_version"] == SCHEMA_VERSION

    def test_temp_file_removed_on_success(self, tmp_path: Path) -> None:
        path = tmp_path / "result.json"
        write_atomically(path, _sample_result())
        # After a successful write, no .tmp companion remains.
        assert not (tmp_path / "result.json.tmp").exists()

    def test_no_partial_file_when_replace_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "result.json"

        def boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("simulated rename failure")

        monkeypatch.setattr("ralph_pp.unattended.os.replace", boom)
        with pytest.raises(OSError, match="simulated rename failure"):
            write_atomically(path, _sample_result())

        # The target was never created — only a .tmp sibling, which the
        # orchestrator cleanup will ignore. The orchestrator must never
        # observe a partial result.json.
        assert not path.exists()

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "result.json"
        write_atomically(path, _sample_result())
        assert path.is_file()


class TestResolveResultPath:
    def test_explicit_wins(self, tmp_path: Path) -> None:
        explicit = tmp_path / "custom.json"
        worktree = tmp_path / "wt"
        cwd = tmp_path / "cwd"
        assert (
            resolve_result_path(explicit=explicit, worktree=worktree, run_id="rid", cwd=cwd)
            == explicit
        )

    def test_worktree_default(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        cwd = tmp_path / "cwd"
        assert (
            resolve_result_path(explicit=None, worktree=worktree, run_id="rid", cwd=cwd)
            == worktree / "scripts" / "ralph" / "result.json"
        )

    def test_cwd_fallback_when_no_worktree(self, tmp_path: Path) -> None:
        cwd = tmp_path / "cwd"
        assert (
            resolve_result_path(explicit=None, worktree=None, run_id="rid-xyz", cwd=cwd)
            == cwd / "ralph-result-rid-xyz.json"
        )


# ── Helpers tested against a real tmp git repo ───────────────────────────


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=path, check=True)


def _head_sha(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


class TestCollectCommits:
    def test_returns_commits_after_base(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _head_sha(repo)
        for i in range(2):
            subprocess.run(
                ["git", "commit", "-q", "--allow-empty", "-m", f"c{i}"],
                cwd=repo,
                check=True,
            )
        commits = collect_commits(repo, base)
        assert len(commits) == 2

    def test_empty_when_no_worktree(self) -> None:
        assert collect_commits(None, "abc") == []

    def test_empty_when_no_base_sha(self, tmp_path: Path) -> None:
        assert collect_commits(tmp_path, None) == []

    def test_empty_when_git_fails(self, tmp_path: Path) -> None:
        # Path is not a git repo; collect_commits must not raise.
        assert collect_commits(tmp_path, "abc123") == []


class TestCollectArtifacts:
    def test_finds_known_artifacts(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        (wt / "tasks").mkdir(parents=True)
        (wt / "tasks" / "prd-test-feat.md").write_text("# PRD")
        (wt / "scripts" / "ralph").mkdir(parents=True)
        (wt / "scripts" / "ralph" / "progress.txt").write_text("step")
        (wt / "scripts" / "ralph" / ".base-sha").write_text("abc123")

        artifacts = collect_artifacts(wt)
        assert artifacts == {
            "prd": "tasks/prd-test-feat.md",
            "progress": "scripts/ralph/progress.txt",
            "base_sha": "scripts/ralph/.base-sha",
        }

    def test_omits_missing_keys(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        (wt / "scripts" / "ralph").mkdir(parents=True)
        (wt / "scripts" / "ralph" / "progress.txt").write_text("p")
        artifacts = collect_artifacts(wt)
        assert artifacts == {"progress": "scripts/ralph/progress.txt"}

    def test_empty_when_no_worktree(self) -> None:
        assert collect_artifacts(None) == {}


# ── Orchestrator integration tests ───────────────────────────────────────


def _make_orchestrator(
    tmp_path: Path,
    *,
    unattended: bool = True,
    result_file: Path | None = None,
) -> Orchestrator:
    from ralph_pp.config import (
        Config,
        OrchestratedConfig,
        PostReviewConfig,
        RalphConfig,
    )

    cfg = Config.__new__(Config)
    cfg.repo_path = tmp_path
    cfg.ralph = RalphConfig(mode="delegated", max_iterations=1, sandbox_tool="claude")
    cfg.post_review = PostReviewConfig(max_cycles=1)
    cfg.hooks = {}
    cfg.orchestrated = OrchestratedConfig()

    return Orchestrator(
        "test-feat",
        cfg,
        unattended=unattended,
        run_id="01HXYZ" + "0" * 20,
        result_file=result_file,
    )


class TestOrchestratorWritesResultFile:
    @patch.object(Orchestrator, "_step_cleanup")
    @patch.object(Orchestrator, "_step_post_review")
    @patch.object(Orchestrator, "_step_sandbox")
    @patch.object(Orchestrator, "_step_prd")
    @patch.object(Orchestrator, "_step_worktree")
    @patch("ralph_pp.orchestrator.validate_sandbox_prerequisites")
    def test_success_writes_result(
        self,
        _val: object,
        _wt: object,
        _prd: object,
        _sandbox: object,
        _post: object,
        _clean: object,
        tmp_path: Path,
    ) -> None:
        result_path = tmp_path / "result.json"
        orch = _make_orchestrator(tmp_path, result_file=result_path)
        orch.run()

        assert result_path.is_file()
        data = json.loads(result_path.read_text())
        assert data["status"] == "succeeded"
        assert data["exit_code"] == 0
        assert data["error"] is None
        assert data["run_id"] == "01HXYZ" + "0" * 20
        assert data["feature"] == "test-feat"
        assert data["schema_version"] == SCHEMA_VERSION
        assert ISO_8601_MS_RE.match(data["started_at"])
        assert ISO_8601_MS_RE.match(data["finished_at"])
        assert isinstance(data["duration_seconds"], int | float)

    @patch.object(Orchestrator, "_step_cleanup")
    @patch.object(Orchestrator, "_step_sandbox", side_effect=RuntimeError("boom"))
    @patch.object(Orchestrator, "_step_prd")
    @patch.object(Orchestrator, "_step_worktree")
    @patch("ralph_pp.orchestrator.validate_sandbox_prerequisites")
    def test_failure_writes_result_and_reraises(
        self,
        _val: object,
        _wt: object,
        _prd: object,
        _sandbox: object,
        _clean: object,
        tmp_path: Path,
    ) -> None:
        result_path = tmp_path / "result.json"
        orch = _make_orchestrator(tmp_path, result_file=result_path)
        with pytest.raises(RuntimeError, match="boom"):
            orch.run()

        assert result_path.is_file()
        data = json.loads(result_path.read_text())
        assert data["status"] == "failed"
        assert data["exit_code"] == 1
        assert data["error"] is not None
        assert data["error"]["message"] == "boom"
        assert data["error"]["category"] == "internal"

    @patch.object(Orchestrator, "_step_cleanup")
    @patch.object(Orchestrator, "_step_post_review")
    @patch.object(Orchestrator, "_step_sandbox")
    @patch.object(Orchestrator, "_step_prd")
    @patch.object(Orchestrator, "_step_worktree")
    @patch("ralph_pp.orchestrator.validate_sandbox_prerequisites")
    def test_no_result_file_when_not_unattended(
        self,
        _val: object,
        _wt: object,
        _prd: object,
        _sandbox: object,
        _post: object,
        _clean: object,
        tmp_path: Path,
    ) -> None:
        result_path = tmp_path / "should-not-exist.json"
        orch = _make_orchestrator(tmp_path, unattended=False, result_file=result_path)
        orch.run()
        assert not result_path.exists()

    @patch.object(Orchestrator, "_step_cleanup")
    @patch.object(Orchestrator, "_step_worktree", side_effect=ValueError("config bad"))
    @patch("ralph_pp.orchestrator.validate_sandbox_prerequisites")
    def test_cwd_fallback_when_no_worktree_created(
        self,
        _val: object,
        _wt: object,
        _clean: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Failure before worktree creation must still produce a result file
        in the cwd-based fallback location."""
        monkeypatch.chdir(tmp_path)
        orch = _make_orchestrator(tmp_path)
        with pytest.raises(ValueError, match="config bad"):
            orch.run()

        run_id = orch.run_id
        assert run_id is not None
        fallback = tmp_path / f"ralph-result-{run_id}.json"
        assert fallback.is_file()
        data = json.loads(fallback.read_text())
        assert data["status"] == "failed"
        assert data["worktree"] is None
