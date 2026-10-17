.PHONY: setup bootstrap start stop logs profiles verify test lint typecheck check build stripe-mock

setup:
	uv sync
	uv run python scripts/bootstrap.py

bootstrap:
	uv run python scripts/bootstrap.py

start:
	docker compose up --build -d
	@echo "13 agents are starting. Run 'make profiles', open every Inspector URL, and connect Mailbox."

stop:
	docker compose down

logs:
	docker compose logs -f

profiles:
	uv run python scripts/list_profiles.py

verify:
	uv run python scripts/verify_config.py
	docker compose config --quiet

test:
	uv run pytest

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy agents resourcepilot sellers scripts

check: lint typecheck test

build:
	docker build -t resourcepilot-la .

# Drive one full Mock Stripe checkout against the running Orchestrator. No money moves.
stripe-mock:
	@echo "WARNING: STRIPE_MOCK makes no Stripe API call and moves no money."
	uv run python scripts/demo_payment_buyer.py
