#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path
from urllib.parse import quote

from uagents import Agent

from resourcepilot.config import ALL_AGENTS

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / ".env.example"
ENV_FILE = ROOT / ".env"
MANIFEST = ROOT / ".profiles.json"


def derive_identity(name: str, seed: str) -> tuple[str, str]:
    probe = Agent(name=f"bootstrap-{name}", seed=seed)
    return str(probe.address), str(probe.wallet.address())


def replace_value(lines: list[str], key: str, value: str) -> None:
    prefix = f"{key}="
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{key}={value}"
            return
    lines.append(f"{key}={value}")


def confirm_overwrite() -> bool:
    response = input(".env already exists. Replace it and generate new identities? [y/N] ")
    return response.strip().lower() in {"y", "yes"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Create local secrets and uAgent identities")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing .env (explicit opt-in; identities will change)",
    )
    args = parser.parse_args()
    if ENV_FILE.exists() and not args.force and not confirm_overwrite():
        print("Kept the existing .env. Nothing was overwritten.")
        return 0

    lines = TEMPLATE.read_text().splitlines()
    profiles = []
    for identity in ALL_AGENTS:
        seed = secrets.token_urlsafe(48)
        address, wallet = derive_identity(identity.key, seed)
        replace_value(lines, identity.seed_env, seed)
        replace_value(lines, identity.address_env, address)
        if identity.kind == "seller":
            replace_value(lines, identity.wallet_env, wallet)
        inspector = (
            "https://agentverse.ai/inspect/?uri="
            f"{quote(f'http://127.0.0.1:{identity.port}', safe='/')}&address={address}"
        )
        profiles.append(
            {
                "key": identity.key,
                "kind": identity.kind,
                "name": identity.name,
                "address": address,
                "port": identity.port,
                "inspector_url": inspector,
                "profile_url": f"https://agentverse.ai/agents/details/{address}/profile",
            }
        )

    ENV_FILE.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_FILE, 0o600)
    MANIFEST.write_text(json.dumps({"agents": profiles}, indent=2) + "\n")

    print(f"\nCreated .env with {len(profiles)} private random seeds (seeds are not displayed).")
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
