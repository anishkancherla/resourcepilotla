from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from resourcepilot.config import (
    ALL_AGENTS,
    PLATFORM_ROLES,
    ROLE_BY_KEY,
    SERVICE_CHAINS,
    SERVICES,
    SPECIALISTS,
    agent_addresses,
    get_role,
    service_chain,
)

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "orchestrator": ("ResourcePilot Orchestrator", 8000),
    "intake": ("ResourcePilot Intake", 8001),
    "researcher": ("ResourcePilot Campus Researcher", 8002),
    "campus_rooms": ("ResourcePilot Campus Rooms", 8003),
    "verification": ("ResourcePilot Verification", 8004),
    "planner": ("ResourcePilot Planner", 8005),
    "memory_audit": ("ResourcePilot Memory and Audit", 8006),
}
REMOVED_SETTINGS = (
    "_WALLET=",
    "PAYMENT_",
    "STRIPE_MOCK",
    "FET_",
    "PLATFORM_FEE",
    "NEW_SELLER_CAP_CENTS",
    "QUOTE_WINDOW",
    "HOLD_WINDOW",
    "HOLD_TTL",
    "ALMANAC_DISCOVERY",
    "SELLER_",
    "DISCOVERY_",
    "ANALYSIS_",
    "FULFILLMENT_",
)


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def env_with_identities(agents: tuple, *, extra: str = "") -> str:
    """The template with fake (never real) identities filled in for `agents`."""
    lines = []
    keys = {agent.seed_env: agent for agent in agents}
    keys.update({agent.address_env: agent for agent in agents})
    for line in (ROOT / ".env.example").read_text().splitlines():
        key = line.split("=", 1)[0]
        agent = keys.get(key)
        if agent is None:
            lines.append(line)
        elif key == agent.seed_env:
            lines.append(f"{key}=kept-seed-{agent.key}-{'x' * 32}")
        else:
            lines.append(f"{key}=agent1qkept{agent.key}")
    return "\n".join(lines) + "\n" + extra


def test_seven_real_agents_with_the_contract_names_and_ports() -> None:
    assert len(PLATFORM_ROLES) == 7
    assert ALL_AGENTS == PLATFORM_ROLES
    assert {role.key: (role.name, role.port) for role in ALL_AGENTS} == EXPECTED
    assert [role.key for role in ALL_AGENTS] == list(EXPECTED)
    assert all(role.responsibility for role in ALL_AGENTS)
    assert set(ROLE_BY_KEY) == set(EXPECTED)


def test_every_identity_has_unique_seed_address_port_and_key() -> None:
    count = len(ALL_AGENTS)
    for attribute in ("key", "port", "seed_env", "address_env", "port_env", "compose_service"):
        assert len({getattr(agent, attribute) for agent in ALL_AGENTS}) == count
    for agent in ALL_AGENTS:
        assert agent.seed_env == f"{agent.key.upper()}_SEED"
        assert agent.address_env == f"{agent.key.upper()}_ADDRESS"
        assert agent.port_env == f"{agent.key.upper()}_PORT"
        assert agent.readme_dir == f"agents/{agent.key}"
    assert ROLE_BY_KEY["campus_rooms"].seed_env == "CAMPUS_ROOMS_SEED"
    assert ROLE_BY_KEY["memory_audit"].compose_service == "memory-audit"


def test_no_seller_or_wallet_identity_remains() -> None:
    import resourcepilot.config as config

    for name in ("SELLER_AGENTS", "SELLER_BY_KEY", "get_seller", "AgentKind"):
        assert not hasattr(config, name), name
    assert not hasattr(ALL_AGENTS[0], "wallet_env")
    assert not hasattr(ALL_AGENTS[0], "kind")


def test_service_chains_use_real_specialists() -> None:
    assert SERVICES == ("rooms", "housing")
    assert SERVICE_CHAINS == {
        "rooms": ("intake", "researcher", "campus_rooms", "verification", "planner"),
        "housing": ("intake", "researcher", "verification"),
    }
    assert SPECIALISTS == {"intake", "researcher", "campus_rooms", "verification", "planner"}
    assert SPECIALISTS <= set(ROLE_BY_KEY) - {"orchestrator", "memory_audit"}


def test_service_chain_falls_back_to_rooms() -> None:
    assert service_chain("rooms") == SERVICE_CHAINS["rooms"]
    assert service_chain("housing", {"brief": {"school_domain": "purdue.edu"}}) == (
        "intake",
        "researcher",
        "verification",
    )
    assert service_chain("bundle") == SERVICE_CHAINS["rooms"]
    assert service_chain("unknown", None) == SERVICE_CHAINS["rooms"]


def test_role_lookup_rejects_unknown_identities(monkeypatch) -> None:
    assert get_role("memory-audit").key == "memory_audit"
    assert get_role("Campus-Rooms").key == "campus_rooms"
    monkeypatch.setenv("AGENT_ROLE", "planner")
    assert get_role().key == "planner"
    for removed in ("discovery", "analysis", "fulfillment", "payment_bot", "gear_share"):
        with pytest.raises(RuntimeError, match="AGENT_ROLE"):
            get_role(removed)


def test_agent_addresses_cover_exactly_the_seven(monkeypatch) -> None:
    for agent in ALL_AGENTS:
        monkeypatch.setenv(agent.address_env, f"agent1q{agent.key}")
    monkeypatch.setenv("DISCOVERY_ADDRESS", "agent1qold")
    assert agent_addresses() == {agent.key: f"agent1q{agent.key}" for agent in ALL_AGENTS}
    monkeypatch.delenv("PLANNER_ADDRESS")
    with pytest.raises(RuntimeError, match="PLANNER_ADDRESS"):
        agent_addresses()
    assert "planner" not in agent_addresses(require_all=False)


def test_template_has_one_empty_identity_per_agent() -> None:
    template = (ROOT / ".env.example").read_text()
    for agent in ALL_AGENTS:
        assert f"\n{agent.seed_env}=\n" in template
        assert f"\n{agent.address_env}=\n" in template
        assert f"\n{agent.port_env}={agent.port}\n" in template
    seeds = set(re.findall(r"^([A-Z_]+)_SEED=", template, flags=re.MULTILINE))
    assert seeds == {agent.env_prefix for agent in ALL_AGENTS}
    assert "agent1q..." not in template
    assert "your_seed" not in template.lower()


def test_template_keeps_runtime_settings_and_drops_payment_ones() -> None:
    template = (ROOT / ".env.example").read_text()
    for kept in (
        "DATABASE_URL=postgresql://",
        "ENABLE_MAILBOX=true",
        "AGENTVERSE_URL=",
        "API_TIMEOUT_SECONDS=",
        "TASK_TIMEOUT_SECONDS=",
    ):
        assert kept in template
    keys = [line.split("=", 1)[0] + "=" for line in template.splitlines() if "=" in line]
    for removed in REMOVED_SETTINGS:
        assert not any(removed in key for key in keys), removed
    assert "mainnet" not in template.lower()


def test_bootstrap_requires_confirmation_before_overwrite() -> None:
    source = (ROOT / "scripts" / "bootstrap.py").read_text()
    assert "if ENV_FILE.exists() and not args.force:" in source
    assert 'confirm(".env already exists. Replace it' in source
    assert "secrets.token_urlsafe(48)" in source
    assert "os.chmod(ENV_FILE, 0o600)" in source
    assert "--add-missing" in source
    assert "wallet" not in source.lower()
    for script in ("bootstrap.py", "verify_config.py", "list_profiles.py"):
        text = (ROOT / "scripts" / script).read_text()
        assert "print(seed" not in text
        assert not re.search(r"print\([^)]*seed_env\)", text)
        assert "SELLER" not in text


def test_add_missing_reports_nothing_when_every_agent_has_an_identity(
    tmp_path, monkeypatch
) -> None:
    bootstrap = load_script("bootstrap")
    leftovers = (
        "DISCOVERY_SEED=old-seed\nCAMPUS_ROOMS_WALLET=fetch1old\nFET_NETWORK=stable-testnet\n"
    )
    env_file = tmp_path / ".env"
    env_file.write_text(env_with_identities(ALL_AGENTS, extra=leftovers))
    before = env_file.read_text()
    monkeypatch.setattr(bootstrap, "ENV_FILE", env_file)

    def no_prompt(_prompt: str) -> str:
        raise AssertionError("nothing to add, so nothing to ask")

    monkeypatch.setattr("builtins.input", no_prompt)
    assert bootstrap.add_missing() == []
    assert env_file.read_text() == before


def test_add_missing_keeps_every_existing_identity(tmp_path, monkeypatch) -> None:
    bootstrap = load_script("bootstrap")
    kept, new = ALL_AGENTS[:-2], ALL_AGENTS[-2:]
    env_file = tmp_path / ".env"
    env_file.write_text(env_with_identities(kept, extra="DISCOVERY_SEED=old-seed\n"))
    before = env_file.read_text()
    monkeypatch.setattr(bootstrap, "ENV_FILE", env_file)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    added = bootstrap.add_missing()
    assert [agent.key for agent in added or []] == [agent.key for agent in new]
    written = env_file.read_text()
    new_keys = {key for agent in new for key in (agent.seed_env, agent.address_env)}
    for line in before.splitlines():
        if line.split("=", 1)[0] not in new_keys:
            assert f"{line}\n" in written
    for agent in kept:
        assert f"{agent.seed_env}=kept-seed-{agent.key}-" in written
    for agent in new:
        assert f"{agent.seed_env}=\n" not in written
        assert f"{agent.address_env}=agent1q" in written
    assert "DISCOVERY_SEED=old-seed\n" in written


def test_add_missing_changes_nothing_without_a_yes(tmp_path, monkeypatch) -> None:
    bootstrap = load_script("bootstrap")
    env_file = tmp_path / ".env"
    env_file.write_text((ROOT / ".env.example").read_text())
    before = env_file.read_text()
    monkeypatch.setattr(bootstrap, "ENV_FILE", env_file)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert bootstrap.add_missing() is None
    assert env_file.read_text() == before


def test_verify_config_ignores_settings_of_removed_agents(tmp_path) -> None:
    verify_config = load_script("verify_config")
    for agent in ALL_AGENTS:
        (tmp_path / agent.readme_dir).mkdir(parents=True)
        (tmp_path / agent.readme_dir / "README.md").write_text("Protocols: chat\n")
    values: dict[str, str | None] = {}
    for agent in ALL_AGENTS:
        values[agent.seed_env] = f"seed-{agent.key}-{'x' * 32}"
        values[agent.address_env] = f"agent1q{agent.key}"
        values[agent.port_env] = str(agent.port)
    leftovers = {
        "DISCOVERY_SEED": "leftover-secret-seed",
        "DISCOVERY_PORT": "8002",
        "WESTWOOD_PRINT_WALLET": "fetch1leftover",
        "FET_NETWORK": "stable-testnet",
    }
    summary = verify_config.verify({**values, **leftovers}, tmp_path)
    assert "7 unique seeds" in summary
    assert "DISCOVERY_SEED" in summary
    for secret in ("leftover-secret-seed", *(str(values[a.seed_env]) for a in ALL_AGENTS)):
        assert secret not in summary


def test_verify_config_rejects_missing_or_duplicate_identities(tmp_path) -> None:
    verify_config = load_script("verify_config")
    values = {agent.seed_env: f"seed-{agent.key}-{'x' * 32}" for agent in ALL_AGENTS}
    values.update({agent.address_env: f"agent1q{agent.key}" for agent in ALL_AGENTS})
    with pytest.raises(SystemExit, match="Missing profile README"):
        verify_config.verify(values, tmp_path)
    with pytest.raises(SystemExit, match="bootstrap-missing"):
        verify_config.verify({**values, "PLANNER_SEED": ""}, tmp_path)
    with pytest.raises(SystemExit, match="Ports must be unique"):
        verify_config.verify({**values, "PLANNER_PORT": "8000"}, tmp_path)
    duplicate = {**values, "PLANNER_SEED": values["INTAKE_SEED"]}
    with pytest.raises(SystemExit, match="Seeds must be"):
        verify_config.verify(duplicate, tmp_path)


def test_compose_runs_exactly_postgres_and_the_seven_agents() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    services_block = compose.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    services = re.findall(r"^  ([a-z][a-z0-9-]*):\n", services_block, flags=re.MULTILINE)
    assert sorted(services) == sorted(["postgres", *(a.compose_service for a in ALL_AGENTS)])
    assert compose.count("AGENT_MODULE: agents.run") == len(ALL_AGENTS)
    for agent in ALL_AGENTS:
        assert f"      AGENT_ROLE: {agent.key}\n" in compose
        assert f"      AGENT_PORT: ${{{agent.port_env}}}\n" in compose
        assert f'ports: ["127.0.0.1:${{{agent.port_env}}}:18000"]' in compose
    for removed in ("sellers.run", "SELLER_ID", "WALLET", "FET_", "STRIPE"):
        assert removed not in compose


def test_packaging_no_longer_ships_sellers_or_payment_dependencies() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "sellers" not in dockerfile
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "agents",
        "resourcepilot",
    ]
    assert not any(dep.startswith("cosmpy") for dep in pyproject["project"]["dependencies"])
    makefile = (ROOT / "Makefile").read_text()
    assert "stripe-mock" not in makefile
    assert "sellers" not in makefile
    assert '"7 agents are starting.' in makefile
    for target in ("setup", "bootstrap", "bootstrap-missing", "start", "verify", "check"):
        assert re.search(rf"^{target}:", makefile, flags=re.MULTILINE), target
