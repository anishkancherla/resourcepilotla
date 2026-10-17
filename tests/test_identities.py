from __future__ import annotations

from pathlib import Path

import pytest

from resourcepilot.config import (
    ALL_AGENTS,
    PLATFORM_ROLES,
    SELLER_AGENTS,
    SPECIALIST_CHAIN,
    get_role,
    get_seller,
)

ROOT = Path(__file__).resolve().parents[1]


def test_eight_platform_roles_and_five_independent_sellers() -> None:
    assert len(PLATFORM_ROLES) == 8
    assert len(SELLER_AGENTS) == 5
    assert all(agent.kind == "platform" for agent in PLATFORM_ROLES)
    assert all(agent.kind == "seller" for agent in SELLER_AGENTS)


def test_every_identity_has_unique_seed_address_port_and_key() -> None:
    count = len(ALL_AGENTS)
    for attribute in ("key", "port", "seed_env", "address_env", "port_env"):
        assert len({getattr(agent, attribute) for agent in ALL_AGENTS}) == count


def test_specialist_chain_uses_real_platform_roles() -> None:
    assert set(SPECIALIST_CHAIN) <= {role.key for role in PLATFORM_ROLES}
    assert SPECIALIST_CHAIN[0] == "intake"
    assert SPECIALIST_CHAIN[-1] == "planner"


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
    assert "and not confirm_overwrite()" in source
    assert "secrets.token_urlsafe(48)" in source
    assert "os.chmod(ENV_FILE, 0o600)" in source
    assert "print(seed" not in source
