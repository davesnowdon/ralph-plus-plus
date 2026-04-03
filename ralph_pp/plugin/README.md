# Ralph Skills Plugin

Skills for PRD generation and conversion in the Ralph autonomous development
workflow.

Originally based on https://github.com/snarktank/ralph

## Skills

- **prd** — generates a structured Product Requirements Document from a feature
  description, with clarifying questions, user stories, and acceptance criteria.
- **ralph** — converts a text PRD into the `prd.json` format used by Ralph for
  autonomous execution, with properly sized stories and dependency ordering.

## Installation

This plugin is installed automatically by `ralph++` when it detects the skills
are missing. It is placed in `~/.claude/plugins/local/plugins/ralph-skills/`.
