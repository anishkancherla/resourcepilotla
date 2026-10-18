#!/usr/bin/env python3
"""Print every ASI:One card payload ResourcePilot sends, validated exactly as the agents send them.

    uv run python scripts/print_cards.py                 # menu and forms only, no network
    uv run python scripts/print_cards.py purdue.edu      # plus live Researcher and Campus Rooms

With school domains it runs the real Campus Researcher logic and the Campus Rooms quote against
public pages (IPEDS, the school's site, LibCal, the school's Localist events feed). It needs no
keys and never books anything.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from typing import Any

from resourcepilot import cards
from resourcepilot.bundles import plan_room_options
from resourcepilot.clock import utc_now
from resourcepilot.http import Http, ProviderError, build_http
from resourcepilot.intake import housing_brief, room_brief
from resourcepilot.protocols import QuoteRequest
from resourcepilot.registry import LISTING_BY_ID
from resourcepilot.research import research
from resourcepilot.store import MemoryStore
from sellers.live_rooms import quote_rooms


def show(title: str, card: tuple[str, dict[str, Any]]) -> None:
    kind, payload = card
    message = cards.card_message(title, kind, payload)  # raises on any undocumented field
    size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    print(f"\n=== {title} ({kind}, {size} bytes, {len(message.content)} content parts)")
    print(json.dumps(payload, indent=2, ensure_ascii=False))


async def live(domain: str, http: Http) -> None:
    try:
        housing = await research(
            {"brief": housing_brief({"school_domain": domain})}, http, now=utc_now()
        )
        show(f"Housing at {domain}", cards.housing_card(housing))
        day = (utc_now() + timedelta(days=1)).date().isoformat()
        fields = {"school_domain": domain, "day": day, "window": "afternoon", "duration": "60"}
        found = await research({"brief": room_brief(fields)}, http, now=utc_now())
        if found.get("needs_link"):
            school = found["school"]["school_name"]
            show(f"Room link needed at {domain}", cards.room_link_card("demo", school, "rooms"))
            return
        brief = found["brief"]
        request = QuoteRequest(
            round_id="demo",
            workflow_id="demo",
            categories=["study_room"],
            quantities={"study_room": 1},
            window_start=brief["window_start"],
            window_end=brief["window_end"],
            campus=found["campus"],
        )
        listing = LISTING_BY_ID["campus_rooms"]
        offers = [
            {**offer, "seller_name": listing.display_name}
            for offer in await quote_rooms(build_http(follow_redirects=False), listing, request)
            if offer["available"]
        ]
        if not offers:
            print(f"\n=== No open room at {domain} tomorrow afternoon")
            return
        options = plan_room_options(brief, offers)
        school = found["school"]["school_name"]
        show(f"Open rooms at {domain}", cards.rooms_carousel("demo", options, school))
        show(
            f"Room detail at {domain}",
            cards.bundle_detail("demo", brief, options[0], checkout=True),
        )
    except (ProviderError, ValueError) as exc:
        print(f"\n=== {domain}: {exc}")
    await live_wellness(domain, http)


async def live_wellness(domain: str, http: Http) -> None:
    """The wellness card with live rooms and events. Verification's re-checks are not run here."""
    try:
        day = (utc_now() + timedelta(days=1)).date().isoformat()
        fields = {"school_domain": domain, "day": day, "window": "afternoon", "duration": "30"}
        found = await research(
            {"brief": room_brief(fields, service="wellness")}, http, now=utc_now()
        )
        options: list[dict[str, Any]] = []
        if found.get("campus"):
            brief = found["brief"]
            request = QuoteRequest(
                round_id="demo",
                workflow_id="demo",
                categories=["wellness_room"],
                quantities={"wellness_room": 1},
                window_start=brief["window_start"],
                window_end=brief["window_end"],
                campus=found["campus"],
            )
            listing = LISTING_BY_ID["campus_rooms"]
            offers = await quote_rooms(build_http(follow_redirects=False), listing, request)
            usable = [{**o, "seller_name": listing.display_name} for o in offers if o["available"]]
            found["room_note"] = next(
                (o["unavailable_reason"] for o in offers if not o["available"]), ""
            )
            options = plan_room_options(brief, usable)
        show(f"Wellness at {domain}", cards.wellness_card("demo", {**found, "bundles": options}))
    except (ProviderError, ValueError) as exc:
        print(f"\n=== {domain} wellness: {exc}")


async def main(domains: list[str]) -> None:
    show("Campus services menu", cards.welcome_card())
    for service in ("rooms", "wellness", "housing", "bundle"):
        show(f"{service} form", cards.service_form(service))
    show("Wellness card, nothing found", cards.wellness_card("demo", {}))
    show("Paste-link fallback", cards.room_link_card("demo", "Example University", "bundle"))
    http = build_http(MemoryStore())  # one cache, so IPEDS is fetched once for every domain
    try:
        for domain in domains:
            await live(domain, http)
    finally:
        await http.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
