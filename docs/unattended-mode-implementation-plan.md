# Unattended Mode — Implementation Plan

## References

- Spec: [unattended-mode-mvp.md](unattended-mode-mvp.md)
- GitHub issue: [#163](https://github.com/davesnowdon/ralph-plus-plus/issues/163)
- Repo conventions: [AGENTS.md](../AGENTS.md)

## Resolved Decisions

The four open questions in the spec are resolved as follows; this plan
assumes them throughout.

| Question | Decision |
|----------|----------|
| Run id format | **ULID**, via `python-ulid>=2.0`. |
| Result file fallback when no worktree exists | `./ralph-result-{run_id}.json` in the process cwd. |
| Conflict policy with interactive-only flags | **Reject at parse time** with exit 20. |
| `--unattended` on subcommands other than `run` | Out of scope for MVP; only `run` accepts the flag. `RALPH_UNATTENDED=1` in the env still triggers plain-output behaviour everywhere. |

## Phasing Strategy

Five phases, each independently mergeable and testable. Earlier phases land
the safest, smallest changes; later phases handle the trickier signal/exit
work. The repo's `make check` (`lint`, `typecheck`, `test`) must pass at the
end of every phase.

```
Phase 1  Foundation         flag, env, run_id, plain output, conflict rejection
Phase 2  Result scaffolding  RunResult dataclass, atomic writer, success path
Phase 3  Exit code taxonomy  exception → (status, exit_code, category) mapping
Phase 4  Signal handling    SIGTERM/SIGINT graceful shutdown
Phase 5  Logging + docs     startup/shutdown lines, README, acceptance pass
```

Each phase ships with the tests for that phase. No phase ships without
tests.

---

## Phase 1 — Foundation

Smallest possible activation surface. After this phase, `--unattended` is
recognised, run ids round-trip, output is plain, and conflicting flags are
rejected. No result file is written yet.

### Changes

- **`pyproject.toml`** — add runtime dependency `python-ulid>=2.0`.
- **`ralph_pp/cli.py`** (`run` command, lines [118–251](../ralph_pp/cli.py#L118-L251)):
  - Add three options:
    - `--unattended / --no-unattended` (bool, default false).
    - `--run-id <str>` (optional; generated if absent).
    - `--result-file <path>` (optional; resolved later in Phase 2).
  - Read `RALPH_UNATTENDED` (truthy values: `1`, `true`, `yes`,
    case-insensitive) before the Console is touched. CLI flag wins on
    conflict with env.
  - When unattended is active:
    - Force `--non-interactive` on (set `cfg.non_interactive.enabled = True`
      to keep parity with [cli.py:320–321](../ralph_pp/cli.py#L320-L321)).
    - Set `NO_COLOR=1` in `os.environ` and pass `force_terminal=False`,
      `no_color=True` when constructing the module-level Console at
      [cli.py:25](../ralph_pp/cli.py#L25). Cleanest is to lazy-init the
      Console via a `get_console()` helper so the same flags can propagate to
      the existing imports in `orchestrator.py:35` and the steps modules.
  - Conflict rejection (parse time, exit 20):
    - `--unattended` + `--manual-prd` ([cli.py:193](../ralph_pp/cli.py#L193))
      → `click.UsageError`.
    - Any future interactive-only flag added to this list as it appears.
  - Generate a ULID for `run_id` if not supplied, log it to stderr as part
    of the startup line (line itself lands in Phase 5; the value is captured
    here).
- **`ralph_pp/orchestrator.py`** ([Orchestrator.__init__, lines 39–54](../ralph_pp/orchestrator.py#L39-L54)):
  - Extend constructor to accept `unattended: bool = False`,
    `run_id: str | None = None`, `result_file: Path | None = None`. Store on
    `self`. Don't act on them yet beyond storage.

### Tests

New file `tests/test_unattended_cli.py`:

- `--unattended` is recognised and propagates to a `Orchestrator` instance
  (patch `Orchestrator` constructor, assert kwargs).
- `RALPH_UNATTENDED=1` env activates the mode equivalently (use
  `CliRunner(env={...})`).
- `--unattended` + `--manual-prd` exits with non-zero (Click usage error,
  which is exit 2; we'll narrow to 20 in Phase 3).
- With `--unattended`, captured stderr/stdout contain no ANSI escape codes
  (`\x1b\[`).
- `--run-id custom-123` round-trips into the constructor.

### Done criteria for Phase 1

```bash
cd ralph-plus-plus
uv sync --group dev
make check
```

Plus the new unattended CLI tests must pass.

---

## Phase 2 — Result file scaffolding

This phase implements the always-written result file on the success path
and on raised exceptions. Signal handling is **not** in scope here; that's
Phase 4.

### Changes

- **New module `ralph_pp/unattended.py`** (keep the surface tight and
  dependency-light):
  - `@dataclass RunResult` matching the FR-3 schema. All fields typed.
    Provides `to_dict() -> dict` that produces the JSON shape exactly.
  - `class ErrorInfo` (or a nested dataclass): `category`, `message`,
    `retriable: bool`.
  - Constants: `SCHEMA_VERSION = 1`, exit-code constants.
  - `def write_atomically(path: Path, result: RunResult) -> None`:
    write JSON to `path.with_suffix(path.suffix + ".tmp")`, `fsync`, then
    `os.replace` to `path`. Ensure parent directory exists.
  - `def resolve_result_path(*, explicit, worktree, run_id, cwd) -> Path`:
    implements FR-2's resolution order — explicit > `{worktree}/scripts/ralph/result.json`
    > `{cwd}/ralph-result-{run_id}.json`.
  - `def now_iso8601_ms() -> str`: ISO-8601 UTC with millisecond precision.
- **`ralph_pp/orchestrator.py`** (`Orchestrator.run`, lines [56–117](../ralph_pp/orchestrator.py#L56-L117)):
  - Capture `started_at = now_iso8601_ms()` and `start_monotonic = time.monotonic()`
    near [line 74](../ralph_pp/orchestrator.py#L74).
  - Wrap the body in `try / except / finally` such that:
    - On success: build `RunResult(status="succeeded", exit_code=0, ...)`.
    - On exception: build `RunResult(status="failed", exit_code=1, error=ErrorInfo(...))`.
      Re-raise after the result is constructed; the actual write happens in
      `finally`.
    - In `finally`: only when `self.unattended` is `True`, resolve the
      result path and call `write_atomically`. Never raise from the finally
      block; on a write failure, log to stderr and continue exiting.
  - Helper `_build_result(self, *, status, exit_code, error)` consolidates
    field population:
    - `branch`, `worktree`: from existing instance attributes.
    - `mode`: from `self.config.mode` (or wherever it lives today).
    - `iterations`: from `self._run_summary.iterations` if set, else `0`.
    - `commits`: compute via `git log {base_sha}..HEAD --format=%H` against
      `self.worktree_path`. Empty list if base sha or worktree absent.
    - `artifacts`: dict with `prd`, `progress`, `base_sha` keys, omitted if
      the corresponding file does not exist. Paths relative to `worktree`.

### Cross-cutting

- For exit-code semantics, Phase 2 uses exit 0 / 1 only. The richer
  taxonomy (20, 30) lands in Phase 3.
- The result file is written even when `worktree_path is None` (use the
  cwd fallback).

### Tests

Extend `tests/test_unattended_cli.py` and add `tests/test_unattended_result.py`:

- Successful run produces a result file at the default location, schema
  matches: required keys present, types correct, ISO-8601 timestamps parse,
  paths in `artifacts` are relative to `worktree`.
- Failure inside a step produces a result file with `status: "failed"`,
  populated `error.message`, and the run exits non-zero.
- Atomic write: monkeypatch `os.replace` to raise after the temp file is
  written; assert no `result.json` is observable, only the `.tmp` file.
- Custom `--result-file` is honoured.
- Cwd fallback path is used when no worktree was created (simulate by
  forcing a config error before worktree setup).
- Commits list matches the actual `git log base..HEAD` output (use a
  `tmp_path`-based fake repo, mirror existing test patterns from
  [test_cli.py:34–54](../tests/test_cli.py)).

### Done criteria for Phase 2

`make check` plus all unattended tests pass. Spec acceptance criteria met:
*"succeeds end-to-end and writes a schema-conforming result file"* and
*"--run-id round-trips"*.

---

## Phase 3 — Exit code taxonomy

Map raised exceptions to the four-code table in FR-4 and to the right
`error.category`. Untangles success/generic-failure (Phase 2) into the full
table.

### Changes

- **`ralph_pp/unattended.py`**:
  - Add a small typed exception hierarchy or a classification function.
    Recommended: classification function over a hierarchy, since the
    existing exceptions are not ours to subclass cleanly.
    ```python
    def classify_error(exc: BaseException) -> tuple[str, int, ErrorInfo]:
        """Return (status, exit_code, error_info) for an exception."""
    ```
    Categories per FR-3: `config`, `environment`, `prd`, `sandbox`,
    `review`, `internal`.
- **Inventory pass on existing exceptions** (one-time research, captured
  here so the implementer doesn't re-discover):
  - `click.UsageError` / `click.BadParameter` → category `config`, exit 20.
    Caught at the top of `cli.py` `main` callback (or via a `Group` wrapper)
    so we can write a result file before exiting.
  - Anything raised from `ralph_pp/config.py` validation → `config`, exit
    20.
  - Docker / git CalledProcessError where the *tool* failed (not the work)
    → `environment`, exit 1, retriable=true.
  - Failures in `_step_prd*` → `prd`, exit 1.
  - Failures in `_step_sandbox*` → `sandbox`, exit 1, retriable=true.
  - Failures in post-review steps → `review`, exit 1.
  - Anything else → `internal`, exit 1, retriable=false.
- **`ralph_pp/cli.py`**:
  - Wrap the `Orchestrator(...)` construction + `.run()` call in a top-level
    handler that:
    - Catches `click.UsageError` / `click.BadParameter` early enough to
      still write a result file (when `--unattended` was parsed; if parse
      failed before unattended was seen, no result file is written and the
      normal Click flow takes over).
    - Catches all other exceptions, calls `classify_error`, ensures the
      orchestrator's result-file write happened, and `sys.exit(exit_code)`.
  - The conflict rejections from Phase 1 should now exit with **20**, not
    Click's default 2. Implement by using `click.UsageError` plus a wrapper
    that translates to 20 when unattended is active.

### Tests

New file `tests/test_unattended_exit_codes.py`:

- Parametrized: each `(failure_injection, expected_exit, expected_category)`
  pair exits with the right code and the result file's `error.category`
  matches.
- `--unattended --manual-prd` exits **20** (not 2).
- A successful run exits **0**.
- A `KeyboardInterrupt` raised from inside a step (simulating `SIGINT`)
  results in `exit_code: 30, status: "interrupted"`. (The full signal-driven
  path lands in Phase 4; here we verify the classification.)
- An unknown exception (e.g. `RuntimeError("boom")`) → exit 1, category
  `internal`, retriable false.

### Done criteria for Phase 3

`make check` passes. Spec acceptance criteria *"PRD failure writes failed
result"* and *"bad config / missing input exits 20"* are satisfied.

---

## Phase 4 — Signal handling

The trickiest phase. Touches subprocess management around docker and the
sandbox runner.

### Changes

- **`ralph_pp/unattended.py`**:
  - `class ShutdownState`: thread-safe flag with `requested: bool`,
    `hard_kill: bool`, `signal_received_at: float | None`. Singleton or
    passed explicitly.
  - `def install_signal_handlers(state: ShutdownState) -> None`:
    installs handlers for `SIGTERM` and (when unattended) `SIGINT` that:
    - First call: set `state.requested = True`, capture timestamp.
    - Second call within 5 seconds: set `state.hard_kill = True` and
      forward `SIGKILL` to the tracked subprocess.
- **`ralph_pp/orchestrator.py`**:
  - At the top of `run()`, when `self.unattended` is true, install signal
    handlers and remember the previous handlers so we can restore on exit.
  - Between sandbox iterations, check `state.requested` and break the
    loop. Exact location depends on the sandbox iteration loop; see
    [steps/sandbox.py:_run_orchestrated](../ralph_pp/steps/sandbox.py).
  - On loop break due to shutdown, raise a
    `_ShutdownRequested` exception so the existing try/except chain handles
    cleanup uniformly. Map it in `classify_error` to category=internal but
    `status: "interrupted"`, `exit_code: 30`. (Special-case in
    `_build_result` — `status` is "interrupted", not "failed".)
- **`ralph_pp/steps/sandbox.py`** (subprocess spawn, around [line 536+](../ralph_pp/steps/sandbox.py#L536)):
  - Track the active `Popen` in `ShutdownState.active_proc`.
  - On shutdown request: send `SIGTERM` to the subprocess, wait up to N
    seconds, then `SIGKILL` if `hard_kill` is set.
  - Use `Popen(..., start_new_session=True)` so we can signal the whole
    sandbox process group, not just the wrapper.

### Tests

New file `tests/test_unattended_signals.py`:

- Spawn `ralph++` as a subprocess via `subprocess.Popen` against a
  fixture repo and a mocked sandbox runner that sleeps for several
  seconds. After the process is observed to start, send `SIGTERM`. Assert:
  - Process exits with code 30 within a bounded grace period.
  - The result file exists, is schema-conforming, has `status: "interrupted"`.
  - No orphan child processes remain (best-effort: check via `psutil` if
    available, otherwise just check the parent's children list).
- Two `SIGTERM`s within 5s cause faster exit (still 30).
- A run that finishes normally before any signal is unaffected.

These tests are integration-flavoured. They go in a new
`tests/test_unattended_signals.py` and may need a `pytestmark` skip on
Windows (signal semantics differ). Reuse the temp-git-repo helper from
[test_cli.py](../tests/test_cli.py).

### Done criteria for Phase 4

`make check` passes. Spec acceptance criterion *"SIGTERM mid-iteration writes
schema-conforming result with status interrupted, exits 30 within bounded
time"* is met. The full integration test for signal handling runs in CI on
Linux.

---

## Phase 5 — Logging, docs, polish

The remaining acceptance criteria: explicit startup/shutdown log lines,
README mention, final-pass smoke.

### Changes

- **`ralph_pp/orchestrator.py`** — emit two stderr log lines:
  - Startup, right after timestamps are captured:
    ```
    {iso_ts} ralph++ run started run_id={run_id} feature={feature}
      mode={mode} worktree={worktree_or_dash} result_file={result_file}
    ```
    (Single line; broken here for readability.)
  - Shutdown, in `finally`, after the result file is written:
    ```
    {iso_ts} ralph++ run finished status={status} exit_code={code}
      duration_seconds={dur}
    ```
  - Use Python `logging` with a plain formatter when unattended; bypass
    `rich.Console` so escape codes never sneak in.
- **`README.md`** — short "Unattended mode" subsection linking to the spec
  and showing one example invocation:
  ```bash
  RALPH_UNATTENDED=1 ralph++ run \
    --feature add-rate-limiting \
    --prd-file /tmp/prds/rate-limiting.md \
    --result-file /tmp/results/rate-limiting.json \
    --run-id 01HXYZ...
  ```
- **`docs/unattended-mode-mvp.md`** — flip Open Question 1 to "Resolved:
  cwd fallback" with a link back to this plan, and Open Question 2 to
  "Resolved: ULID via python-ulid". Open Questions 3 and 4 stay open.
- **Acceptance pass** — work the FR-checklist top to bottom against the
  built artifact. Any miss becomes a follow-up commit in Phase 5.

### Tests

- Assertion in existing tests: stderr contains the startup and shutdown
  log lines with the expected fields.
- Snapshot a single representative `--unattended --dry-run` invocation,
  parse stderr, validate format.

### Done criteria for Phase 5

All acceptance criteria from [the spec](unattended-mode-mvp.md#acceptance-criteria)
check off. `make check` passes. Manual smoke per
[AGENTS.md](../AGENTS.md#change-rules-for-agents):

- delegated mode with `claude`
- delegated mode with `codex`
- orchestrated mode with custom `SESSION_RUNNER`
- linked git worktree execution
- failure handling for reviewer/fixer/tooling errors

For unattended mode specifically, also smoke:

- Successful run end-to-end with `--unattended` against a real sandbox
  checkout; inspect the result JSON.
- Manual `kill -TERM <pid>` mid-run; verify result file says `interrupted`
  and exit code is 30.

---

## Files Touched (Summary)

| File | Phases | Notes |
|------|--------|-------|
| [pyproject.toml](../pyproject.toml) | 1 | Add `python-ulid>=2.0`. |
| [ralph_pp/cli.py](../ralph_pp/cli.py) | 1, 3, 5 | New flags, env detection, exit-code wrapper, conflict rejection. |
| [ralph_pp/orchestrator.py](../ralph_pp/orchestrator.py) | 1, 2, 4, 5 | Constructor extension, try/finally, result building, signal hooks, log lines. |
| [ralph_pp/unattended.py](../ralph_pp/) | 2, 3, 4 | New module: `RunResult`, `ErrorInfo`, atomic writer, classifier, `ShutdownState`. |
| [ralph_pp/steps/sandbox.py](../ralph_pp/steps/sandbox.py) | 4 | `Popen` tracked in `ShutdownState`; `start_new_session=True`; signal forwarding. |
| [ralph_pp/config.py](../ralph_pp/config.py) | — | No changes; unattended is CLI/env-only per spec. |
| [README.md](../README.md) | 5 | Unattended mode section. |
| [docs/unattended-mode-mvp.md](unattended-mode-mvp.md) | 5 | Resolve open questions 1–2. |
| `tests/test_unattended_cli.py` | 1 | New. |
| `tests/test_unattended_result.py` | 2 | New. |
| `tests/test_unattended_exit_codes.py` | 3 | New. |
| `tests/test_unattended_signals.py` | 4 | New, Linux-only via `pytestmark`. |

## Risks and Watch Items

1. **Console singleton.** The repo creates one `Console()` at module load
   in [cli.py:25](../ralph_pp/cli.py#L25) and re-imports it elsewhere. If we
   only set `NO_COLOR=1` after that import, ANSI may still appear. The
   simplest fix is to do env-var setup at the very top of `main()`, before
   any subcommand callback runs and before any other module is touched
   that imports the Console. Verify in a Phase 1 test that asserts on
   stderr emptiness of escape codes.
2. **Subprocess process groups.** Forwarding signals to the sandbox
   container reliably requires `start_new_session=True` plus careful
   `os.killpg` semantics. Worth a focused test on Phase 4 day one before
   building the integration test.
3. **Atomic writes on Windows.** `os.replace` is atomic on POSIX and
   Windows for same-filesystem renames. Don't write the temp file under
   `/tmp` if the result file lives elsewhere — write next to it (the spec
   uses "sibling temp path" and that's what the helper should do).
4. **Existing exit-code 1 behaviour.** Tests that already assert
   non-zero exits without checking the value remain green; tests that
   assert specific exit codes need updating only inside the new unattended
   tests. Audit before merging Phase 3.
5. **ULID dependency footprint.** `python-ulid` is small and pure-Python.
   If the project later wants to drop the dep, we can replace it with a
   ~20-line implementation. The choice in this plan is the convenience
   one.

## Verification

Per phase, the same pattern:

```bash
cd /workspace/ralph-plus-plus
uv sync --group dev
make check
```

End-to-end smoke after Phase 5:

```bash
RALPH_UNATTENDED=1 ralph++ run \
  --feature smoketest \
  --prd-prompt "add a hello world function" \
  --dry-run \
  --result-file /tmp/ralph-smoke.json

jq . /tmp/ralph-smoke.json
echo "exit code: $?"
```

Expected: exit 0, JSON with `status: "succeeded"` (or the appropriate
dry-run equivalent), and no ANSI escape codes in stderr.
