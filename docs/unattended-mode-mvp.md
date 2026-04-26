# Unattended Mode — MVP Requirements

## Status

Draft. No code changes yet. This document defines the minimum viable surface
for running `ralph++` as a child process of an external orchestrator (a trigger
service, a queue consumer, a scheduler, a CI job).

## Motivation

Today `ralph++` is designed for a human at a terminal. Its output is `rich`
formatted, its progress lives in human-readable logs, and its success/failure
signal is implicit in the log stream. An automation layer that wants to drive
`ralph++` per external trigger has to scrape logs, guess at completion, and
manage the worktree out-of-band.

Unattended mode is a small, additive contract that makes `ralph++` a
well-behaved subprocess: predictable output, a single machine-readable result,
documented exit codes, and a clean response to termination signals.

It is **not** a daemon, server, or long-lived agent. `ralph++` remains
one-shot. The orchestrator owns scheduling, retries, queueing, and notification.

## Scope

### In scope (MVP)

- A single activation flag and matching environment variable.
- An always-written, atomically-produced JSON result file.
- A small, stable set of exit codes.
- A caller-supplied run identifier echoed back in the result.
- Clean termination on `SIGTERM` with a well-formed result file.
- Plain output (no colour, no spinners, no TTY-only formatting).

### Out of scope (deferred)

- JSONL event stream / structured progress events.
- `status` subcommand and `resume` subcommand.
- Idempotency keys.
- Metrics file.
- Stdin / file-descriptor based input.
- HTTP, daemon, or webhook surfaces.

These are useful additions that can land later without breaking the MVP
contract.

## Terminology

- **Orchestrator** — the external process that spawns `ralph++` (a trigger
  service, queue consumer, CI job, etc.).
- **Run** — one invocation of `ralph++ run` from start to exit.
- **Result file** — the single JSON document produced by every unattended run.
- **Worktree** — the per-run git worktree `ralph++` already creates today.

## Naming

The activation flag is `--unattended`, with environment variable
`RALPH_UNATTENDED=1`. The name was chosen because it is the established term
in adjacent automation tooling (e.g. Debian's unattended-upgrades,
`DEBIAN_FRONTEND=noninteractive`) for "no human present, an automation layer
is driving this". It is more specific than `--agent` and avoids conflicting
with the existing `delegated`/`orchestrated` execution modes.

## Functional Requirements

### FR-1 — Activation

- A new flag `--unattended` on the `run` command activates unattended mode.
- The environment variable `RALPH_UNATTENDED=1` is equivalent to passing
  `--unattended`. CLI flag wins on conflict.
- Activating unattended mode implies `--non-interactive` (existing behaviour).
- Activating unattended mode disables all `rich` formatting: no colour, no
  panels, no rules, no spinners, no progress bars. Logs become plain text on
  stderr.
- The mode is opt-in. With neither the flag nor the env var, behaviour is
  unchanged for human users.

### FR-2 — Result file

- A new flag `--result-file PATH` specifies where the result JSON is written.
- When `--unattended` is active and `--result-file` is omitted, the result
  defaults to `{worktree}/scripts/ralph/result.json`.
- The result file **must** be written on every exit path: success, failure,
  caught exception, and signal-driven shutdown.
- The result file **must** be written atomically (write to a sibling temp
  path, `fsync`, `rename`). An orchestrator must never observe a partial or
  truncated result file.
- If the worktree was never created (e.g. config error before worktree
  setup), the result file is still written if `--result-file` was supplied
  explicitly. If the default location was selected and no worktree exists,
  the result is written to the working directory as
  `ralph-result-{run_id}.json` so the orchestrator can still read it.

### FR-3 — Result schema

The result file is a single JSON object with the following shape. Field
ordering is not significant; all listed fields are required unless marked
optional.

```json
{
  "schema_version": 1,
  "run_id": "01HXXXXXXXXXXXXXXXXXXXXXXX",
  "status": "succeeded",
  "exit_code": 0,
  "started_at": "2026-04-25T10:31:04.812Z",
  "finished_at": "2026-04-25T10:58:19.114Z",
  "duration_seconds": 1634,
  "feature": "add-rate-limiting",
  "mode": "delegated",
  "repo": "/abs/path/to/target/repo",
  "branch": "ralph/add-rate-limiting-a3f9c2",
  "worktree": "/abs/path/to/.git/worktrees/ralph/add-rate-limiting-a3f9c2",
  "iterations": 4,
  "artifacts": {
    "prd": "tasks/prd-add-rate-limiting.md",
    "progress": "scripts/ralph/progress.txt",
    "base_sha": "abc123def456..."
  },
  "commits": ["def456...", "789abc..."],
  "error": null
}
```

Field rules:

- `schema_version`: integer. Bumped on incompatible changes.
- `run_id`: string. Caller-supplied via `--run-id` if present, otherwise
  generated.
- `status`: one of `"succeeded"`, `"failed"`, `"interrupted"`.
- `exit_code`: integer matching the process exit code (see FR-4).
- `started_at`, `finished_at`: ISO-8601 UTC timestamps with millisecond
  precision.
- `duration_seconds`: number. `finished_at - started_at`.
- `feature`: string. The resolved feature name.
- `mode`: one of `"delegated"`, `"orchestrated"`. The execution mode used.
- `repo`: absolute path. The target repository the run operated against.
- `branch`: string or `null` if no branch was created.
- `worktree`: absolute path or `null` if no worktree was created.
- `iterations`: integer. Sandbox iteration count actually executed. Zero if
  the run failed before sandbox start.
- `artifacts`: object of string keys to paths **relative to `worktree`**.
  Required keys when applicable: `prd`, `progress`, `base_sha`. Keys are
  omitted when the corresponding artifact does not exist.
- `commits`: array of full commit SHAs created by this run, in order. Empty
  array if none.
- `error`: `null` on success, otherwise an object:
  ```json
  {
    "category": "config" | "environment" | "prd" | "sandbox" | "review" | "internal",
    "message": "human-readable single-line message",
    "retriable": true | false
  }
  ```
  `category` describes which phase failed. `retriable` is the orchestrator's
  hint: `true` for transient failures (sandbox crash, environment hiccup),
  `false` for input/config errors.

### FR-4 — Exit codes

A small, stable mapping. The CLI must use only these values when
`--unattended` is active:

| Code | Meaning                                   | `status`        |
|------|-------------------------------------------|-----------------|
| 0    | Run completed successfully                | `succeeded`     |
| 1    | Run failed (PRD, sandbox, or review)      | `failed`        |
| 20   | Bad input or configuration error          | `failed`        |
| 30   | Interrupted by signal                     | `interrupted`   |

Mapping of failure categories to exit codes is implementation-defined within
this table. The contract for the orchestrator is:

- `0` → success.
- `20` → don't retry, fix the inputs.
- `30` → run was interrupted; safe to re-trigger.
- `1` → generic failure; retry policy is up to the orchestrator (consult
  `error.retriable` in the result file for guidance).

### FR-5 — Run identifier

- A new flag `--run-id <string>` on the `run` command.
- If supplied, the value is echoed verbatim in `result.run_id`.
- If omitted, `ralph++` generates a value (ULID or UUIDv7 — implementation
  choice; must be sortable by creation time).
- The generated value is logged to stderr at startup so a human running the
  command can still correlate.

### FR-6 — Termination handling

- On `SIGTERM`, `ralph++` performs a graceful shutdown:
  1. Stop scheduling new sandbox iterations.
  2. Allow the current sandbox iteration (if any) to complete or be
     terminated by its own timeout.
  3. Write the result file with `status: "interrupted"` and `exit_code: 30`.
  4. Exit with code 30.
- A second `SIGTERM` or `SIGINT` within ~5 seconds of the first causes
  immediate child-process termination and a best-effort result file write.
  Exit 30 in either case.
- On `SIGINT` in unattended mode, behaviour is identical to `SIGTERM`. (No
  human is present; do not prompt.)
- The result file write must complete even if child subprocesses are still
  being torn down.

### FR-7 — Output discipline

When `--unattended` is active:

- Stdout is reserved. Nothing is written to stdout by the orchestrator-facing
  code path. (Subprocess output captured from docker/git/test commands may
  still be relayed for diagnostic purposes, but `ralph++`'s own logs do not
  go to stdout.)
- Stderr receives plain-text human-readable logs: no ANSI colour, no
  spinners, no `rich` panels or rules.
- `NO_COLOR=1` is honoured. A non-TTY stderr is also treated as colour-off.
- Log lines start with an ISO-8601 timestamp and the current step name.

### FR-8 — Concurrency safety

Unattended mode preserves today's concurrency properties:

- Multiple `ralph++` processes may run in parallel, each with a different
  feature, without interference.
- Worktree paths remain unique (existing `{feature}-{8charrandom}` scheme).
- The result file path must be unique per run. Orchestrators that pin
  `--result-file` are responsible for not colliding paths between concurrent
  runs.

## Non-Functional Requirements

### NFR-1 — Backwards compatibility

- All existing CLI flags continue to behave identically when `--unattended`
  is not set.
- No existing exit code is repurposed. Today, generic failures already exit
  non-zero; the MVP narrows that to the table in FR-4 only when unattended
  mode is active.

### NFR-2 — Documentation

- `README.md` gains a short "Unattended mode" section linking to this
  document.
- The result schema is documented inline in this file (above) and serves as
  the source of truth until a JSON Schema is added.

### NFR-3 — Testing

- Unit tests cover:
  - Result file is written on success.
  - Result file is written on each failure category.
  - Result file is written on `SIGTERM`.
  - Atomic write semantics (no partial file observable mid-write).
  - Exit code mapping for each category.
  - `--run-id` round-trips into the result.
  - `RALPH_UNATTENDED=1` activates the mode equivalently to the flag.
  - Plain-output mode disables colour and rich formatting.
- An integration-style test runs `ralph++ run --unattended --dry-run` and
  asserts the produced result file matches the schema.

### NFR-4 — Logging

- A startup log line on stderr declares: run id, feature, mode, worktree
  path, result file path. This is the orchestrator's last fallback if the
  result file is somehow missing.
- A shutdown log line declares: status, exit code, duration.

## CLI Surface

New flags on `ralph++ run`:

| Flag              | Type   | Default                                   | Description                              |
|-------------------|--------|-------------------------------------------|------------------------------------------|
| `--unattended`    | bool   | `false`                                   | Activate unattended mode (FR-1).         |
| `--result-file`   | path   | `{worktree}/scripts/ralph/result.json`    | Where to write the JSON result (FR-2).   |
| `--run-id`        | string | generated ULID/UUIDv7                     | Caller-supplied run identifier (FR-5).   |

New environment variables:

| Variable             | Equivalent to     |
|----------------------|-------------------|
| `RALPH_UNATTENDED=1` | `--unattended`    |

## Acceptance Criteria

The MVP is complete when all of the following hold:

- [ ] `ralph++ run --unattended` succeeds end-to-end against a known-good
      feature and writes a schema-conforming result file with `status:
      "succeeded"` and `exit_code: 0`.
- [ ] A run that fails during PRD review writes a schema-conforming result
      file with `status: "failed"`, a populated `error` object, and exits
      with the documented code.
- [ ] A run that receives `SIGTERM` mid-iteration writes a schema-conforming
      result file with `status: "interrupted"` and exits 30 within a bounded
      time (e.g. one iteration grace period).
- [ ] A bad config / missing required input exits 20 with `status: "failed"`
      and `error.category: "config"` or `"environment"`.
- [ ] With `--unattended`, no ANSI escape codes appear on stderr or stdout.
- [ ] With `--unattended`, `--run-id my-id-123` is echoed in
      `result.run_id`.
- [ ] With `RALPH_UNATTENDED=1` and no flag, behaviour is identical to
      passing `--unattended` explicitly.
- [ ] All existing tests continue to pass.
- [ ] New unit and integration tests cover the criteria above.

## Open Questions

1. **Where does `--result-file` default when no worktree exists?** —
   **Resolved:** the working directory with a `run_id`-based name
   (`./ralph-result-{run_id}.json`). Predictable, no XDG dependency,
   easy for an orchestrator to clean up. See
   [unattended-mode-implementation-plan.md](unattended-mode-implementation-plan.md).
2. **Run id generation: ULID, UUIDv7, or other?** — **Resolved: ULID**
   via the `python-ulid` package. 26-char Crockford base32, sortable by
   creation time, smaller and friendlier on the eye than UUIDv7.
3. **Should `--unattended` be accepted on subcommands other than `run`?** —
   For the MVP, only `run` accepts the flag. `worktrees clean` and `config`
   do not need a result file. `RALPH_UNATTENDED=1` in the environment still
   activates plain-output behaviour for any subcommand so an orchestrator
   spawning them gets clean stderr.
4. **Do we want a `schema_version: 1` JSON Schema file checked into the
   repo?** Useful for orchestrator authors to validate against. Could be
   added as a follow-up; not blocking the MVP.

## Future Work (Not Part of MVP)

Documented here so the MVP design does not paint these into a corner:

- **JSONL event stream** — `--events-file PATH` or `--events-fd N` emitting
  ordered, sequenced events for live progress consumption. The result file
  remains the source of truth for final outcome; the event stream is purely
  a real-time view.
- **`ralph++ status --worktree PATH`** — read-only inspection of a running
  or completed run.
- **`ralph++ resume --run-id ID`** — deterministic resume of an interrupted
  run, emitting a fresh result with a `resumed_from` field.
- **Idempotency key** — `--idempotency-key STR` to prevent duplicate runs
  when the orchestrator is unsure whether a previous trigger started one.
- **Metrics file** — `--metrics-file PATH` writing aggregated counters
  (tokens, subprocess time, retries) at exit.
- **Stdin input** — `--prd-stdin` so orchestrators don't have to write a
  temp file for the PRD.
- **`--max-runtime DURATION`** — built-in wall-clock timeout, exit 124 on
  expiry.

## References

- [README.md](../README.md)
- [AGENTS.md](../AGENTS.md)
- [default-prompt-spec.md](default-prompt-spec.md)
- `ralph_pp/cli.py` — CLI entry point
- `ralph_pp/orchestrator.py` — workflow sequencing
- `ralph_pp/config.py` — config parsing
