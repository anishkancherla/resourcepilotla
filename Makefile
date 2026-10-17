.PHONY: setup test lint typecheck check

setup:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy resourcepilot sellers

check: lint typecheck test
