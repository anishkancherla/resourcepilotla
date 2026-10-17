.PHONY: setup bootstrap profiles verify test lint typecheck check stripe-mock

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
	uv run mypy resourcepilot sellers scripts/bootstrap.py scripts/verify_config.py \
		scripts/demo_payment_buyer.py

check: lint typecheck test

# Drive one full Mock Stripe checkout against the running Orchestrator. No money moves.
stripe-mock:
	uv run python scripts/demo_payment_buyer.py
