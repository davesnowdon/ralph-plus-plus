#!/bin/bash
# ralph-single-step.sh — Run exactly one coding tool iteration.
# Used by ralph++ orchestrated mode as a SESSION_RUNNER.
#
# Expects:
#   PROJECT_DIR      — absolute path to the project (set by sandbox entrypoint)
#   RALPH_TOOL       — tool to use: claude or codex (set by sandbox entrypoint)
#   RALPH_PROMPT_FILE — optional path to a custom prompt file (defaults to CLAUDE.md)
#
# The script runs one tool invocation and exits. Ralph++ handles iteration,
# review, retry, and backout logic in Python.

set -e

RALPH_HOME="${PROJECT_DIR}/scripts/ralph"
TOOL="${RALPH_TOOL:-claude}"
PROMPT_FILE="${RALPH_PROMPT_FILE:-${RALPH_HOME}/CLAUDE.md}"

if [[ ! -f "${PROMPT_FILE}" ]]; then
  echo "ERROR: Prompt file not found: ${PROMPT_FILE}" >&2
  exit 1
fi

echo "ralph-single-step: tool=${TOOL}, prompt=${PROMPT_FILE}"

if [[ "$TOOL" == "codex" ]]; then
    cat "${PROMPT_FILE}" | codex 2>&1
else
    claude --dangerously-skip-permissions --print < "${PROMPT_FILE}" 2>&1
fi
