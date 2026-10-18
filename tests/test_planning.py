from __future__ import annotations

from hashlib import sha256
from typing import Any

import pytest

from resourcepilot.planning import MAX_ROOM_OPTIONS, option_id, plan_room_options, planner_handler
from resourcepilot.rooms import LINE_KEYS, PAGE_ITEM

BRIEF = {"service": "rooms", "group_size": 3}


def offer(item_id: str, start: str, capacity: int = 0, **fields: Any) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "name": f"Room {item_id}",
        "capacity": capacity,
        "slot_start": f"2026-10-19T{start}:00-04:00",
        "slot_end": "2026-10-19T17:00:00-04:00",
        "slot_label": f"Mon Oct 19 \u00b7 {start}",
        "location_label": "Library",
        "handoff_url": f"https://calendar.lib.purdue.edu/space/{item_id}",
        "live_times": True,
        "source_label": "LibCal",
        "school_name": "Purdue University-Main Campus",
        "available": True,
        "unavailable_reason": "",
        "policy": "Free.",
        **fields,
    }


OFFERS = [
    offer("late", "14:00", 4),
    offer("big", "12:00", 10),
    offer("snug", "12:00", 3),
    offer("unlisted", "12:00", 0),
    offer("early", "11:30", 8),
]


def ids(options: list[dict[str, Any]]) -> list[str]:
    return [option["lines"][0]["item_id"] for option in options]


def test_rooms_rank_earliest_then_snuggest_fit() -> None:
    options = plan_room_options(BRIEF, OFFERS)
    assert ids(options) == ["early", "snug", "big", "unlisted", "late"]
    assert options[0]["label"] == "Earliest opening"
    assert [option["label"] for option in options[1:]] == [
        "Mon Oct 19 \u00b7 12:00",
        "Mon Oct 19 \u00b7 12:00",
        "Mon Oct 19 \u00b7 12:00",
        "Mon Oct 19 \u00b7 14:00",
    ]
    assert all(set(option) == {"option_id", "label", "lines"} for option in options)
    assert all(tuple(option["lines"][0]) == LINE_KEYS for option in options)


def test_label_falls_back_to_the_room_name() -> None:
    options = plan_room_options(BRIEF, [offer("a", "12:00"), offer("b", "13:00", slot_label="")])
    assert [option["label"] for option in options] == ["Earliest opening", "Room b"]


def test_excluded_and_unavailable_rooms_are_left_out() -> None:
    options = plan_room_options(BRIEF, OFFERS, {"early", "snug"})
    assert ids(options) == ["big", "unlisted", "late"]
    assert options[0]["label"] == "Earliest opening"
    closed = [offer("x", "12:00", available=False), offer("y", "13:00")]
    assert ids(plan_room_options(BRIEF, closed)) == ["y"]


def test_options_are_capped() -> None:
    many = [offer(f"r{index}", f"{10 + index}:00") for index in range(MAX_ROOM_OPTIONS + 3)]
    assert len(plan_room_options(BRIEF, many)) == MAX_ROOM_OPTIONS


def test_nothing_usable_raises() -> None:
    message = "No open room is left at your school for that time"
    with pytest.raises(ValueError, match=message):
        plan_room_options(BRIEF, [])
    with pytest.raises(ValueError, match=message):
        plan_room_options(BRIEF, [offer("only", "12:00")], {"only"})


def test_option_ids_are_stable() -> None:
    first = plan_room_options(BRIEF, OFFERS)
    second = plan_room_options(BRIEF, list(reversed(OFFERS)))
    assert [option["option_id"] for option in first] == [option["option_id"] for option in second]
    line = first[0]["lines"][0]
    expected = sha256(f"early|{line['slot_start']}".encode()).hexdigest()[:10]
    assert first[0]["option_id"] == option_id(line) == f"opt_{expected}"
    assert len({option["option_id"] for option in first}) == len(first)
    page = {"item_id": PAGE_ITEM, "slot_start": ""}
    assert option_id(page) == f"opt_{sha256(f'{PAGE_ITEM}|'.encode()).hexdigest()[:10]}"


def test_planner_handler_carries_the_payload_and_exclusions() -> None:
    payload = {
        "brief": BRIEF,
        "school": {"domain": "purdue.edu"},
        "offers": OFFERS,
        "exclude_items": ["snug", "early", "snug"],
    }
    result = planner_handler(payload, {})
    assert result["school"] == payload["school"]
    assert result["offers"] == OFFERS
    assert result["exclude_items"] == ["early", "snug"]
    assert ids(result["options"]) == ["big", "unlisted", "late"]
    fresh = planner_handler({"brief": BRIEF, "offers": OFFERS}, {})
    assert fresh["exclude_items"] == []
    assert ids(fresh["options"])[0] == "early"


def test_wellness_with_nothing_usable_has_no_options_instead_of_an_error() -> None:
    wellness = {**BRIEF, "service": "wellness", "group_size": 1}
    assert plan_room_options(wellness, []) == []
    assert plan_room_options(wellness, [offer("only", "12:00")], {"only"}) == []
