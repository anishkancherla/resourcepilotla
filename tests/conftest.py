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


@pytest.fixture
def registry_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Give every demo seller a fake registry address, as `make bootstrap` would."""
    from resourcepilot.config import SELLER_AGENTS

    addresses = {}
    for seller in SELLER_AGENTS:
        addresses[seller.key] = f"agent1q{seller.key.replace('_', '')}"
        monkeypatch.setenv(seller.address_env, addresses[seller.key])
        monkeypatch.setenv(seller.wallet_env, f"fetch1{seller.key.replace('_', '')}")
    return addresses


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
