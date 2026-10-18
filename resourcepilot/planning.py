"""Planning: rank open rooms, and match on-campus housing to the student's criteria.

Rooms: one option per open room, earliest opening first, then the snuggest fit. When a re-check
finds a room taken, the Planner runs again with that room's ``item_id`` excluded, so the student
gets the next best rooms instead of an error. A wellness search with no room left gets no options
rather than an error: its card still shows events and the crisis line.

Housing: each hall the Researcher read is checked against each criterion as ``match``,
``unknown`` (the hall's page does not say), or ``out`` (the page rules it out). Halls ruled out
are hidden; the rest come confirmed matches first, then fewest unknowns, then lowest yearly price.
Nothing is inferred: an unknown never counts as a match, and only a page's own words rule a hall
out.
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


MAX_HALLS_SHOWN = 10
# Transfer students usually live where continuing students do.
WHO_FITS = {
    "first_year": {"first_year"},
    "transfer": {"transfer", "continuing"},
    "continuing": {"continuing"},
    "graduate": {"graduate"},
    "family": {"family"},
}


def housing_checks(brief: dict[str, Any], hall: dict[str, Any]) -> dict[str, str]:
    """``match``, ``unknown``, or ``out`` for each criterion the student set."""
    checks: dict[str, str] = {}
    who = str(brief.get("who") or "")
    if who:
        listed = set(hall.get("who") or [])
        checks["who"] = (
            "unknown" if not listed else "match" if listed & WHO_FITS.get(who, {who}) else "out"
        )
    style = str(brief.get("style") or "any")
    if style != "any":
        found = str(hall.get("style") or "")
        checks["style"] = "unknown" if not found else "match" if found == style else "out"
    features = set(hall.get("features") or [])
    for need in brief.get("must_haves") or []:
        # Pages rarely say what a hall lacks, so a missing feature is unknown, never out.
        checks[str(need)] = "match" if need in features else "unknown"
    budget = int(brief.get("max_budget_cents") or 0)
    if budget:
        # The hall's lowest published yearly price: a hall fits if some room in it does. A price
        # with no stated period, or none at all, is unknown, never a guess.
        price = int(hall.get("yearly_cents") or 0)
        checks["budget"] = "unknown" if not price else "match" if price <= budget else "out"
    return checks


def plan_housing(brief: dict[str, Any], halls: list[dict[str, Any]]) -> dict[str, Any]:
    """Every hall with its checks, ranked; ``shown`` leaves out halls the pages rule out."""
    rated = []
    for hall in halls:
        checks = housing_checks(brief, hall)
        unknown = sorted(key for key, value in checks.items() if value == "unknown")
        out = sorted(key for key, value in checks.items() if value == "out")
        rated.append({**hall, "checks": checks, "unknown": unknown, "ruled_out": out})

    def rank(hall: dict[str, Any]) -> tuple[int, int, int, str]:
        price = int(hall.get("yearly_cents") or 0) or 10**9
        return len(hall["ruled_out"]), len(hall["unknown"]), price, str(hall["name"]).lower()

    ranked = sorted(rated, key=rank)
    shown = [hall for hall in ranked if not hall["ruled_out"]][:MAX_HALLS_SHOWN]
    return {
        "halls": shown,
        "all_halls": ranked,
        "hidden_count": sum(1 for hall in ranked if hall["ruled_out"]),
    }


def planner_handler(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """The Planner's task: rank the verified offers, leaving out rooms already found taken; or
    match the school's halls to the student's housing criteria."""
    brief = dict(payload["brief"])
    if brief.get("service") == "housing":
        housing = dict(payload.get("housing") or {})
        return {**payload, **plan_housing(brief, list(housing.get("options") or []))}
    excluded = {str(item) for item in payload.get("exclude_items", [])}
    options = plan_room_options(brief, list(payload.get("offers", [])), excluded)
    return {**payload, "options": options, "exclude_items": sorted(excluded)}
