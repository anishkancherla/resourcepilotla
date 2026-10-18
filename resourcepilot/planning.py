"""Room planning: one option per open room, earliest opening first, then the snuggest fit.

When a re-check finds a room taken, the Planner runs again with that room's ``item_id`` excluded,
so the student gets the next best rooms instead of an error. A wellness search with no room left
gets no options rather than an error: its card still shows events and the crisis line.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from resourcepilot.rooms import LINE_KEYS

MAX_ROOM_OPTIONS = 6
NO_ROOM = "No open room is left at your school for that time"


def option_id(line: dict[str, Any]) -> str:
    """A stable id for one room at one start time."""
    key = f"{line.get('item_id', '')}|{line.get('slot_start') or ''}"
    return f"opt_{sha256(key.encode()).hexdigest()[:10]}"


def _line(offer: dict[str, Any]) -> dict[str, Any]:
    return {key: offer.get(key) for key in LINE_KEYS}


def plan_room_options(
    brief: dict[str, Any],
    offers: list[dict[str, Any]],
    exclude_items: set[str] | None = None,
) -> list[dict[str, Any]]:
    """One single-line option per open room: earliest start, then the snuggest fit."""
    group = int(brief.get("group_size") or 1)
    excluded = exclude_items or set()
    usable = [
        offer
        for offer in offers
        if offer.get("item_id")
        and offer.get("available", True)
        and str(offer["item_id"]) not in excluded
    ]
    if not usable and brief.get("service") == "wellness":
        return []
    if not usable:
        raise ValueError(NO_ROOM)

    def fit(offer: dict[str, Any]) -> tuple[str, int, str]:
        capacity = int(offer.get("capacity") or 0)
        snug = capacity - group if capacity else 99
        return str(offer.get("slot_start") or ""), snug, str(offer["item_id"])

    options = []
    for index, offer in enumerate(sorted(usable, key=fit)[:MAX_ROOM_OPTIONS]):
        line = _line(offer)
        label = "Earliest opening" if index == 0 else str(line.get("slot_label") or line["name"])
        options.append({"option_id": option_id(line), "label": label, "lines": [line]})
    return options


def planner_handler(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """The Planner's task: rank the verified offers, leaving out rooms already found taken."""
    brief = dict(payload["brief"])
    excluded = {str(item) for item in payload.get("exclude_items", [])}
    options = plan_room_options(brief, list(payload.get("offers", [])), excluded)
    return {**payload, "options": options, "exclude_items": sorted(excluded)}
