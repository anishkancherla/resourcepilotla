#!/usr/bin/env python3
"""Print every ASI:One card payload ResourcePilot sends, validated exactly as the agents send them.

    uv run python scripts/print_cards.py                 # every card from sample data, no network
    uv run python scripts/print_cards.py purdue.edu      # plus live Researcher and Campus Rooms

With school domains it runs the real Campus Researcher logic and the Campus Rooms search against
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
from resourcepilot.clock import utc_now
from resourcepilot.http import Http, ProviderError, build_http
from resourcepilot.intake import housing_brief, room_brief
from resourcepilot.research import research
from resourcepilot.store import MemoryStore

SAMPLE_LINE: dict[str, Any] = {
    "item_id": "libcal-1-2-3",
    "name": "Example Library 210",
    "capacity": 6,
    "slot_start": "2026-10-22T18:00:00+00:00",
    "slot_end": "2026-10-22T19:00:00+00:00",
    "slot_label": "Thu Oct 22 · 2:00 PM\u20133:00 PM",
    "location_label": "Example Library",
    "handoff_url": "https://libcal.example.edu/space/3",
    "live_times": True,
    "source_label": "LibCal",
    "school_name": "Example University",
    "available": True,
    "unavailable_reason": "",
    "policy": (
        "Free. ResourcePilot re-checks this room but cannot reserve it: book it on "
        "Example University's site with your own sign-in."
    ),
}
SAMPLE_OPTIONS: list[dict[str, Any]] = [
    {"option_id": "opt_sample0001", "label": "Earliest opening", "lines": [SAMPLE_LINE]},
    {
        "option_id": "opt_sample0002",
        "label": "Booking page",
        "lines": [
            {
                **SAMPLE_LINE,
                "item_id": "booking-page",
                "name": "Book on Example University's LibCal",
                "capacity": 0,
                "slot_label": "",
                "location_label": "Availability isn't public",
                "handoff_url": "https://libcal.example.edu/reserve",
                "live_times": False,
            }
        ],
    },
]
SAMPLE_HOUSING: dict[str, Any] = {
    "school": {"school_name": "Example University", "city": "Springfield", "state": "IL"},
    "housing": {
        "facts": [("On-campus housing", "Yes"), ("Dorm beds", "4,200")],
        "facts_source": "IPEDS 2022 via Urban Institute Education Data API",
        "office_url": "https://housing.example.edu/",
        "portal_url": "https://housing.example.edu/apply",
        "portal_label": "Apply for housing",
    },
}

# Sample emergency drill data: a masked number only, never a real one.
SAMPLE_PREVIEW: dict[str, Any] = {
    "to": "\u2022\u2022\u2022\u2022 0142",
    "opening": "Hello, this is a ResourcePilot demo drill, not a real emergency.",
    "rows": [("Calling", "Your demo contact (\u2022\u2022\u2022\u2022 0142), not 911")],
}
SAMPLE_CALL: dict[str, Any] = {
    "to": "\u2022\u2022\u2022\u2022 0142",
    "conversation_id": "conv_demo",
}
SAMPLE_CALL_STATUS: dict[str, Any] = {
    "status_label": "Call ended",
    "duration": "1:15",
    "summary": "Reported a fainting at the library.",
    "transcript": [{"speaker": "Dispatcher", "message": "What is your location?"}],
}


def show(title: str, card: tuple[str, dict[str, Any]], *, terminal: bool = False) -> None:
    kind, payload = card
    # Raises on any undocumented field, an oversize payload, or a custom tree deeper than eight.
    message = cards.card_message(title, kind, payload, terminal=terminal)
    size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    print(f"\n=== {title} ({kind}, {size} bytes, {len(message.content)} content parts)")
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def samples() -> None:
    show("Campus services menu", cards.welcome_card())
    for service in ("rooms", "wellness", "housing", "emergency"):
        show(f"{service} form", cards.service_form(service))
    show("Wellness card, nothing found", cards.wellness_card("demo", {}))
    show("Paste-link fallback", cards.room_link_card("demo", "Example University"))
    chain = ("intake", "researcher", "campus_rooms", "verification", "planner")
    show("Progress", cards.progress_card("demo", "campus_rooms", chain))
    show("Sample open rooms", cards.rooms_carousel("demo", SAMPLE_OPTIONS, "Example University"))
    show("Sample room detail", cards.room_detail("demo", SAMPLE_OPTIONS[0]))
    show("Sample booking link", cards.room_link_receipt(SAMPLE_OPTIONS[0]), terminal=True)
    show("Sample housing", cards.housing_card(SAMPLE_HOUSING))
    show("No bookings yet", cards.bookings_card([]))
    show("Demo call review", cards.emergency_review_card("demo", SAMPLE_PREVIEW))
    show("Demo call placed", cards.emergency_call_card("demo", SAMPLE_CALL))
    show("Demo call checked", cards.emergency_call_card("demo", SAMPLE_CALL, SAMPLE_CALL_STATUS))


async def live_rooms(domain: str, found: dict[str, Any], rooms_http: Http) -> None:
    # Imported here so the sample cards print without the live Campus Rooms code path.
    from resourcepilot.planning import plan_room_options
    from resourcepilot.rooms import find_rooms

    brief = found["brief"]
    offers = [
        offer
        for offer in await find_rooms(
            rooms_http, found["campus"], brief["window_start"], brief["window_end"]
        )
        if offer["available"]
    ]
    if not offers:
        print(f"\n=== No open room at {domain} tomorrow afternoon")
        return
    options = plan_room_options(brief, offers)
    school = found["school"]["school_name"]
    show(f"Open rooms at {domain}", cards.rooms_carousel("demo", options, school))
    show(f"Room detail at {domain}", cards.room_detail("demo", options[0]))
    show(f"Booking link at {domain}", cards.room_link_receipt(options[0]), terminal=True)


async def live_wellness(domain: str, http: Http, rooms_http: Http) -> None:
    """The wellness card with live rooms and events. Verification's re-checks are not run here."""
    from resourcepilot.planning import plan_room_options
    from resourcepilot.rooms import find_rooms
    from resourcepilot.vetting import verification_handler

    day = (utc_now() + timedelta(days=1)).date().isoformat()
    fields = {"school_domain": domain, "day": day, "window": "afternoon", "duration": "30"}
    found = await research({"brief": room_brief(fields, service="wellness")}, http, now=utc_now())
    if found.get("campus"):
        brief = found["brief"]
        offers = await find_rooms(
            rooms_http,
            found["campus"],
            brief["window_start"],
            brief["window_end"],
            wellness=True,
        )
        found = verification_handler({**found, "offers": offers}, {})
        found["options"] = plan_room_options(brief, found["offers"])
    show(f"Wellness at {domain}", cards.wellness_card("demo", found))


async def live(domain: str, http: Http, rooms_http: Http) -> None:
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
            show(f"Room link needed at {domain}", cards.room_link_card("demo", school))
        else:
            await live_rooms(domain, found, rooms_http)
    except (ProviderError, ValueError) as exc:
        print(f"\n=== {domain}: {exc}")
    try:
        await live_wellness(domain, http, rooms_http)
    except (ProviderError, ValueError) as exc:
        print(f"\n=== {domain} wellness: {exc}")


async def main(domains: list[str]) -> None:
    samples()
    if not domains:
        return
    cache = MemoryStore()  # one cache, so IPEDS is fetched once for every domain
    http = build_http(cache)
    rooms_http = build_http(cache, follow_redirects=False)  # as Campus Rooms runs it
    try:
        for domain in domains:
            await live(domain, http, rooms_http)
    finally:
        await http.close()
        await rooms_http.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
