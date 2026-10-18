"""Room vetting: decide which room offers ResourcePilot is willing to put in front of a student.

Campus Rooms reads untrusted school pages, so Verification checks every offer independently
before the Planner ranks it:

1. The offer names a room (``name`` and ``item_id``) and is available; an unavailable offer
   keeps its own reason, such as "No room for 4 ... is open".
2. Its booking link is https on the school's own domain or a known booking vendor.
3. A live time slot sits inside the window the student asked for.
4. A listed capacity seats the whole group.

Housing links are re-opened separately (``research.recheck_link``), because that check is a
network call.
"""

from __future__ import annotations

from typing import Any

from resourcepilot.clock import parse
from resourcepilot.research import safe_url

NO_ROOM = "No open room is left at your school for that time"
MISSING = "Offer is missing a room name or id"
UNSAFE_LINK = "Booking link is not on the school's site or a known booking system"
OUTSIDE_WINDOW = "Open time is outside the window you asked for"


def _inside_window(brief: dict[str, Any], offer: dict[str, Any]) -> bool:
    try:
        low, high = parse(str(brief["window_start"])), parse(str(brief["window_end"]))
        start, end = parse(str(offer["slot_start"])), parse(str(offer["slot_end"]))
    except (KeyError, TypeError, ValueError):
        return False
    return low <= start < end <= high


def _capacity(offer: dict[str, Any]) -> int:
    try:
        return int(offer.get("capacity") or 0)
    except (TypeError, ValueError):
        return 0


def _problem(brief: dict[str, Any], offer: dict[str, Any], domain: str, group: int) -> str | None:
    if not offer.get("name") or not offer.get("item_id"):
        return MISSING
    if not offer.get("available"):
        return str(offer.get("unavailable_reason") or "Not available")
    if not safe_url(str(offer.get("handoff_url") or ""), domain):
        return UNSAFE_LINK
    if offer.get("live_times") and not _inside_window(brief, offer):
        return OUTSIDE_WINDOW
    capacity = _capacity(offer)
    if capacity and capacity < group:
        return f"Seats {capacity}, fewer than your group of {group}"
    return None


def vet_room_offers(
    brief: dict[str, Any], offers: list[dict[str, Any]], campus: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Return (accepted offers, rejections); each rejection is ``{"item", "reason"}``."""
    domain = str((campus or {}).get("domain") or "")
    group = max(1, int(brief.get("group_size") or 1))
    accepted: list[dict[str, Any]] = []
    rejections: list[dict[str, str]] = []
    for raw in offers:
        offer = dict(raw) if isinstance(raw, dict) else {}
        problem = _problem(brief, offer, domain, group)
        if problem:
            item = str(offer.get("name") or offer.get("item_id") or "Room")
            rejections.append({"item": item, "reason": problem})
            continue
        accepted.append(offer)
    return accepted, rejections


def verification_handler(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Verification's room task: keep only the offers that pass every check."""
    brief = dict(payload["brief"])
    campus = dict(payload.get("campus") or {})
    accepted, rejections = vet_room_offers(brief, list(payload.get("offers", [])), campus)
    if not accepted:
        raise ValueError(rejections[0]["reason"] if rejections else NO_ROOM)
    return {**payload, "offers": accepted, "rejections": rejections}
