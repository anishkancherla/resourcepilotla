from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from provider_fakes import SEARCH_DAY, campus, fake_http, libcal_grid, libcal_html

from resourcepilot.rooms import (
    LINE_KEYS,
    PAGE_ITEM,
    TAKEN,
    campus_rooms_handler,
    clock_label,
    find_rooms,
    first_opening,
    libcal_host,
    open_runs,
    parse_libcal_resources,
    recheck_room,
    study_spaces,
)

ZONE = ZoneInfo("America/Indiana/Indianapolis")


def at(hour: int, minute: int = 0) -> str:
    return (
        datetime.combine(SEARCH_DAY, datetime.min.time(), ZONE)
        .replace(hour=hour, minute=minute)
        .isoformat()
    )


def test_libcal_rooms_and_open_runs_parse_from_public_pages() -> None:
    rooms = parse_libcal_resources(libcal_html())
    assert [(room["eid"], room["name"], room["capacity"]) for room in rooms] == [
        (101, "HSSE 2101", 6),
        (102, "WALC 3138", 2),
        (103, "Bernette b64 Serger", 0),
    ]
    assert rooms[2]["grouping"] == "Knowledge Lab Specialty Textile Machines"
    assert [room["eid"] for room in study_spaces(rooms, 1)] == [101, 102]  # no serger
    assert [room["eid"] for room in study_spaces(rooms, 4)] == [101]
    slots = libcal_grid(SEARCH_DAY, {"101@12:00", "101@12:30"})["slots"]
    runs = open_runs(slots, 101, SEARCH_DAY)
    assert runs == [(8 * 60, 12 * 60), (13 * 60, 22 * 60)]
    assert first_opening(runs, (12 * 60, 17 * 60), 60) == 13 * 60
    assert first_opening(runs, (12 * 60, 13 * 60), 60) is None
    assert (clock_label(13 * 60), clock_label(9 * 60 + 30), clock_label(0)) == (
        "1:00pm",
        "9:30am",
        "12:00am",
    )


def test_libcal_host_is_the_school_domain_or_libcal_only() -> None:
    assert libcal_host(campus()) == "calendar.lib.purdue.edu"
    assert libcal_host(campus(libcal_host="purdue.libcal.com")) == "purdue.libcal.com"
    assert libcal_host(campus(libcal_host="evil.example.com")) == ""
    assert libcal_host(campus(system="page")) == ""


async def test_offers_are_free_live_links_that_fit_the_group() -> None:
    offers = await find_rooms(fake_http(), campus(group_size="4"), at(12), at(17))
    assert [offer["name"] for offer in offers] == ["HSSE 2101"]  # WALC 3138 seats only 2
    offer = offers[0]
    assert tuple(offer) == LINE_KEYS
    assert offer["item_id"] == "libcal-9-7-101"
    assert (offer["available"], offer["live_times"], offer["capacity"]) == (True, True, 6)
    assert offer["handoff_url"] == "https://calendar.lib.purdue.edu/space/101"
    assert (offer["slot_start"], offer["slot_end"]) == (at(12), at(13))
    assert offer["slot_label"].endswith("12:00pm\u20131:00pm")
    assert (offer["source_label"], offer["location_label"]) == ("LibCal", "HSSE Library")
    assert offer["school_name"] == "Purdue University-Main Campus"
    assert "cannot reserve it" in offer["policy"]
    assert not {"seller_id", "category", "subtotal_cents", "rating", "tags"} & set(offer)


async def test_booked_slots_push_the_offer_later() -> None:
    http = fake_http(booked={"101@12:00", "102@12:00", "102@12:30"})
    offers = await find_rooms(http, campus(), at(12), at(17))
    assert [(offer["name"], offer["slot_start"]) for offer in offers] == [
        ("HSSE 2101", at(12, 30)),
        ("WALC 3138", at(13)),
    ]


async def test_no_opening_is_reported_not_invented() -> None:
    booked = {f"{eid}@{hour:02d}:{minute:02d}" for eid in (101, 102)
              for hour in range(12, 17) for minute in (0, 30)}  # fmt: skip
    offers = await find_rooms(fake_http(booked=booked), campus(), at(12), at(17))
    assert len(offers) == 1
    assert offers[0]["item_id"] == PAGE_ITEM
    assert offers[0]["available"] is False
    assert "No room for 1" in offers[0]["unavailable_reason"]
    assert tuple(offers[0]) == LINE_KEYS


async def test_libcal_outage_falls_back_to_the_booking_link() -> None:
    http = fake_http(failing={"calendar.lib.purdue.edu"})
    offers = await find_rooms(http, campus(), at(12), at(17))
    assert [offer["item_id"] for offer in offers] == [PAGE_ITEM]
    assert offers[0]["live_times"] is False
    assert offers[0]["available"] is True
    assert offers[0]["handoff_url"] == "https://calendar.lib.purdue.edu/allspaces"
    assert offers[0]["slot_start"] == ""


async def test_non_libcal_schools_get_one_link_offer() -> None:
    calls: list[str] = []
    offers = await find_rooms(
        fake_http(calls=calls),
        campus(
            system="page",
            system_label="Booking page",
            libcal_host="",
            booking_url="https://lib.purdue.edu/rooms",
        ),
        at(12),
        at(17),
    )
    assert [offer["item_id"] for offer in offers] == [PAGE_ITEM]
    assert offers[0]["handoff_url"] == "https://lib.purdue.edu/rooms"
    assert offers[0]["location_label"] == "Availability isn't public"
    assert calls == []
    unsafe = campus(system="page", libcal_host="", booking_url="https://evil.example.com/rooms")
    assert await find_rooms(fake_http(), unsafe, at(12), at(17)) == []


async def test_hosts_off_the_school_domain_are_never_read() -> None:
    calls: list[str] = []
    http = fake_http(calls=calls)
    offers = await find_rooms(http, campus(libcal_host="evil.example.com"), at(12), at(17))
    assert all("evil.example.com" not in url for url in calls)
    assert [offer["item_id"] for offer in offers] == [PAGE_ITEM]
    assert await find_rooms(http, campus(domain="", libcal_host=""), at(12), at(17)) == []
    private = campus(libcal_host="intranet.purdue.edu")
    assert await find_rooms(http, private, at(12), at(17)) == []
    assert all("intranet.purdue.edu" not in url for url in calls)


async def test_recheck_reads_the_slot_live() -> None:
    assert await recheck_room(fake_http(), campus(), "libcal-9-7-101", at(14), at(15)) is None
    taken = await recheck_room(
        fake_http(booked={"102@14:30"}), campus(), "libcal-9-7-102", at(14), at(15)
    )
    assert taken == TAKEN
    assert "just booked" in taken


async def test_recheck_page_link_is_always_open_and_bad_input_is_refused() -> None:
    calls: list[str] = []
    http = fake_http(calls=calls)
    assert await recheck_room(http, campus(), PAGE_ITEM, "", "") is None
    assert calls == []
    assert await recheck_room(http, campus(), "not-a-room", at(14), at(15)) == "Unknown room"
    off_domain = campus(libcal_host="evil.example.com")
    assert await recheck_room(http, off_domain, "libcal-9-7-101", at(14), at(15)) == "Unknown room"
    no_school = campus(domain="")
    assert await recheck_room(http, no_school, "libcal-9-7-101", at(14), at(15))
    private = campus(libcal_host="intranet.purdue.edu")
    assert await recheck_room(http, private, "libcal-9-7-101", at(14), at(15))
    for start, end in ((at(14), at(14)), ("", "")):
        problem = await recheck_room(http, campus(), "libcal-9-7-101", start, end)
        assert problem == "Unknown time for this room"
    assert calls == []
    outage = fake_http(failing={"calendar.lib.purdue.edu"})
    assert "LibCal" in str(await recheck_room(outage, campus(), "libcal-9-7-101", at(14), at(15)))


async def test_handler_finds_rooms_for_the_brief() -> None:
    payload = {
        "brief": {"service": "rooms", "window_start": at(12), "window_end": at(17)},
        "campus": campus(),
        "school": {"domain": "purdue.edu"},
    }
    result = await campus_rooms_handler(payload, fake_http())
    assert result["school"] == payload["school"]
    # Both open at noon; the smaller room is listed first.
    assert [offer["name"] for offer in result["offers"]] == ["WALC 3138", "HSSE 2101"]


async def test_handler_rechecks_one_line() -> None:
    line = {"item_id": "libcal-9-7-102", "slot_start": at(14), "slot_end": at(15)}
    payload = {"phase": "recheck", "campus": campus(), "line": line}
    assert await campus_rooms_handler(payload, fake_http()) == {
        "open": True,
        "reason": "",
        "item_id": "libcal-9-7-102",
    }
    taken = await campus_rooms_handler(payload, fake_http(booked={"102@14:00"}))
    assert taken == {"open": False, "reason": TAKEN, "item_id": "libcal-9-7-102"}
    page = {**payload, "line": {"item_id": PAGE_ITEM, "slot_start": "", "slot_end": ""}}
    assert (await campus_rooms_handler(page, fake_http()))["open"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {"brief": {"window_start": "x", "window_end": "y"}},
        {"brief": {"window_start": "x", "window_end": "y"}, "campus": campus(domain="")},
        {"phase": "recheck", "line": {"item_id": PAGE_ITEM}, "campus": "purdue.edu"},
    ],
)
async def test_handler_refuses_a_missing_campus(payload: dict) -> None:
    with pytest.raises(ValueError, match="Missing school context for rooms"):
        await campus_rooms_handler(payload, fake_http())


async def test_handler_needs_a_window_and_a_line() -> None:
    with pytest.raises(ValueError, match="time window"):
        await campus_rooms_handler({"brief": {}, "campus": campus()}, fake_http())
    with pytest.raises(ValueError, match="room to re-check"):
        await campus_rooms_handler({"phase": "recheck", "campus": campus()}, fake_http())
