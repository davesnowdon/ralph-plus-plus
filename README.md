# ralph++

WORK IN PROGRESS DO NOT USE

Automated orchestration for the [Ralph](https://github.com/snarktank/ralph) agentic coding workflow.

## What it does

Runs the full Ralph workflow end-to-end from a single command:

1. Creates a git worktree + branch (slugified feature name + random suffix)
2. Runs lifecycle hooks (e.g. `codegraph init && codegraph index`)
3. Generates a PRD via the Claude `/prd` skill
4. Iteratively reviews and improves the PRD (configurable reviewer/fixer, max cycles)
5. Converts the PRD to `prd.json` via the Claude `/ralph` skill
6. Runs Ralph inside the docker sandbox with an inner review loop after each iteration
7. Runs a post-completion review + fix loop
8. Cleans up git config

## Installation

```bash
pip install -e .
```

## Usage

```bash
# Zero-config (uses built-in defaults)
ralph++ --feature "add user authentication"

# Point at a specific repo
ralph++ --repo /path/to/repo --feature "add user authentication"

# With explicit options
ralph++ \
  --feature "add user authentication" \
  --repo /path/to/repo \
  --mode orchestrated \
  --max-iters 20
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
```

### Sandbox resolution

If `ralph.sandbox_dir` is not set, ralph++ resolves the sandbox automatically:

1. `RALPH_SANDBOX_DIR` environment variable
2. `ralph-sandbox` on `PATH`
3. Sibling checkout (`../ralph-sandbox` relative to `--repo`)

### Test command auto-detection

When `orchestrated.run_tests_between_steps` is enabled but `test_commands` is
empty, ralph++ auto-detects commands for common project types (Makefile, pytest,
npm, cargo, go).

### Examples

- [`examples/user-config.yaml`](examples/user-config.yaml) — machine-local preferences
- [`examples/project-config.yaml`](examples/project-config.yaml) — repo-specific settings
- [`ralph++.yaml.example`](ralph++.yaml.example) — full reference with all options

## Requirements

- Python 3.11+
- Docker (for the Ralph sandbox)
- `claude` CLI installed and authenticated
- `codex` CLI installed and authenticated
- `git` with worktree support
