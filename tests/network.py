"""In-process agent network for end-to-end tests.

Each platform role and seller is loaded as its own module instance with its own seed, address,
and handlers, exactly as its container would load it. Messages sent with ``ctx.send`` are queued
and delivered to the destination module's real handler. A shared MemoryStore stands in for the
shared Postgres. The clock can be advanced to exercise timeouts without sleeping.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from uagents import Agent, Model
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage

from resourcepilot import clock
from resourcepilot.config import ALL_AGENTS, PLATFORM_ROLES, SELLER_AGENTS
from resourcepilot.store import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
USER = "agent1qtestbuyer0000000000000000000000000000000000000000000000000"
SESSION = "session-under-test"

HANDLER_NAMES = {
    "ChatMessage": "handle_chat",
    "ChatAcknowledgement": "handle_ack",
    "TaskRequest": "handle_task",
    "TaskResult": "handle_task_result",
    "AuditEvent": "handle_audit",
    "Quote": "handle_quote",
    "QuoteRequest": "handle_quote_request",
    "HoldRequest": "handle_hold_request",
    "HoldConfirmed": "handle_hold_confirmed",
    "HoldRejected": "handle_hold_rejected",
    "CommitHold": "handle_commit",
    "BookingConfirmed": "handle_booking_confirmed",
    "ReleaseHold": "handle_release",
    "CommitPayment": "handle_commit",
    "RejectPayment": "handle_reject",
}


def build_env() -> dict[str, str]:
    env = {
        "ENABLE_MAILBOX": "false",
        "DATABASE_URL": "",
        "QUOTE_WINDOW_SECONDS": "5",
        "HOLD_WINDOW_SECONDS": "5",
        "TASK_TIMEOUT_SECONDS": "30",
    }
    for identity in ALL_AGENTS:
        seed = f"resourcepilot-test-seed-{identity.key}-" + "x" * 32
        probe = Agent(name=f"probe-{identity.key}", seed=seed)
        env[identity.seed_env] = seed
        env[identity.address_env] = str(probe.address)
        if identity.kind == "seller":
            env[identity.wallet_env] = str(probe.wallet.address())
    return env


class FakeContext:
    def __init__(self, network: AgentNetwork, address: str, session: str = SESSION) -> None:
        self.network = network
        self.address = address
        self.session = session
        self.logger = logging.getLogger(f"agent.{address[:12]}")

    async def send(self, destination: str, message: Model, **_: Any) -> None:
        self.network.queue.append((self.address, destination, message, self.session))

    def get_agents_by_protocol(self, digest: str, limit: int = 10) -> list[str]:
        return list(self.network.almanac.get(digest, []))[:limit]


@dataclass
class AgentNetwork:
    modules: dict[str, ModuleType] = field(default_factory=dict)
    by_key: dict[str, ModuleType] = field(default_factory=dict)
    queue: deque[tuple[str, str, Model, str]] = field(default_factory=deque)
    inbox: list[Model] = field(default_factory=list)
    almanac: dict[str, list[str]] = field(default_factory=dict)
    dropped: list[tuple[str, str]] = field(default_factory=list)
    offset: timedelta = timedelta()
    store: MemoryStore = field(default_factory=MemoryStore)

    def address(self, key: str) -> str:
        return str(self.by_key[key].agent.address)

    async def send_user(self, message: Model, to: str = "orchestrator") -> None:
        self.queue.append((USER, self.address(to), message, SESSION))
        await self.run()

    async def run(self, limit: int = 2000) -> None:
        for _ in range(limit):
            if not self.queue:
                return
            sender, destination, message, session = self.queue.popleft()
            if destination == USER:
                if not isinstance(message, ChatAcknowledgement):
                    self.inbox.append(message)
                continue
            module = self.modules.get(destination)
            name = HANDLER_NAMES.get(type(message).__name__)
            handler = getattr(module, name, None) if module and name else None
            if handler is None:
                self.dropped.append((destination, type(message).__name__))
                continue
            await handler(FakeContext(self, destination, session), sender, message)
        raise RuntimeError("message loop did not settle")

    async def advance(self, seconds: float) -> None:
        """Move every agent's clock forward, then run each interval sweep once."""
        self.offset += timedelta(seconds=seconds)
        for address, module in self.modules.items():
            sweep = getattr(module, "sweep", None)
            if sweep is not None:
                await sweep(FakeContext(self, address))
        await self.run()

    def cards(self) -> list[tuple[str, dict[str, Any], str]]:
        """Return (card_kind, payload, narration) for every card the user received."""
        import json

        found = []
        for message in self.inbox:
            if not isinstance(message, ChatMessage):
                continue
            text = " ".join(getattr(c, "text", "") for c in message.content if hasattr(c, "text"))
            for content in message.content:
                metadata = getattr(content, "metadata", None)
                if metadata and "card_kind" in metadata:
                    found.append(
                        (metadata["card_kind"], json.loads(metadata["card_payload"]), text)
                    )
        return found

    def texts(self) -> list[str]:
        return [
            " ".join(getattr(c, "text", "") for c in message.content if hasattr(c, "text"))
            for message in self.inbox
            if isinstance(message, ChatMessage)
        ]


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_modules(env: dict[str, str]) -> dict[str, ModuleType]:
    """Import every runtime once, each under its own role, with the test identities."""
    saved = {key: os.environ.get(key) for key in [*env, "AGENT_ROLE", "SELLER_ID"]}
    os.environ.update(env)
    modules: dict[str, ModuleType] = {}
    try:
        for role in PLATFORM_ROLES:
            os.environ["AGENT_ROLE"] = role.key
            modules[role.key] = _load(ROOT / "agents" / "run.py", f"_test_agent_{role.key}")
        for seller in SELLER_AGENTS:
            os.environ["SELLER_ID"] = seller.key
            modules[seller.key] = _load(ROOT / "sellers" / "run.py", f"_test_seller_{seller.key}")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return modules


@contextmanager
def agent_network(
    monkeypatch: Any, env: dict[str, str], modules: dict[str, ModuleType]
) -> Iterator[AgentNetwork]:
    """Give the already-loaded runtimes a fresh shared store and a controllable clock."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    network = AgentNetwork()
    real_now = clock.utc_now

    def shifted_now() -> datetime:
        return real_now() + network.offset

    monkeypatch.setattr(clock, "utc_now", shifted_now)
    for name, module in list(sys.modules.items()):
        if name.startswith(("resourcepilot.", "sellers.")) and hasattr(module, "utc_now"):
            monkeypatch.setattr(module, "utc_now", shifted_now)
    for key, module in modules.items():
        monkeypatch.setattr(module, "store", network.store)
        monkeypatch.setattr(module, "READY", True)
        monkeypatch.setattr(module, "utc_now", shifted_now)
        network.by_key[key] = module
        network.modules[str(module.agent.address)] = module
    yield network
