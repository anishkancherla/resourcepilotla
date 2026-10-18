#!/usr/bin/env python3
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

from resourcepilot.config import ALL_AGENTS

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


def verify(values: Mapping[str, str | None], root: Path = ROOT) -> str:
    """Check every agent identity and return a summary; raise SystemExit on the first problem.

    Seeds are compared, never displayed. Settings left over from agents that ResourcePilot no
    longer runs are ignored, so an older .env keeps working unchanged.
    """
    count = len(ALL_AGENTS)
    missing = [agent.name for agent in ALL_AGENTS if not values.get(agent.seed_env)]
    if missing:
        raise SystemExit(
            f"No identity yet for {', '.join(missing)}. Run `make bootstrap-missing`; "
            "it keeps every existing identity."
        )
    seeds = [str(values.get(agent.seed_env) or "") for agent in ALL_AGENTS]
    addresses = [str(values.get(agent.address_env) or "") for agent in ALL_AGENTS]
    ports = [str(values.get(agent.port_env) or agent.port) for agent in ALL_AGENTS]

    if any(len(seed) < 32 for seed in seeds) or len(set(seeds)) != count:
        raise SystemExit("Seeds must be present, strong, and unique.")
    if (
        any(not address.startswith("agent1") for address in addresses)
        or len(set(addresses)) != count
    ):
        raise SystemExit("Addresses must be present and unique.")
    if len(set(ports)) != count:
        raise SystemExit("Ports must be unique.")
    for agent in ALL_AGENTS:
        if not (root / agent.readme_dir / "README.md").exists():
            raise SystemExit(f"Missing profile README for {agent.key}")

    summary = (
        f"Configuration verified: {count} unique seeds, addresses, ports, and profile READMEs."
    )
    current = {agent.seed_env for agent in ALL_AGENTS}
    leftover = sorted(key for key in values if key.endswith("_SEED") and key not in current)
    if leftover:
        summary += (
            "\nIgnored leftover settings for agents that no longer run "
            f"({', '.join(leftover)}); you may delete them from .env."
        )
    return summary


def main() -> int:
    if not ENV_FILE.exists():
        raise SystemExit("Missing .env. Run `make bootstrap`.")
    print(verify(dotenv_values(ENV_FILE)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
