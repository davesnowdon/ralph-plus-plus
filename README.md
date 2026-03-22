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
# Minimal
ralph++ --feature "add user authentication"

# With options
ralph++ \
  --feature "add user authentication" \
  --repo /path/to/repo \
  --config ralph++.yaml \
  --claude-config ~/.claude \
  --codex-config ~/.codex \
  --max-iters 20
```

## Configuration

Copy `ralph++.yaml.example` to `ralph++.yaml` and edit to suit your setup.

## Requirements

- Python 3.11+
- Docker (for the Ralph sandbox)
- `claude` CLI installed and authenticated
- `codex` CLI installed and authenticated
- `git` with worktree support
