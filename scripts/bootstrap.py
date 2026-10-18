#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dotenv import dotenv_values
from uagents import Agent

from resourcepilot.config import ALL_AGENTS, AgentIdentity

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / ".env.example"
ENV_FILE = ROOT / ".env"
MANIFEST = ROOT / ".profiles.json"


def derive_address(name: str, seed: str) -> str:
    probe = Agent(name=f"bootstrap-{name}", seed=seed)
    return str(probe.address)


def replace_value(lines: list[str], key: str, value: str) -> None:
    prefix = f"{key}="
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{key}={value}"
            return
    lines.append(f"{key}={value}")


def confirm(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


def assign(lines: list[str], identity: AgentIdentity) -> None:
    """Give one agent a fresh private seed and the address derived from it."""
    seed = secrets.token_urlsafe(48)
    replace_value(lines, identity.seed_env, seed)
    replace_value(lines, identity.address_env, derive_address(identity.key, seed))


def profile(identity: AgentIdentity, address: str, port: int) -> dict[str, Any]:
    inspector = (
        "https://agentverse.ai/inspect/?uri="
        f"{quote(f'http://127.0.0.1:{port}', safe='/')}&address={address}"
    )
    return {
        "key": identity.key,
        "name": identity.name,
        "address": address,
        "port": port,
        "inspector_url": inspector,
        "profile_url": f"https://agentverse.ai/agents/details/{address}/profile",
    }


def missing_identities(values: dict[str, str | None]) -> list[AgentIdentity]:
    return [identity for identity in ALL_AGENTS if not (values.get(identity.seed_env) or "")]


def _free_ports(lines: list[str], added: list[AgentIdentity]) -> None:
    """Move a newly added agent off a port that an existing agent in .env already uses.

    Older layouts used different ports (Memory and Audit was once 8007), so a template default
    can collide with a kept identity.
    """
    values = {
        key: value for key, _, value in (line.partition("=") for line in lines if "=" in line)
    }
    new = {identity.key for identity in added}
    taken = {
        int(values.get(identity.port_env) or identity.port)
        for identity in ALL_AGENTS
        if identity.key not in new
    }
    for identity in added:
        port = int(values.get(identity.port_env) or identity.port)
        while port in taken:
            port += 1
        taken.add(port)
        replace_value(lines, identity.port_env, str(port))


def add_missing() -> list[AgentIdentity] | None:
    """Append identities for agents added since .env was made. Existing seeds never change."""
    missing = missing_identities(dotenv_values(ENV_FILE))
    if not missing:
        print("Every agent already has an identity. .env was not changed.")
        return []
    names = ", ".join(identity.name for identity in missing)
    if not confirm(f"Add identities for {len(missing)} new agents ({names})? Others stay as is."):
        print("Kept the existing .env. Nothing was changed.")
        return None
    lines = ENV_FILE.read_text().splitlines()
    present = {line.split("=", 1)[0] for line in lines if "=" in line}
    for line in TEMPLATE.read_text().splitlines():
        if "=" in line and not line.startswith("#") and line.split("=", 1)[0] not in present:
            lines.append(line)
    for identity in missing:
        assign(lines, identity)
    _free_ports(lines, missing)
    ENV_FILE.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_FILE, 0o600)
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description="Create local secrets and uAgent identities")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing .env (explicit opt-in; identities will change)",
    )
    parser.add_argument(
        "--add-missing",
        action="store_true",
        help="Keep every existing identity and only add agents that have none yet",
    )
    args = parser.parse_args()
    if ENV_FILE.exists() and args.add_missing:
        added = add_missing()
        if added is None:
            return 0
        if added:
            print(f"\nAdded {len(added)} identities (seeds are not displayed).")
    else:
        if ENV_FILE.exists() and not args.force:
            if missing_identities(dotenv_values(ENV_FILE)):
                print("Your .env predates some agents; `make bootstrap-missing` adds only those.")
            if not confirm(".env already exists. Replace it and generate new identities?"):
                print("Kept the existing .env. Nothing was overwritten.")
                return 0
        lines = TEMPLATE.read_text().splitlines()
        for identity in ALL_AGENTS:
            assign(lines, identity)
        ENV_FILE.write_text("\n".join(lines) + "\n")
        os.chmod(ENV_FILE, 0o600)
        print(f"\nCreated .env with {len(ALL_AGENTS)} private random seeds (seeds not displayed).")

    values = dotenv_values(ENV_FILE)
    profiles = [
        profile(
            identity,
            str(values.get(identity.address_env) or ""),
            int(values.get(identity.port_env) or identity.port),
        )
        for identity in ALL_AGENTS
    ]
    MANIFEST.write_text(json.dumps({"agents": profiles}, indent=2) + "\n")
    print("Keep .env stable and private: changing a seed changes that agent's identity.\n")
    for item in profiles:
        print(f"{item['name']}")
        print(f"  Address:   {item['address']}")
        print(f"  Inspector: {item['inspector_url']}")
        print(f"  Profile:   {item['profile_url']}")
    print(
        "\nAfter `make start`, open EVERY Inspector link, click Connect, and choose Mailbox. "
        "Those manual Agentverse steps cannot be automated safely."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
