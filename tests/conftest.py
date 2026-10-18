from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from resourcepilot.store import DurableStore, MemoryStore, PostgresStore

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()


@pytest.fixture(params=["memory", "postgres"])
async def any_store(request: pytest.FixtureRequest) -> AsyncIterator[DurableStore]:
    """Run store contract tests against MemoryStore and, when available, real Postgres."""
    if request.param == "memory":
        yield MemoryStore()
        return
    if not TEST_DATABASE_URL:
        pytest.skip("set TEST_DATABASE_URL to run the Postgres store contract")
    store = PostgresStore(TEST_DATABASE_URL)
    await store.setup()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def unique() -> str:
    return uuid4().hex[:12]


@pytest.fixture(scope="session")
def network_env() -> dict[str, str]:
    from network import build_env

    return build_env()


@pytest.fixture(scope="session")
def network_modules(network_env: dict[str, str]):
    from network import load_modules

    return load_modules(network_env)


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch, network_env: dict[str, str], network_modules):
    from network import agent_network

    with agent_network(monkeypatch, network_env, network_modules) as running:
        yield running


@pytest.fixture
async def postgres_network(
    monkeypatch: pytest.MonkeyPatch, network_env: dict[str, str], network_modules
):
    """The whole agent network on real Postgres, isolated in a throwaway schema."""
    import asyncpg
    from network import agent_network

    if not TEST_DATABASE_URL:
        pytest.skip("set TEST_DATABASE_URL to run the network on Postgres")
    schema = f"rp_e2e_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(TEST_DATABASE_URL)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    joiner = "&" if "?" in TEST_DATABASE_URL else "?"
    store = PostgresStore(f"{TEST_DATABASE_URL}{joiner}search_path={schema}")
    await store.setup()
    try:
        with agent_network(monkeypatch, network_env, network_modules, store=store) as running:
            yield running
    finally:
        await store.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
