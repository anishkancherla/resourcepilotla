#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from resourcepilot.config import ALL_AGENTS

ROOT = Path(__file__).resolve().parents[1]
manifest = ROOT / ".profiles.json"

if not manifest.exists():
    raise SystemExit("Run `make bootstrap` first.")

# Older manifests may also list agents that no longer run; show only the current seven.
profiles = {profile.get("key"): profile for profile in json.loads(manifest.read_text())["agents"]}
print(f"ResourcePilot agents ({len(ALL_AGENTS)})")
for identity in ALL_AGENTS:
    profile = profiles.get(identity.key)
    if profile is None:
        continue
    print(f"{identity.name} ({profile['address']})")
    print(f"  Inspector: {profile['inspector_url']}")
    print(f"  Profile:   {profile['profile_url']}")

stale = [identity.name for identity in ALL_AGENTS if identity.key not in profiles]
if stale:
    print(
        f"\n.profiles.json has no entry for {', '.join(stale)}. "
        "Run `make bootstrap-missing` to refresh it; existing identities stay as they are."
    )
