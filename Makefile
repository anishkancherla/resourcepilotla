.PHONY: setup bootstrap bootstrap-missing start stop logs profiles verify test lint typecheck check build

setup:
	uv sync
	uv run python scripts/bootstrap.py

bootstrap:
	uv run python scripts/bootstrap.py

# Keep every existing identity; add only agents that are new since .env was created.
bootstrap-missing:
	uv run python scripts/bootstrap.py --add-missing

start:
	docker compose up --build -d
	@echo "7 agents are starting. Run 'make profiles', open every Inspector URL, and connect Mailbox."

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
	uv run mypy agents resourcepilot scripts

check: lint typecheck test

build:
	docker build -t resourcepilot-la .
