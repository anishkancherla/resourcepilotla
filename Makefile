.PHONY: setup bootstrap profiles verify test lint typecheck check

setup:
	uv sync
	uv run python scripts/bootstrap.py

bootstrap:
	uv run python scripts/bootstrap.py

profiles:
	uv run python scripts/list_profiles.py

verify:
	uv run python scripts/verify_config.py

test:
	uv run pytest

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy resourcepilot sellers scripts/bootstrap.py scripts/verify_config.py

check: lint typecheck test
