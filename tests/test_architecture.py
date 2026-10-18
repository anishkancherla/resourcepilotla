"""Static checks that the deployed shape matches the architecture the docs describe."""

from __future__ import annotations

from pathlib import Path

from resourcepilot.config import ALL_AGENTS, PLATFORM_ROLES

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.yml").read_text()
RUNTIME = (ROOT / "agents" / "run.py").read_text()
REMOVED = (
    "sellers",
    "resourcepilot/bundles.py",
    "resourcepilot/checkout.py",
    "resourcepilot/escrow.py",
    "resourcepilot/logistics.py",
    "resourcepilot/payment.py",
    "resourcepilot/pricing.py",
    "resourcepilot/protocols.py",
    "resourcepilot/registry.py",
    "scripts/demo_payment_buyer.py",
)


def test_compose_runs_every_agent_as_its_own_service() -> None:
    for role in PLATFORM_ROLES:
        assert f"  {role.compose_service}:\n" in COMPOSE
        assert f"AGENT_ROLE: {role.key}\n" in COMPOSE
    assert COMPOSE.count("AGENT_MODULE: agents.run") == len(PLATFORM_ROLES)
    assert "SELLER_ID" not in COMPOSE


def test_every_published_port_is_loopback_only() -> None:
    for identity in ALL_AGENTS:
        assert f'"127.0.0.1:${{{identity.port_env}}}:18000"' in COMPOSE
    assert '"127.0.0.1:5432:5432"' in COMPOSE
    assert "http://127.0.0.1:18000/health" in COMPOSE
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert 'CMD ["python", "-m", "resourcepilot.container_entrypoint"]' in dockerfile
    assert "USER app" in dockerfile
    assert "sellers" not in dockerfile


def test_containers_share_postgres_as_the_durable_store() -> None:
    assert "condition: service_healthy" in COMPOSE
    template = (ROOT / ".env.example").read_text()
    assert "DATABASE_URL=postgresql://" in template
    assert "@postgres:5432/" in template


def test_every_agent_has_a_profile_readme_naming_its_protocols() -> None:
    for identity in ALL_AGENTS:
        readme = ROOT / identity.readme_dir / "README.md"
        assert readme.exists(), readme
        assert "Protocols:" in readme.read_text()


def test_runtime_publishes_manifests_and_delegates_with_ctx_send() -> None:
    assert "Protocol(spec=chat_protocol_spec)" in RUNTIME
    assert "agent.include(chat_proto, publish_manifest=True)" in RUNTIME
    assert "agent.include(task_proto, publish_manifest=True)" in RUNTIME
    assert "await ctx.send(ADDRESSES[task.target_role], task)" in RUNTIME
    assert "from uagents_core.contrib.protocols.chat import" in RUNTIME


def test_eight_real_agents_and_only_live_roles_use_the_network() -> None:
    assert len(ALL_AGENTS) == 8
    assert [identity.key for identity in ALL_AGENTS] == [
        "orchestrator",
        "intake",
        "researcher",
        "campus_rooms",
        "verification",
        "planner",
        "memory_audit",
        "emergency",
    ]
    assert len({identity.port for identity in ALL_AGENTS}) == 8
    importers = sorted(
        str(path.relative_to(ROOT))
        for path in (*ROOT.glob("agents/*.py"), *ROOT.glob("resourcepilot/*.py"))
        if "resourcepilot.http import" in path.read_text()
    )
    # agents/run.py hosts the Researcher, Campus Rooms, Verification's link re-checks, and the
    # Emergency Dispatcher, the only part that calls ElevenLabs.
    assert importers == [
        "agents/run.py",
        "resourcepilot/emergency.py",
        "resourcepilot/research.py",
        "resourcepilot/rooms.py",
    ]


def test_no_payment_code_or_demo_shops_remain() -> None:
    for relative in REMOVED:
        assert not (ROOT / relative).exists(), relative
    for path in (*ROOT.glob("agents/*.py"), *ROOT.glob("resourcepilot/*.py")):
        text = path.read_text()
        assert "payment_protocol_spec" not in text, path
        assert "resource_hold_spec" not in text, path
        assert "import stripe" not in text, path
        assert "cosmpy" not in text, path
