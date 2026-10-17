#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
manifest = ROOT / ".profiles.json"

if not manifest.exists():
    raise SystemExit("Run `make bootstrap` first.")

for kind, title in (("platform", "Platform agents"), ("seller", "Demo seller agents")):
    print(f"\n{title}")
    for profile in json.loads(manifest.read_text())["agents"]:
        if profile.get("kind", "platform") != kind:
            continue
        print(f"{profile['name']} ({profile['address']})")
        print(f"  Inspector: {profile['inspector_url']}")
        print(f"  Profile:   {profile['profile_url']}")
