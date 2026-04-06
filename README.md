# ralph++

Automated orchestration for the [Ralph](https://github.com/snarktank/ralph) agentic coding workflow.

## What it does

Runs the full Ralph workflow end-to-end from a single command:

1. Creates a git worktree + branch (slugified feature name + random suffix)
2. Runs lifecycle hooks (e.g. `hatch env create`, `codegraph init`)
3. Generates a PRD via the Claude `/prd` skill (or accepts a pre-written one)
4. Iteratively reviews and improves the PRD (configurable reviewer/fixer, max cycles)
5. Converts the PRD to `prd.json` and reviews the generated stories for feasibility
6. Runs Ralph inside the docker sandbox with an inner review loop after each iteration
7. Runs a post-completion review + fix loop
8. Cleans up git config and orchestration artifacts

## Installation

```bash
pip install -e .
# or
uv pip install -e .
```

## Quick Start

```bash
# Simplest usage — generate PRD from a short feature description
ralph++ run --feature "add user authentication" --repo /path/to/repo

# Use a pre-written PRD (skips generation and review)
ralph++ run \
  --prd-file tasks/prd-memory-unification.md \
  --repo /path/to/repo

# Separate the branch name from the PRD prompt
ralph++ run \
  --feature "memory-unification" \
  --prd-prompt "Unify the dual memory systems behind a single canonical contract. \
Promote MemoryRecord as the single record type, migrate all runtime consumers..." \
  --repo /path/to/repo

# PRD prompt from a file
ralph++ run \
  --feature "memory-unification" \
  --prd-prompt "$(cat docs/memory-unification-plan.md)" \
  --repo /path/to/repo
```

## Workflow Modes

ralph++ supports three modes, each with different speed/quality tradeoffs:

### Delegated Mode

A single Claude session runs all iterations inside the sandbox. Fastest, but
defers quality issues to the post-run review.

```bash
ralph++ run \
  --feature "add memory store" \
  --repo /path/to/repo \
  --mode delegated \
  --max-iters 10
```

### Orchestrated Mode — Backout

ralph++ controls each iteration. If the reviewer rejects an iteration, the
changes are backed out and the coder retries from a clean state.

```bash
ralph++ run \
  --prd-file tasks/prd-memory-unification.md \
  --repo /path/to/repo \
  --config /path/to/repo/.ralph/ralph++-orch-backout.yaml \
  --max-iters 20
```

### Orchestrated Mode — Fix-in-place

Like backout mode, but instead of reverting, the fixer agent patches the code
in-place based on the reviewer's findings. Slower but catches more issues
inline, resulting in cleaner post-run reviews.

```bash
ralph++ run \
  --prd-file tasks/prd-memory-unification.md \
  --repo /path/to/repo \
  --config /path/to/repo/.ralph/ralph++-orch-fixinplace.yaml \
  --max-iters 20
```

### Mode Comparison

|                         | Delegated        | Orchestrated + Backout | Orchestrated + Fix-in-place |
|-------------------------|------------------|------------------------|-----------------------------|
| Speed                   | Fastest (~40min) | Medium (~70min)        | Slowest (~110min)           |
| Inline review           | None             | Per-iteration          | Per-iteration + fix cycles  |
| Issues caught inline    | 0                | Varies                 | Most                        |
| Post-run review quality | Issues deferred  | Issues deferred        | Usually clean LGTM          |
| Best for                | Trusted codebases, speed | Balance of speed and quality | Maximum quality        |

*Times are from real runs on an 8-story PRD against a ~750-line Python project.*

## PRD Workflows

### Auto-generate from a feature description

```bash
ralph++ run --feature "add user authentication" --repo /path/to/repo
```

### Interactive PRD session

Opens an interactive Claude session where you drive the PRD creation manually:

```bash
ralph++ run \
  --manual-prd \
  --feature "memory-unification" \
  --repo /path/to/repo
```

### Generate PRD only (no implementation)

Generate and review the PRD without creating a worktree or running any code:

```bash
ralph++ run \
  --prd-only \
  --feature "memory-unification" \
  --repo /path/to/repo
```

### Provide a pre-written PRD

Skip PRD generation entirely and go straight to implementation:

```bash
ralph++ run \
  --prd-file /path/to/tasks/prd-memory-unification.md \
  --repo /path/to/repo
```

The `--feature` flag is optional when using `--prd-file` — the feature name
is derived from the filename (e.g. `prd-memory-unification.md` → `memory-unification`).

### Separate feature name from PRD prompt

`--feature` names the branch and worktree. `--prd-prompt` provides the
(potentially long) description for PRD generation:

```bash
ralph++ run \
  --feature "memory-unification" \
  --prd-prompt "$(cat docs/memory-unification-plan.md)" \
  --repo /path/to/repo
```

## Resuming a Failed Run

If a run fails or is interrupted, the worktree is preserved. Resume from where
it left off (skips worktree creation and PRD generation):

```bash
ralph++ run \
  --resume-worktree /path/to/ralph-memory-unification-d23f \
  --feature "memory-unification"
```

## Running Specific Stories

Run only a subset of stories from the PRD:

```bash
ralph++ run \
  --resume-worktree /path/to/worktree \
  --feature "memory-unification" \
  --story US-005 --story US-006
```

## Worktree Management

```bash
# List all ralph++ worktrees
ralph++ worktrees list --repo /path/to/repo

# Remove all ralph++ worktrees and branches
ralph++ worktrees clean --repo /path/to/repo

# Force-remove (including dirty worktrees)
ralph++ worktrees clean --force --repo /path/to/repo
```

## Configuration

ralph++ works with zero configuration on a machine that has `claude`, `codex`,
and `ralph-sandbox` installed in standard locations. For customisation, config
files are loaded in layers (later wins):

1. **Built-in defaults** — sensible values for all settings
2. **User config** — `~/.config/ralph-plus-plus/config.yaml` or `~/.ralph++.yaml`
3. **Project config** — `<repo>/.ralph/ralph++.yaml` or `<repo>/ralph++.yaml`
4. **CLI flags** — highest precedence

### Inspect effective config

```bash
ralph++ config                          # show merged defaults
ralph++ config --repo /path/to/repo     # include project config
ralph++ config --show-sources           # show which layer set each value
```

### Minimal project config (orchestrated + fix-in-place)

The most common starting point for a new project. Place this at
`<repo>/.ralph/ralph++.yaml`:

```yaml
ralph:
  mode: orchestrated
  sandbox_dir: /path/to/ralph-sandbox
  session_runner: scripts/ralph-single-step.sh

orchestrated:
  coder: claude
  reviewer: codex
  fixer: claude
  backout_on_failure: false       # fix-in-place mode
  max_iteration_retries: 5        # fix cycles before aborting
  run_tests_between_steps: true
  test_commands:
    - ruff format .               # auto-format before CI (prevents trivial failures)
    - make check                  # your project's CI command

hooks:
  post_worktree_create:
    - "make install"              # set up the dev environment in the new worktree
```

### Orchestrated + backout config

```yaml
ralph:
  mode: orchestrated
  sandbox_dir: /path/to/ralph-sandbox
  session_runner: scripts/ralph-single-step.sh

orchestrated:
  coder: claude
  reviewer: codex
  fixer: claude
  backout_on_failure: true        # revert and retry on review failure
  max_iteration_retries: 3
  run_tests_between_steps: true
  test_commands:
    - make check

hooks:
  post_worktree_create:
    - "make install"
```

### Python project config (hatch)

```yaml
ralph:
  mode: orchestrated
  sandbox_dir: /path/to/ralph-sandbox
  session_runner: scripts/ralph-single-step.sh

orchestrated:
  coder: claude
  reviewer: codex
  fixer: claude
  backout_on_failure: false
  max_iteration_retries: 5
  run_tests_between_steps: true
  test_commands:
    - hatch run ruff format .
    - hatch run ci

hooks:
  post_worktree_create:
    - "hatch env create"
```

### Node.js project config

```yaml
ralph:
  mode: orchestrated
  sandbox_dir: /path/to/ralph-sandbox
  session_runner: scripts/ralph-single-step.sh

orchestrated:
  backout_on_failure: false
  max_iteration_retries: 5
  run_tests_between_steps: true
  test_commands:
    - npm run lint:fix
    - npm test

hooks:
  post_worktree_create:
    - "npm ci"
```

### Sandbox resolution

If `ralph.sandbox_dir` is not set, ralph++ resolves the sandbox automatically:

1. `RALPH_SANDBOX_DIR` environment variable
2. `ralph-sandbox` on `PATH` (must be inside a checkout with `docker-compose.yml`)
3. Sibling checkout (`../ralph-sandbox` relative to `--repo`)

### Test command auto-detection

When `orchestrated.run_tests_between_steps` is enabled but `test_commands` is
empty, ralph++ auto-detects commands for common project types (Makefile, pytest,
npm, cargo, go).

### Full config reference

See [`ralph++.yaml.example`](ralph++.yaml.example) for all available options
with comments.

## CLI Reference

```
ralph++ run [OPTIONS]

Options:
  -f, --feature TEXT          Feature name (used for branch/worktree naming)
  -r, --repo PATH             Path to the git repository
  -c, --config PATH            Path to ralph++.yaml config file
  -m, --mode [delegated|orchestrated]  Workflow mode
  --max-iters INTEGER         Maximum Ralph iterations
  --prd-prompt TEXT            Rich prompt for PRD generation (--feature still names the branch)
  --prd-file PATH              Use a pre-written PRD (skips generation/review)
  --prd-only                   Generate PRD only, no implementation
  --manual-prd                 Interactive Claude session for PRD generation
  --resume-worktree PATH       Resume from an existing worktree
  --story TEXT                 Run specific stories (repeatable)
  --skip-prd-review            Skip the PRD review loop
  --skip-post-review           Skip the post-run review loop
  --claude-config PATH         Path to Claude config directory
  --codex-config PATH          Path to Codex config directory
  --sandbox-dir PATH           Path to ralph-sandbox checkout
  --setup-cmd TEXT             Shell command to run after worktree creation (repeatable)
  --dry-run                    Print what would be done
```

```
ralph++ worktrees list [OPTIONS]
ralph++ worktrees clean [OPTIONS]

Options:
  -r, --repo PATH    Path to the git repository
  --force            Force removal of dirty worktrees (clean only)
```

```
ralph++ config [OPTIONS]

Options:
  -r, --repo PATH       Path to the git repository
  -c, --config PATH      Path to ralph++.yaml config file
  --show-sources         Show which config layer set each value
```

## Requirements

- Python 3.11+
- Docker (for the Ralph sandbox)
- `claude` CLI installed and authenticated
- `codex` CLI installed and authenticated (for reviewer role)
- `git` with worktree support
