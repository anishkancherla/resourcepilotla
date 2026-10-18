from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class AgentIdentity:
    """One independently addressed uAgent: its seed, address, and port come from the env."""

    key: str
    name: str
    responsibility: str
    port: int

    @property
    def env_prefix(self) -> str:
        return self.key.upper()

    @property
    def seed_env(self) -> str:
        return f"{self.env_prefix}_SEED"

    @property
    def address_env(self) -> str:
        return f"{self.env_prefix}_ADDRESS"

    @property
    def port_env(self) -> str:
        return f"{self.env_prefix}_PORT"

    @property
    def compose_service(self) -> str:
        return self.key.replace("_", "-")

    @property
    def readme_dir(self) -> str:
        return f"agents/{self.key}"

    def configured_port(self) -> int:
        return int(os.getenv(self.port_env, str(self.port)))

    def seed(self) -> str:
        value = os.getenv(self.seed_env, "").strip()
        if not value:
            raise RuntimeError(f"{self.seed_env} is missing; run `make bootstrap` first")
        return value


# Seven real uAgents, each its own process run by agents/run.py with AGENT_ROLE=<key>.
# Seeds and addresses are keyed by `key`, so identities in an existing .env stay valid.
PLATFORM_ROLES: Final[tuple[AgentIdentity, ...]] = (
    AgentIdentity(
        "orchestrator",
        "ResourcePilot Orchestrator",
        "Runs the ASI:One chat, the services menu, cards, and the workflow",
        8000,
    ),
    AgentIdentity(
        "intake",
        "ResourcePilot Intake",
        "Turns a submitted form into a validated brief, keeping only the school domain",
        8001,
    ),
    AgentIdentity(
        "researcher",
        "ResourcePilot Campus Researcher",
        "Finds a school's room-booking system and housing pages from its email domain",
        8002,
    ),
    AgentIdentity(
        "campus_rooms",
        "ResourcePilot Campus Rooms",
        "Reads open library rooms live and re-checks one before handing over the booking link",
        8003,
    ),
    AgentIdentity(
        "verification",
        "ResourcePilot Verification",
        "Checks every room offer and re-opens official housing links before you see them",
        8004,
    ),
    AgentIdentity(
        "planner",
        "ResourcePilot Planner",
        "Ranks open rooms and reroutes when a room is taken",
        8005,
    ),
    AgentIdentity(
        "memory_audit",
        "ResourcePilot Memory and Audit",
        "Keeps the append-only audit timeline and your recent bookings",
        8006,
    ),
)

ALL_AGENTS: Final = PLATFORM_ROLES
ROLE_BY_KEY: Final = {role.key: role for role in PLATFORM_ROLES}

# The sequential pipelines, one per campus service.
SERVICES: Final = ("rooms", "housing")
SERVICE_CHAINS: Final[dict[str, tuple[str, ...]]] = {
    "rooms": ("intake", "researcher", "campus_rooms", "verification", "planner"),
    "housing": ("intake", "researcher", "verification"),
}
SPECIALISTS: Final = frozenset(role for chain in SERVICE_CHAINS.values() for role in chain)


def service_chain(service: str, payload: dict[str, object] | None = None) -> tuple[str, ...]:
    """Return the specialists a service visits, in order. Unknown services get the rooms chain."""
    return SERVICE_CHAINS.get(service, SERVICE_CHAINS["rooms"])


def _normalize(raw: str) -> str:
    return raw.strip().lower().replace("-", "_")


def get_role(key: str | None = None) -> AgentIdentity:
    selected = _normalize(key if key is not None else (os.getenv("AGENT_ROLE") or ""))
    try:
        return ROLE_BY_KEY[selected]
    except KeyError as exc:
        raise RuntimeError(f"AGENT_ROLE must be one of: {', '.join(ROLE_BY_KEY)}") from exc


def agent_addresses(*, require_all: bool = True) -> dict[str, str]:
    result: dict[str, str] = {}
    for identity in ALL_AGENTS:
        address = os.getenv(identity.address_env, "").strip()
        if address:
            result[identity.key] = address
        elif require_all:
            raise RuntimeError(f"{identity.address_env} is missing; rerun `make bootstrap`")
    return result


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    value = int(raw) if raw else default
    return max(minimum, value) if minimum is not None else value
