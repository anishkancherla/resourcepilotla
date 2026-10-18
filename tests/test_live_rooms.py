from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from provider_fakes import SEARCH_DAY, campus, fake_http

from resourcepilot.clock import utc_now
from resourcepilot.protocols import (
    BookingConfirmed,
    CommitHold,
    HoldConfirmed,
    HoldRejected,
    HoldRequest,
    QuoteRequest,
)
from resourcepilot.registry import LISTING_BY_ID
from resourcepilot.store import MemoryStore
from sellers.inventory import commit_hold, place_hold
from sellers.live_rooms import (
    PAGE_ITEM,
    commit_room,
    first_opening,
    hold_room,
    open_runs,
    parse_libcal_resources,
    quote_rooms,
    study_spaces,
    wellness_spaces,
)

ROOMS = LISTING_BY_ID["campus_rooms"]
ZONE = ZoneInfo("America/Indiana/Indianapolis")
NOW = utc_now().isoformat()


def at(hour: int, minute: int = 0) -> str:
    return (
        datetime.combine(SEARCH_DAY, datetime.min.time(), ZONE)
        .replace(hour=hour, minute=minute)
        .isoformat()
    )


def quote_request(start: str = at(12), end: str = at(17), **context: str) -> QuoteRequest:
    return QuoteRequest(
        round_id="round-1",
        workflow_id="wf-1",
        categories=["study_room"],
        quantities={"study_room": 1},
        window_start=start,
        window_end=end,
        campus=campus(**context),
    )


def wellness_request(**context: str) -> QuoteRequest:
    return quote_request(**context).copy(
        update={"categories": ["wellness_room"], "quantities": {"wellness_room": 1}}
    )


def hold_request(item_id: str, start: str, end: str, **context: str) -> HoldRequest:
    return HoldRequest(
        round_id="round-2",
        workflow_id="wf-1",
        hold_id="hold-1",
        item_id=item_id,
        quantity=1,
        window_start=start,
        window_end=end,
        campus=campus(**context),
    )


def test_libcal_rooms_and_open_runs_parse_from_public_pages() -> None:
    from provider_fakes import libcal_grid, libcal_html

    rooms = parse_libcal_resources(libcal_html())
    assert [(room["eid"], room["name"], room["capacity"]) for room in rooms] == [
        (101, "HSSE 2101", 6),
        (102, "WALC 3138", 2),
        (103, "Bernette b64 Serger", 0),
        (104, "Meditation Room", 1),
    ]
    assert rooms[2]["grouping"] == "Knowledge Lab Specialty Textile Machines"
    # A meditation room is never offered as a study room, and a wellness search offers nothing else.
    assert [room["eid"] for room in study_spaces(rooms, 1)] == [101, 102]
    assert [room["eid"] for room in wellness_spaces(rooms)] == [104]
    slots = libcal_grid(SEARCH_DAY, {"101@12:00", "101@12:30"})["slots"]
    runs = open_runs(slots, 101, SEARCH_DAY)
    assert runs == [(8 * 60, 12 * 60), (13 * 60, 22 * 60)]
    assert first_opening(runs, (12 * 60, 17 * 60), 60) == 13 * 60


async def test_quotes_are_free_live_handoffs_that_fit_the_group() -> None:
    offers = await quote_rooms(fake_http(), ROOMS, quote_request(group_size="4"))
    assert [offer["name"] for offer in offers] == ["HSSE 2101"]  # WALC 3138 seats only 2
    offer = offers[0]
    assert offer["item_id"] == "libcal-9-7-101"
    assert (offer["subtotal_cents"], offer["handoff"], offer["live_times"]) == (0, True, True)
    assert offer["handoff_url"] == "https://calendar.lib.purdue.edu/space/101"
    assert offer["slot_start"] == at(12)
    assert offer["slot_end"] == at(13)
    assert offer["rating"] is None


async def test_booked_slots_push_the_offer_later() -> None:
    http = fake_http(booked={"101@12:00", "102@12:00", "102@12:30"})
    offers = await quote_rooms(http, ROOMS, quote_request())
    assert [(offer["name"], offer["slot_start"]) for offer in offers] == [
        ("HSSE 2101", at(12, 30)),
        ("WALC 3138", at(13)),
    ]


async def test_no_opening_is_reported_not_invented() -> None:
    booked = {f"{eid}@{hour:02d}:{minute:02d}" for eid in (101, 102)
              for hour in range(12, 17) for minute in (0, 30)}  # fmt: skip
    offers = await quote_rooms(fake_http(booked=booked), ROOMS, quote_request())
    assert len(offers) == 1
    assert offers[0]["available"] is False
    assert "No room for 1" in offers[0]["unavailable_reason"]


async def test_libcal_outage_falls_back_to_the_booking_link() -> None:
    http = fake_http(failing={"calendar.lib.purdue.edu"})
    offers = await quote_rooms(http, ROOMS, quote_request())
    assert [offer["item_id"] for offer in offers] == [PAGE_ITEM]
    assert offers[0]["live_times"] is False
    assert offers[0]["handoff_url"] == "https://calendar.lib.purdue.edu/allspaces"


async def test_non_libcal_schools_get_one_link_offer() -> None:
    offers = await quote_rooms(
        fake_http(),
        ROOMS,
        quote_request(
            system="page",
            system_label="Booking page",
            libcal_host="",
            booking_url="https://lib.purdue.edu/rooms",
        ),
    )
    assert [offer["item_id"] for offer in offers] == [PAGE_ITEM]


async def test_hosts_off_the_school_domain_are_never_read() -> None:
    calls: list[str] = []
    http = fake_http(calls=calls)
    offers = await quote_rooms(http, ROOMS, quote_request(libcal_host="evil.example.com"))
    assert all("evil.example.com" not in url for url in calls)
    assert [offer["item_id"] for offer in offers] == [PAGE_ITEM]
    assert await quote_rooms(http, ROOMS, quote_request(domain="", libcal_host="")) == []


async def test_hold_rechecks_the_slot_live() -> None:
    store = MemoryStore()
    open_hold = await hold_room(
        fake_http(),
        store,
        ROOMS,
        hold_request("libcal-9-7-101", at(14), at(15)),
        ttl_seconds=60,
        now=NOW,
    )
    assert isinstance(open_hold, HoldConfirmed)
    assert open_hold.subtotal_cents == 0
    taken = await hold_room(
        fake_http(booked={"102@14:30"}),
        MemoryStore(),
        ROOMS,
        hold_request("libcal-9-7-102", at(14), at(15)),
        ttl_seconds=60,
        now=NOW,
    )
    assert isinstance(taken, HoldRejected)
    assert "just booked" in taken.reason


async def test_hold_without_school_context_is_rejected() -> None:
    result = await hold_room(
        fake_http(),
        MemoryStore(),
        ROOMS,
        hold_request("libcal-9-7-101", at(14), at(15), domain=""),
        ttl_seconds=60,
        now=NOW,
    )
    assert isinstance(result, HoldRejected)


async def test_commit_hands_over_the_link_and_is_idempotent() -> None:
    store = MemoryStore()
    request = hold_request(PAGE_ITEM, at(14), at(15))
    await hold_room(fake_http(), store, ROOMS, request, ttl_seconds=60, now=NOW)
    commit = CommitHold(round_id="round-3", hold_id="hold-1", payment_reference="free:wf-1")
    first = await commit_room(store, ROOMS, commit, now=NOW)
    again = await commit_room(store, ROOMS, commit, now=NOW)
    assert isinstance(first, BookingConfirmed) and isinstance(again, BookingConfirmed)
    assert first.confirmation_code == again.confirmation_code
    assert first.confirmation_code.startswith("HANDOFF-")
    unknown = await commit_room(
        store,
        ROOMS,
        CommitHold(round_id="r", hold_id="nope", payment_reference="free:wf-1"),
        now=NOW,
    )
    assert isinstance(unknown, HoldRejected)


async def test_demo_sellers_refuse_a_free_reference() -> None:
    store = MemoryStore()
    gear = LISTING_BY_ID["gear_share"]
    window = (datetime.now(ZONE) + timedelta(days=2)).replace(hour=15, minute=0, second=0)
    request = HoldRequest(
        round_id="round-2",
        workflow_id="wf-1",
        hold_id="gear-1",
        item_id="mirrorless_kit",
        quantity=1,
        window_start=window.isoformat(),
        window_end=(window + timedelta(hours=1)).isoformat(),
    )
    assert isinstance(
        await place_hold(store, gear, request, ttl_seconds=60, now=NOW), HoldConfirmed
    )
    refused = await commit_hold(
        store,
        gear,
        CommitHold(round_id="round-3", hold_id="gear-1", payment_reference="free:wf-1"),
        now=NOW,
    )
    assert isinstance(refused, HoldRejected)
    assert (await store.get_hold("gear-1"))["status"] == "held"


async def test_wellness_quotes_offer_only_wellness_spaces_as_free_handoffs() -> None:
    offers = await quote_rooms(fake_http(), ROOMS, wellness_request(duration_minutes="30"))
    assert [(offer["name"], offer["item_id"]) for offer in offers] == [
        ("Meditation Room", "libcal-9-11-104")
    ]
    offer = offers[0]
    assert (offer["category"], offer["subtotal_cents"], offer["handoff"]) == (
        "wellness_room",
        0,
        True,
    )
    assert offer["tags"] == ["wellness", "room"]
    assert offer["handoff_url"] == "https://calendar.lib.purdue.edu/space/104"
    assert (offer["slot_start"], offer["slot_end"]) == (at(12), at(12, 30))


async def test_wellness_quotes_explain_when_there_is_nothing_to_book() -> None:
    no_spaces = await quote_rooms(
        fake_http(),
        ROOMS,
        wellness_request(domain="ucla.edu", libcal_host="calendar.library.ucla.edu"),
    )
    no_libcal = await quote_rooms(
        fake_http(),
        ROOMS,
        wellness_request(system="page", libcal_host="", booking_url="https://lib.purdue.edu/r"),
    )
    booked = {f"104@{hour:02d}:{minute:02d}" for hour in range(12, 17) for minute in (0, 30)}
    full = await quote_rooms(fake_http(booked=booked), ROOMS, wellness_request())
    for offers, reason in (
        (no_spaces, "No wellness rooms are listed"),
        (no_libcal, "doesn't list wellness rooms"),
        (full, "No wellness room at"),
    ):
        assert len(offers) == 1 and offers[0]["available"] is False
        assert reason in offers[0]["unavailable_reason"]
        assert offers[0]["category"] == "wellness_room"


async def test_wellness_hold_rechecks_the_slot_live() -> None:
    held = await hold_room(
        fake_http(),
        MemoryStore(),
        ROOMS,
        hold_request("libcal-9-11-104", at(14), at(14, 30)),
        ttl_seconds=60,
        now=NOW,
    )
    assert isinstance(held, HoldConfirmed)
    taken = await hold_room(
        fake_http(booked={"104@14:00"}),
        MemoryStore(),
        ROOMS,
        hold_request("libcal-9-11-104", at(14), at(14, 30)),
        ttl_seconds=60,
        now=NOW,
    )
    assert isinstance(taken, HoldRejected)
