from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from resourcepilot.config import (
    ALL_AGENTS,
    PLATFORM_ROLES,
    SELLER_AGENTS,
    SERVICE_CHAINS,
    get_role,
    get_seller,
    service_chain,
)

ROOT = Path(__file__).resolve().parents[1]


def load_bootstrap() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bootstrap", ROOT / "scripts" / "bootstrap.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nine_platform_roles_and_six_independent_sellers() -> None:
    assert len(PLATFORM_ROLES) == 9
    assert len(SELLER_AGENTS) == 6
    assert len(ALL_AGENTS) == 15
    assert all(agent.kind == "platform" for agent in PLATFORM_ROLES)
    assert all(agent.kind == "seller" for agent in SELLER_AGENTS)


def test_every_identity_has_unique_seed_address_port_and_key() -> None:
    count = len(ALL_AGENTS)
    for attribute in ("key", "port", "seed_env", "address_env", "port_env"):
        assert len({getattr(agent, attribute) for agent in ALL_AGENTS}) == count


def test_service_chains_use_real_platform_roles() -> None:
    roles = {role.key for role in PLATFORM_ROLES}
    for chain in SERVICE_CHAINS.values():
        assert set(chain) <= roles - {"orchestrator", "fulfillment", "memory_audit"}
        assert chain[0] == "intake"
    assert SERVICE_CHAINS["rooms"][-1] == SERVICE_CHAINS["bundle"][-1] == "planner"
    assert SERVICE_CHAINS["housing"] == ("intake", "researcher", "verification")


def test_bundle_only_asks_the_researcher_when_a_school_was_given() -> None:
    assert "researcher" not in service_chain("bundle", {"brief": {"items": {"camera": 1}}})
    assert "researcher" in service_chain("bundle", {"brief": {"school_domain": "purdue.edu"}})
    assert service_chain("rooms") == SERVICE_CHAINS["rooms"]


def test_role_lookup_rejects_unknown_identities() -> None:
    assert get_role("memory-audit").key == "memory_audit"
    assert get_seller("gear-share").key == "gear_share"
    with pytest.raises(RuntimeError, match="AGENT_ROLE"):
        get_role("payment_bot")
    with pytest.raises(RuntimeError, match="SELLER_ID"):
        get_seller("orchestrator")


def test_template_has_no_live_or_placeholder_identity() -> None:
    template = (ROOT / ".env.example").read_text()
    for agent in ALL_AGENTS:
        assert f"{agent.seed_env}=\n" in template
        assert f"{agent.address_env}=\n" in template
    for seller in SELLER_AGENTS:
        assert f"{seller.wallet_env}=\n" in template
    assert "agent1q..." not in template
    assert "your_seed" not in template.lower()


def test_template_is_testnet_only() -> None:
    template = (ROOT / ".env.example").read_text()
    assert "FET_NETWORK=stable-testnet" in template
    assert "FET_DENOM=atestfet" in template
    assert "mainnet" not in template.lower()


def test_bootstrap_requires_confirmation_before_overwrite() -> None:
    source = (ROOT / "scripts" / "bootstrap.py").read_text()
    assert "if ENV_FILE.exists() and not args.force:" in source
    assert 'confirm(".env already exists. Replace it' in source
    assert "secrets.token_urlsafe(48)" in source
    assert "os.chmod(ENV_FILE, 0o600)" in source
    assert "print(seed" not in source


def test_add_missing_keeps_every_existing_identity(tmp_path, monkeypatch) -> None:
    bootstrap = load_bootstrap()

    template = (ROOT / ".env.example").read_text()
    existing = {agent.seed_env: f"kept-seed-{agent.key}" for agent in ALL_AGENTS[:-2]}
    lines = [
        f"{key}={existing[key]}" if key in existing else line
        for line in template.splitlines()
        for key in [line.split("=", 1)[0]]
    ]
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(bootstrap, "ENV_FILE", env_file)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    added = bootstrap.add_missing()
    assert [agent.key for agent in added or []] == [agent.key for agent in ALL_AGENTS[-2:]]
    written = env_file.read_text()
    for key, seed in existing.items():
        assert f"{key}={seed}\n" in written
    for agent in ALL_AGENTS[-2:]:
        assert f"{agent.seed_env}=\n" not in written
        assert f"{agent.address_env}=agent1q" in written


def test_add_missing_changes_nothing_without_a_yes(tmp_path, monkeypatch) -> None:
    bootstrap = load_bootstrap()

    env_file = tmp_path / ".env"
    env_file.write_text((ROOT / ".env.example").read_text())
    before = env_file.read_text()
    monkeypatch.setattr(bootstrap, "ENV_FILE", env_file)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert bootstrap.add_missing() is None
    assert env_file.read_text() == before
