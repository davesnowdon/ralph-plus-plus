.PHONY: install fmt lint typecheck test check

install:
	uv sync

fmt:
	uv run ruff format ralph_pp/ tests/

lint:
	uv run ruff check ralph_pp/ tests/
	uv run ruff format --check ralph_pp/ tests/

typecheck:
	uv run pyright ralph_pp/

test:
	uv run pytest

check: lint typecheck test
