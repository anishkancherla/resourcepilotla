#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values

from resourcepilot.config import ALL_AGENTS, SELLER_AGENTS

ROOT = Path(__file__).resolve().parents[1]
env_path = ROOT / ".env"
if not env_path.exists():
    raise SystemExit("Missing .env. Run `make bootstrap`.")

values = dotenv_values(env_path)
count = len(ALL_AGENTS)
seeds = [str(values.get(agent.seed_env) or "") for agent in ALL_AGENTS]
addresses = [str(values.get(agent.address_env) or "") for agent in ALL_AGENTS]
ports = [str(values.get(agent.port_env) or "") for agent in ALL_AGENTS]
wallets = [str(values.get(seller.wallet_env) or "") for seller in SELLER_AGENTS]

if any(len(seed) < 32 for seed in seeds) or len(set(seeds)) != count:
    raise SystemExit("Seeds must be present, strong, and unique.")
if any(not address.startswith("agent1") for address in addresses) or len(set(addresses)) != count:
    raise SystemExit("Addresses must be present and unique.")
if len(set(ports)) != count:
    raise SystemExit("Ports must be unique.")
if any(not wallet.startswith("fetch1") for wallet in wallets) or len(set(wallets)) != len(wallets):
    raise SystemExit("Seller wallets must be present and unique; rerun `make bootstrap`.")
if str(values.get("FET_NETWORK") or "stable-testnet") != "stable-testnet":
    raise SystemExit("Only FET stable-testnet is supported.")
for agent in ALL_AGENTS:
    if not (ROOT / agent.readme_dir / "README.md").exists():
        raise SystemExit(f"Missing profile README for {agent.key}")

print(
    f"Configuration verified: {count} unique seeds, addresses, ports, and profile READMEs; "
    f"{len(wallets)} seller payout wallets."
)
