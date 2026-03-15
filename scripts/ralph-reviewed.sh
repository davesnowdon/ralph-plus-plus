#!/bin/bash
# ralph-reviewed.sh — Ralph Wiggum with blocking inner review loop
# Forked from https://github.com/snarktank/ralph
#
# Additional env vars (injected by ralph++ orchestrator):
#   SKIP_REVIEW=0|1              disable inner review (default: 0)
#   MAX_REVIEW_CYCLES=N          max review+fix cycles per iteration (default: 2)
#   REVIEWER_CMD                 shell command to run reviewer (receives prompt via stdin)
#   FIXER_CMD                    shell command to run fixer (receives prompt via stdin)
#   REVIEW_PROMPT_TEMPLATE       prompt template; {diff} and {prd_file} are substituted
#   FIX_PROMPT_TEMPLATE          prompt template; {findings} is substituted

set -e

# ── Argument parsing ───────────────────────────────────────────────────
TOOL="claude"
MAX_ITERATIONS=10
MAX_REVIEW_CYCLES="${MAX_REVIEW_CYCLES:-2}"
SKIP_REVIEW="${SKIP_REVIEW:-0}"

while [[ $# -gt 0 ]]; do
  case $1 in
    --tool)
      TOOL="$2"
      shift 2
      ;;
    --tool=*)
      TOOL="${1#*=}"
      shift
      ;;
    --max-review-cycles)
      MAX_REVIEW_CYCLES="$2"
      shift 2
      ;;
    --skip-review)
      SKIP_REVIEW=1
      shift
      ;;
    *)
      if [[ "$1" =~ ^[0-9]+$ ]]; then
        MAX_ITERATIONS="$1"
      fi
      shift
      ;;
  esac
done

if [[ "$TOOL" != "amp" && "$TOOL" != "claude" ]]; then
  echo "Error: Invalid tool '$TOOL'. Must be 'amp' or 'claude'."
  exit 1
fi

# ── Paths ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRD_FILE="$SCRIPT_DIR/prd.json"
PROGRESS_FILE="$SCRIPT_DIR/progress.txt"
ARCHIVE_DIR="$SCRIPT_DIR/archive"
LAST_BRANCH_FILE="$SCRIPT_DIR/.last-branch"

# ── Archive previous run if branch changed ─────────────────────────────
if [ -f "$PRD_FILE" ] && [ -f "$LAST_BRANCH_FILE" ]; then
  CURRENT_BRANCH=$(jq -r '.branchName // empty' "$PRD_FILE" 2>/dev/null || echo "")
  LAST_BRANCH=$(cat "$LAST_BRANCH_FILE" 2>/dev/null || echo "")
  if [ -n "$CURRENT_BRANCH" ] && [ -n "$LAST_BRANCH" ] && [ "$CURRENT_BRANCH" != "$LAST_BRANCH" ]; then
    DATE=$(date +%Y-%m-%d)
    FOLDER_NAME=$(echo "$LAST_BRANCH" | sed 's|^ralph/||')
    ARCHIVE_FOLDER="$ARCHIVE_DIR/$DATE-$FOLDER_NAME"
    echo "Archiving previous run: $LAST_BRANCH"
    mkdir -p "$ARCHIVE_FOLDER"
    [ -f "$PRD_FILE" ] && cp "$PRD_FILE" "$ARCHIVE_FOLDER/"
    [ -f "$PROGRESS_FILE" ] && cp "$PROGRESS_FILE" "$ARCHIVE_FOLDER/"
    echo "  Archived to: $ARCHIVE_FOLDER"
    echo "# Ralph Progress Log" > "$PROGRESS_FILE"
    echo "Started: $(date)" >> "$PROGRESS_FILE"
    echo "---" >> "$PROGRESS_FILE"
  fi
fi

# Track current branch
if [ -f "$PRD_FILE" ]; then
  CURRENT_BRANCH=$(jq -r '.branchName // empty' "$PRD_FILE" 2>/dev/null || echo "")
  [ -n "$CURRENT_BRANCH" ] && echo "$CURRENT_BRANCH" > "$LAST_BRANCH_FILE"
fi

# Initialise progress file
if [ ! -f "$PROGRESS_FILE" ]; then
  echo "# Ralph Progress Log" > "$PROGRESS_FILE"
  echo "Started: $(date)" >> "$PROGRESS_FILE"
  echo "---" >> "$PROGRESS_FILE"
fi

echo "Starting Ralph (reviewed)"
echo "Tool:              $TOOL"
echo "Max iterations:    $MAX_ITERATIONS"
echo "Inner review:      $([ "$SKIP_REVIEW" = '1' ] && echo disabled || echo enabled)"
echo "Max review cycles: $MAX_REVIEW_CYCLES"

# ── Helper: run review+fix loop ────────────────────────────────────────
run_review_loop() {
  local iteration=$1

  if [ "$SKIP_REVIEW" = "1" ]; then
    return 0
  fi

  if [ -z "${REVIEWER_CMD:-}" ]; then
    echo "[review] REVIEWER_CMD not set — skipping inner review"
    return 0
  fi

  for r in $(seq 1 "$MAX_REVIEW_CYCLES"); do
    echo ""
    echo "  ┌─ Review cycle $r/$MAX_REVIEW_CYCLES (iteration $iteration) ─"

    # Build diff context (last commit diff, fallback to staged)
    DIFF=$(git diff HEAD~1 HEAD 2>/dev/null || git diff --cached 2>/dev/null || echo "(no diff available)")

    # Substitute template variables
    REVIEW_PROMPT="${REVIEW_PROMPT_TEMPLATE:-Review the git diff against prd.json. Output LGTM if all good.}"
    REVIEW_PROMPT="${REVIEW_PROMPT//\{diff\}/$DIFF}"
    REVIEW_PROMPT="${REVIEW_PROMPT//\{prd_file\}/$PRD_FILE}"

    # Run reviewer
    FINDINGS=$(echo "$REVIEW_PROMPT" | eval "$REVIEWER_CMD" 2>&1) || true

    if echo "$FINDINGS" | grep -q "LGTM"; then
      echo "  └─ Review passed (LGTM)"
      return 0
    fi

    echo "  │  Issues found — running fix pass..."

    # Append findings to progress.txt for future iterations
    {
      echo ""
      echo "## Review findings (iteration $iteration, cycle $r)"
      echo "$(date)"
      echo "$FINDINGS"
      echo "---"
    } >> "$PROGRESS_FILE"

    # Run fixer
    FIX_PROMPT="${FIX_PROMPT_TEMPLATE:-Fix the following issues and ensure tests pass:\n\n{findings}}"
    FIX_PROMPT="${FIX_PROMPT//\{findings\}/$FINDINGS}"
    echo "$FIX_PROMPT" | eval "$FIXER_CMD" 2>&1 | tee /dev/stderr || true

    if [ "$r" -eq "$MAX_REVIEW_CYCLES" ]; then
      echo "  └─ Max review cycles reached — continuing to next iteration"
    fi
  done
}

# ── Main loop ──────────────────────────────────────────────────────────
for i in $(seq 1 $MAX_ITERATIONS); do
  echo ""
  echo "==============================================================="
  echo "  Ralph Iteration $i of $MAX_ITERATIONS ($TOOL)"
  echo "==============================================================="

  # Run the AI coding tool
  if [[ "$TOOL" == "amp" ]]; then
    OUTPUT=$(cat "$SCRIPT_DIR/prompt.md" | amp --dangerously-allow-all 2>&1 | tee /dev/stderr) || true
  else
    OUTPUT=$(claude --dangerously-skip-permissions --print < "$SCRIPT_DIR/CLAUDE.md" 2>&1 | tee /dev/stderr) || true
  fi

  # Check for completion signal before review
  if echo "$OUTPUT" | grep -q "<promise>COMPLETE</promise>"; then
    echo ""
    echo "Ralph completed all tasks!"
    echo "Completed at iteration $i of $MAX_ITERATIONS"
    exit 0
  fi

  # Run inner review+fix loop
  run_review_loop "$i"

  echo "Iteration $i complete. Continuing..."
  sleep 2
done

echo ""
echo "Ralph reached max iterations ($MAX_ITERATIONS) without completing all tasks."
echo "Check $PROGRESS_FILE for status."
exit 1
