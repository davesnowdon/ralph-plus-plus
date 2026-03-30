# AGENTS

## Repo Purpose

`ralph-plus-plus` automates the higher-level Ralph workflow:

- create a worktree and branch
- run configured hooks
- generate and review a PRD
- convert the PRD to `prd.json`
- run `ralph-sandbox` in delegated or orchestrated mode
- perform post-run review/fix passes

Key files:

- `ralph_pp/cli.py`: command-line entry point
- `ralph_pp/config.py`: config parsing and validation
- `ralph_pp/orchestrator.py`: top-level workflow sequencing
- `ralph_pp/steps/`: worktree, PRD, sandbox, and post-review logic
- `scripts/ralph-single-step.sh`: custom session runner used by orchestrated mode
- `tests/`: Python unit and workflow tests

## Tooling Baseline

This repo uses:

- `uv` for environment management and command execution
- `pytest` for tests
- `ruff` for linting and formatting checks
- `pyright` for type checking

Primary commands:

```bash
make install
make lint
make typecheck
make test
make check
```

## Change Rules For Agents

Agents changing this repo must preserve the workflow contract across config,
step logic, docs, and tests.

When changing orchestration behavior:

- update tests in the same change
- keep delegated and orchestrated modes explicit
- preserve the contract with `ralph-sandbox`'s wrapper and custom-runner mode
- fail fast on infra/tooling errors instead of silently treating them as normal
  review findings
- keep config validation aligned with the documented config surface

If you change prompt handoff, sandbox command construction, or iteration/review
control flow, add or update tests under `tests/test_sandbox*.py`.

If you change PRD generation/review behavior, add or update tests under
`tests/test_prd.py` and `tests/test_review_loops.py`.

## Done Criteria

Before a change can be considered done, run:

```bash
uv sync --group dev
make check
```

`make check` currently means:

- `make lint`
- `make typecheck`
- `make test`

For changes that affect the integration contract with `ralph-sandbox`, also run
a manual integration test against a real sandbox checkout.

Recommended manual cases:

- delegated mode with `claude`
- delegated mode with `codex`
- orchestrated mode with custom `SESSION_RUNNER`
- linked git worktree execution
- failure handling for reviewer/fixer/tooling errors

## Notes

The test suite in this repo is intentionally heavy on orchestrated-mode edge
cases. Do not remove negative-path tests unless the corresponding behavior is
also being removed.
