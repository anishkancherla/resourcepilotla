"""Static checks that the deployed shape matches the architecture the docs describe."""

from __future__ import annotations

from pathlib import Path

from resourcepilot.config import ALL_AGENTS, PLATFORM_ROLES, SELLER_AGENTS

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.yml").read_text()
RUNTIME = (ROOT / "agents" / "run.py").read_text()
SELLER_RUNTIME = (ROOT / "sellers" / "run.py").read_text()


def test_compose_runs_every_agent_as_its_own_service() -> None:
    for role in PLATFORM_ROLES:
        assert f"  {role.compose_service}:\n" in COMPOSE
        assert f"AGENT_ROLE: {role.key}\n" in COMPOSE
    for seller in SELLER_AGENTS:
        assert f"  {seller.compose_service}:\n" in COMPOSE
        assert f"SELLER_ID: {seller.key}\n" in COMPOSE
    assert COMPOSE.count("AGENT_MODULE: agents.run") == len(PLATFORM_ROLES)
    assert COMPOSE.count("AGENT_MODULE: sellers.run") == len(SELLER_AGENTS)


def test_every_published_port_is_loopback_only() -> None:
    for identity in ALL_AGENTS:
        assert f'"127.0.0.1:${{{identity.port_env}}}:18000"' in COMPOSE
    assert '"127.0.0.1:5432:5432"' in COMPOSE
    assert "http://127.0.0.1:18000/health" in COMPOSE
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert 'CMD ["python", "-m", "resourcepilot.container_entrypoint"]' in dockerfile
    assert "USER app" in dockerfile


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


def test_platform_runtime_publishes_manifests_and_delegates_with_ctx_send() -> None:
    assert "Protocol(spec=chat_protocol_spec)" in RUNTIME
    assert "agent.include(chat_proto, publish_manifest=True)" in RUNTIME
    assert "agent.include(task_proto, publish_manifest=True)" in RUNTIME
    assert "await ctx.send(ADDRESSES[task.target_role], task)" in RUNTIME
    assert 'Protocol(spec=resource_hold_spec, role="buyer")' in RUNTIME
    assert "agent.include(hold_proto, publish_manifest=True)" in RUNTIME


def test_orchestrator_is_the_official_payment_seller() -> None:
    assert 'Protocol(spec=payment_protocol_spec, role="seller")' in RUNTIME
    assert "agent.include(payment_proto, publish_manifest=True)" in RUNTIME
    for message in ("RequestPayment(", "CompletePayment(", "CancelPayment("):
        assert message in RUNTIME
    assert "from uagents_core.contrib.protocols.payment import" in RUNTIME
    assert "from uagents_core.contrib.protocols.chat import" in RUNTIME


def test_sellers_speak_the_resource_hold_seller_role() -> None:
    assert 'Protocol(spec=resource_hold_spec, role="seller")' in SELLER_RUNTIME
    assert "agent.include(hold_proto, publish_manifest=True)" in SELLER_RUNTIME
    assert "agent.include(chat_proto, publish_manifest=True)" in SELLER_RUNTIME
    # Sellers never take buyer payments; the Orchestrator's escrow does.
    assert "payment_protocol_spec" not in SELLER_RUNTIME


def test_mock_stripe_is_named_and_documented_as_moving_no_money() -> None:
    payment = (ROOT / "resourcepilot" / "payment.py").read_text()
    assert 'STRIPE_MOCK = "stripe_mock"' in payment
    assert "No Stripe API call, no funds moved" in payment
    assert "import stripe" not in payment
    for path in ROOT.rglob("*.py"):
        if {".venv", "tests"} & set(path.relative_to(ROOT).parts):
            continue
        assert "import stripe" not in path.read_text(), path
