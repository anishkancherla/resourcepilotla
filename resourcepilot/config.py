from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final, Literal

AgentKind = Literal["platform", "seller"]


@dataclass(frozen=True)
class AgentIdentity:
    """One independently addressed uAgent: its seed, address, and port come from the env."""

    key: str
    name: str
    responsibility: str
    port: int
    kind: AgentKind = "platform"

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
    def wallet_env(self) -> str:
        return f"{self.env_prefix}_WALLET"

    @property
    def compose_service(self) -> str:
        return self.key.replace("_", "-")

    @property
    def readme_dir(self) -> str:
        return f"{'agents' if self.kind == 'platform' else 'sellers'}/{self.key}"

    def configured_port(self) -> int:
        return int(os.getenv(self.port_env, str(self.port)))

    def seed(self) -> str:
        value = os.getenv(self.seed_env, "").strip()
        if not value:
            raise RuntimeError(f"{self.seed_env} is missing; run `make bootstrap` first")
        return value


PLATFORM_ROLES: Final[tuple[AgentIdentity, ...]] = (
    AgentIdentity(
        "orchestrator",
        "ResourcePilot Orchestrator",
        "Runs the ASI:One chat, the workflow, and escrow payment",
        8000,
    ),
    AgentIdentity(
        "intake",
        "ResourcePilot Intake",
        "Turns a sentence or a form into a validated booking brief",
        8001,
    ),
    AgentIdentity(
        "discovery",
        "ResourcePilot Seller Discovery",
        "Finds seller agents and collects live quotes from them",
        8002,
    ),
    AgentIdentity(
        "verification",
        "ResourcePilot Seller Verification",
        "Vets seller identity, protocol conformance, track record, and policy",
        8003,
    ),
    AgentIdentity(
        "analysis",
        "ResourcePilot Logistics Analysis",
        "Checks time windows, lead times, and walking distance",
        8004,
    ),
    AgentIdentity(
        "planner",
        "ResourcePilot Bundle Planner",
        "Builds budget-safe bundles with backups",
        8005,
    ),
    AgentIdentity(
        "fulfillment",
        "ResourcePilot Checkout Coordinator",
        "Places holds and commits bookings all-or-nothing",
        8006,
    ),
    AgentIdentity(
        "memory_audit",
        "ResourcePilot Memory and Audit",
        "Keeps the audit timeline, preferences, and seller track records",
        8007,
    ),
)

SELLER_AGENTS: Final[tuple[AgentIdentity, ...]] = (
    AgentIdentity(
        "westwood_print",
        "Westwood Print Lab (demo seller)",
        "Sells same-day poster printing",
        8100,
        "seller",
    ),
    AgentIdentity(
        "gayley_copy",
        "Gayley Copy Center (demo seller)",
        "Sells poster printing with longer hours",
        8101,
        "seller",
    ),
    AgentIdentity(
        "flash_prints",
        "Flash Prints 4U (demo seller, unverified)",
        "Sells very cheap printing without a verified identity",
        8102,
        "seller",
    ),
    AgentIdentity(
        "gear_share",
        "Campus Gear Share (demo seller)",
        "Rents cameras, microphones, and tripods by the hour",
        8103,
        "seller",
    ),
    AgentIdentity(
        "studio_rooms",
        "Hilltop Studio Rooms (demo seller)",
        "Rents podcast studios and study rooms by the hour",
        8104,
        "seller",
    ),
)

ALL_AGENTS: Final = PLATFORM_ROLES + SELLER_AGENTS
ROLE_BY_KEY: Final = {role.key: role for role in PLATFORM_ROLES}
SELLER_BY_KEY: Final = {seller.key: seller for seller in SELLER_AGENTS}

# The sequential planning pipeline. Discovery fans out to sellers inside its own step.
SPECIALIST_CHAIN: Final = ("intake", "discovery", "verification", "analysis", "planner")


def _normalize(raw: str) -> str:
    return raw.strip().lower().replace("-", "_")


def get_role(key: str | None = None) -> AgentIdentity:
    selected = _normalize(key if key is not None else (os.getenv("AGENT_ROLE") or ""))
    try:
        return ROLE_BY_KEY[selected]
    except KeyError as exc:
        raise RuntimeError(f"AGENT_ROLE must be one of: {', '.join(ROLE_BY_KEY)}") from exc


def get_seller(key: str | None = None) -> AgentIdentity:
    selected = _normalize(key if key is not None else (os.getenv("SELLER_ID") or ""))
    try:
        return SELLER_BY_KEY[selected]
    except KeyError as exc:
        raise RuntimeError(f"SELLER_ID must be one of: {', '.join(SELLER_BY_KEY)}") from exc


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
