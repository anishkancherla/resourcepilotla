from __future__ import annotations

from typing import Any

import pytest
from provider_fakes import campus

from resourcepilot.rooms import PAGE_ITEM
from resourcepilot.vetting import verification_handler, vet_room_offers

START = "2026-10-19T12:00:00-04:00"
END = "2026-10-19T17:00:00-04:00"
BRIEF = {"service": "rooms", "group_size": 3, "window_start": START, "window_end": END}
CAMPUS = campus()


def room(**fields: Any) -> dict[str, Any]:
    return {
        "item_id": "libcal-9-7-101",
        "name": "HSSE 2101",
        "capacity": 6,
        "slot_start": "2026-10-19T13:00:00-04:00",
        "slot_end": "2026-10-19T14:00:00-04:00",
        "slot_label": "Mon Oct 19 \u00b7 1:00pm\u20132:00pm",
        "location_label": "HSSE Library",
        "handoff_url": "https://calendar.lib.purdue.edu/space/101",
        "live_times": True,
        "source_label": "LibCal",
        "school_name": "Purdue University-Main Campus",
        "available": True,
        "unavailable_reason": "",
        "policy": "Free.",
        **fields,
    }


def page(**fields: Any) -> dict[str, Any]:
    return room(
        item_id=PAGE_ITEM,
        name="Book on Purdue's LibCal",
        capacity=0,
        slot_start="",
        slot_end="",
        slot_label="",
        handoff_url="https://calendar.lib.purdue.edu/allspaces",
        live_times=False,
        **fields,
    )


def test_trusted_offers_pass_through_unchanged() -> None:
    offers = [room(), page(), room(capacity=0, item_id="libcal-9-7-102", name="Unlisted")]
    vendor = room(item_id="libcal-1-1-1", handoff_url="https://purdue.libcal.com/space/1")
    accepted, rejections = vet_room_offers(BRIEF, [*offers, vendor], CAMPUS)
    assert accepted == [*offers, vendor]
    assert rejections == []
    edges = room(slot_start=START, slot_end=END)
    assert vet_room_offers(BRIEF, [edges], CAMPUS) == ([edges], [])


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ({"available": False, "unavailable_reason": "No room for 3 is open"}, "No room for 3"),
        ({"available": False, "unavailable_reason": ""}, "Not available"),
        ({"name": ""}, "missing a room name or id"),
        ({"item_id": ""}, "missing a room name or id"),
        ({"handoff_url": "https://evil.example.com/space/101"}, "not on the school's site"),
        ({"handoff_url": "http://calendar.lib.purdue.edu/space/101"}, "not on the school's site"),
        ({"handoff_url": ""}, "not on the school's site"),
        ({"slot_start": "2026-10-19T11:30:00-04:00"}, "outside the window"),
        ({"slot_end": "2026-10-19T17:30:00-04:00"}, "outside the window"),
        ({"slot_start": "2026-10-19T14:00:00-04:00"}, "outside the window"),
        ({"slot_start": "", "slot_end": ""}, "outside the window"),
        ({"capacity": 2}, "Seats 2, fewer than your group of 3"),
    ],
)
def test_each_rejection_reason(tamper: dict[str, Any], reason: str) -> None:
    bad = room(**tamper)
    accepted, rejections = vet_room_offers(BRIEF, [bad, page()], CAMPUS)
    assert accepted == [page()]
    assert len(rejections) == 1
    assert set(rejections[0]) == {"item", "reason"}
    assert reason in rejections[0]["reason"]
    assert rejections[0]["item"] == (bad["name"] or bad["item_id"])


def test_links_are_checked_against_the_school_domain() -> None:
    elsewhere = campus(domain="mit.edu")
    _, rejections = vet_room_offers(BRIEF, [room()], elsewhere)
    assert [r["reason"] for r in rejections] == [
        "Booking link is not on the school's site or a known booking system"
    ]


def test_handler_keeps_the_payload_and_reports_rejections() -> None:
    payload = {
        "brief": BRIEF,
        "campus": CAMPUS,
        "school": {"domain": "purdue.edu"},
        "offers": [room(), room(name="Tiny", capacity=1)],
    }
    result = verification_handler(payload, {})
    assert result["school"] == payload["school"]
    assert result["campus"] == CAMPUS
    assert result["offers"] == [room()]
    assert result["rejections"] == [
        {"item": "Tiny", "reason": "Seats 1, fewer than your group of 3"}
    ]


def test_handler_raises_the_first_reason_when_nothing_is_left() -> None:
    unavailable = page(available=False, unavailable_reason="No room for 3 at Purdue is open")
    payload = {"brief": BRIEF, "campus": CAMPUS, "offers": [unavailable, room(capacity=1)]}
    with pytest.raises(ValueError, match="No room for 3 at Purdue is open"):
        verification_handler(payload, {})
    with pytest.raises(ValueError, match="No open room is left at your school for that time"):
        verification_handler({"brief": BRIEF, "campus": CAMPUS, "offers": []}, {})
