"""Phase 4 tests for unattended mode (#163): signal handling.

Covers FR-6 from docs/unattended-mode-mvp.md:

- SIGTERM and SIGINT in unattended mode trigger a graceful shutdown.
- A registered child subprocess receives ``terminate()`` so blocking reads
  unblock promptly.
- The orchestrator's ``finally`` writes a ``status: "interrupted"`` result.
- The CLI's classifier maps ``KeyboardInterrupt`` to exit code 30.
- A second signal within the hard-kill window escalates to ``kill()``.

The signal-driven tests are Linux/macOS only; Windows has very different
signal semantics. We skip there.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from ralph_pp import unattended
from ralph_pp.orchestrator import Orchestrator
from ralph_pp.unattended import (
    EXIT_INTERRUPTED,
    ShutdownState,
    get_shutdown_state,
    install_signal_handlers,
    restore_signal_handlers,
)

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX signal semantics; Windows handles SIGTERM/SIGINT differently.",
)


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_shutdown_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets a fresh ``ShutdownState`` and restored handlers."""
    fresh = ShutdownState()
    monkeypatch.setattr(unattended, "_state", fresh)


@pytest.fixture
def restore_handlers():
    """Snapshot SIGTERM/SIGINT before the test and restore on teardown.

    ``signal.getsignal`` may return a handler object that ``signal.signal``
    cannot re-install (notably ``signal.default_int_handler`` for SIGINT in
    some interpreters). Best-effort restoration is fine for tests.
    """
    prev: dict[int, object] = {}
    for s in (signal.SIGTERM, signal.SIGINT):
        prev[s] = signal.getsignal(s)
    yield
    for s in (signal.SIGTERM, signal.SIGINT):
        handler = prev[s]
        if handler is None:
            continue
        try:
            signal.signal(s, handler)  # type: ignore[arg-type]
        except (OSError, ValueError, TypeError):
            signal.signal(s, signal.SIG_DFL)


# ── Unit tests for the handler ───────────────────────────────────────────


class TestShutdownStateLifecycle:
    def test_track_subprocess_sets_and_clears(self) -> None:
        state = ShutdownState()
        proc = subprocess.Popen(["true"])
        try:
            with state.track_subprocess(proc):
                assert state.active_proc is proc
            assert state.active_proc is None
        finally:
            proc.wait()

    def test_track_subprocess_nests(self) -> None:
        state = ShutdownState()
        outer = subprocess.Popen(["true"])
        inner = subprocess.Popen(["true"])
        try:
            with state.track_subprocess(outer):
                assert state.active_proc is outer
                with state.track_subprocess(inner):
                    assert state.active_proc is inner
                assert state.active_proc is outer
            assert state.active_proc is None
        finally:
            outer.wait()
            inner.wait()


class TestInstallAndRestore:
    def test_install_then_restore_returns_handlers(self, restore_handlers) -> None:
        prev = install_signal_handlers()
        assert signal.SIGTERM in prev
        assert signal.SIGINT in prev
        # The currently-installed handler should be ours.
        assert signal.getsignal(signal.SIGTERM) is unattended._shutdown_handler
        restore_signal_handlers(prev)


class TestSignalDeliversToHandler:
    """Send a real signal to the test process and verify the handler runs."""

    def test_sigterm_raises_keyboard_interrupt_and_marks_state(self, restore_handlers) -> None:
        prev = install_signal_handlers()
        try:
            state = get_shutdown_state()
            assert not state.requested
            with pytest.raises(KeyboardInterrupt):
                os.kill(os.getpid(), signal.SIGTERM)
                # Yield to give the signal handler a chance to fire on this
                # interpreter; signal handlers run between bytecodes.
                for _ in range(10):
                    time.sleep(0.01)
            assert state.requested is True
            assert state.signum == signal.SIGTERM
            assert state.received_at is not None
        finally:
            restore_signal_handlers(prev)

    def test_signal_terminates_registered_child(self, restore_handlers) -> None:
        """A long-running ``sleep`` child should be terminated by the handler
        when a signal is received while it is registered."""
        prev = install_signal_handlers()
        proc = subprocess.Popen(["sleep", "30"])
        try:
            state = get_shutdown_state()
            with state.track_subprocess(proc), pytest.raises(KeyboardInterrupt):
                os.kill(os.getpid(), signal.SIGTERM)
                for _ in range(10):
                    time.sleep(0.01)
            # The handler should have called proc.terminate(); wait
            # confirms it actually exits within a short window.
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                proc.wait(timeout=1.0)
                raise AssertionError("registered child was not terminated") from exc
            assert proc.returncode != 0  # killed by signal
        finally:
            with contextlib.suppress(Exception):
                proc.kill()
            restore_signal_handlers(prev)


class TestHardKillEscalation:
    def test_second_signal_within_window_marks_hard_kill(self, restore_handlers) -> None:
        prev = install_signal_handlers()
        try:
            state = get_shutdown_state()
            # First signal — graceful.
            with pytest.raises(KeyboardInterrupt):
                os.kill(os.getpid(), signal.SIGTERM)
                for _ in range(10):
                    time.sleep(0.01)
            assert state.requested is True
            assert state.hard_kill is False

            # Second signal — escalate.
            with pytest.raises(KeyboardInterrupt):
                os.kill(os.getpid(), signal.SIGTERM)
                for _ in range(10):
                    time.sleep(0.01)
            assert state.hard_kill is True
        finally:
            restore_signal_handlers(prev)


# ── End-to-end: signal during Orchestrator.run writes interrupted result ─


def _make_orchestrator(tmp_path: Path, *, result_file: Path) -> Orchestrator:
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
        unattended=True,
        run_id="01HXYZ" + "0" * 20,
        result_file=result_file,
    )


class TestSignalDuringOrchestratorRun:
    @patch.object(Orchestrator, "_step_cleanup")
    @patch.object(Orchestrator, "_step_prd")
    @patch.object(Orchestrator, "_step_worktree")
    @patch("ralph_pp.orchestrator.validate_sandbox_prerequisites")
    def test_sigterm_during_step_writes_interrupted_result(
        self,
        _val: object,
        _wt: object,
        _prd: object,
        _clean: object,
        tmp_path: Path,
        restore_handlers,
    ) -> None:
        """Schedule a SIGTERM mid-run and verify the result file says
        interrupted with exit_code 30, while the run raises
        KeyboardInterrupt for the CLI to catch."""
        result_path = tmp_path / "r.json"
        orch = _make_orchestrator(tmp_path, result_file=result_path)

        def _fake_sandbox(self) -> None:  # noqa: ARG001  (self is required by patch)
            # Raise the signal from this thread while the orchestrator
            # owns the main thread; signal handlers fire between bytecodes
            # so this propagates as KeyboardInterrupt.
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(20):
                time.sleep(0.01)

        with (
            patch.object(Orchestrator, "_step_sandbox", _fake_sandbox),
            pytest.raises(KeyboardInterrupt),
        ):
            orch.run()

        data = json.loads(result_path.read_text())
        assert data["status"] == "interrupted"
        assert data["exit_code"] == EXIT_INTERRUPTED
        assert data["error"]["category"] == "internal"
        assert data["error"]["retriable"] is True
