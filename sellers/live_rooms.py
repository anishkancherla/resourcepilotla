"""Campus Rooms: real open study and wellness rooms at any school, quoted as free booking handoffs.

For LibCal schools the room list comes from the public ``/allspaces`` page and open slots from the
``/spaces/availability/grid`` endpoint the public booking page itself calls. That endpoint is not
a documented API and may change; failures fall back to a plain link, never to made-up rooms. Other
systems (EMS, 25Live, Skedda, Robin, or a booking page) get one offer that links to the school's
page, because their availability is not public.

ResourcePilot never reserves a room. A hold is a live re-check of the slot, and a commit hands
over the school's booking link. Every offer costs $0, so no payment ever covers it.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from resourcepilot.clock import later, parse
from resourcepilot.http import Http, ProviderError
from resourcepilot.protocols import (
    BookingConfirmed,
    CommitHold,
    HoldConfirmed,
    HoldRejected,
    HoldRequest,
    QuoteRequest,
)
from resourcepilot.registry import SellerListing
from resourcepilot.research import on_domain, safe_url
from resourcepilot.store import DurableStore

RESOURCE_RE = re.compile(r"resources\.push\(\{(.*?)\}\);", re.S)
ITEM_RE = re.compile(r"^libcal-(\d+)-(\d+)-(\d+)$")
PAGE_ITEM = "handoff-page"
MAX_GROUPS = 6
MAX_OFFERS = 6
DIRECTORY_TTL = 24 * 3600
GRID_TTL = 120
CATEGORY = "study_room"
WELLNESS_CATEGORY = "wellness_room"
# LibCal lists every bookable thing as a "space": sergers, terminals, studios. Students asked for
# a room to study in, so equipment and special-purpose spaces are left out, and study-room groups
# are tried first.
NOT_STUDY = re.compile(
    r"machine|terminal|\bstations?\b|serger|embroider|sewing|textile|printer|scanner|"
    r"equipment|laptop|camera|\bkits?\b|bloomberg|\b3d\b|\bvr\b|archives|special collections|"
    r"podcast|studio|recording",
    re.I,
)
STUDY = re.compile(r"study|group|collaborat|\bpods?\b|booth|commons|meeting|discussion", re.I)
# Spaces for rest and reflection. They are never offered as study rooms, and a wellness search
# offers nothing else. A wellness search never asks why the student wants the room.
WELLNESS = re.compile(
    r"meditat|reflection|prayer|wellness|well-?being|mindful|relax|\bnap\b|lactation|"
    r"nursing|sensory",
    re.I,
)


def _js_field(block: str, name: str) -> Any:
    match = re.search(rf'\b{name}:\s*("(?:[^"\\]|\\.)*"|-?\d+)', block)
    if not match:
        return None
    raw = match.group(1)
    return json.loads(raw) if raw.startswith('"') else int(raw)


def parse_libcal_resources(html: str) -> list[dict[str, Any]]:
    rooms = []
    for block in RESOURCE_RE.findall(html):
        eid, lid, gid = (_js_field(block, key) for key in ("eid", "lid", "gid"))
        title = _js_field(block, "title")
        if not (eid and lid and gid and title):
            continue
        rooms.append(
            {
                "eid": int(eid),
                "lid": int(lid),
                "gid": int(gid),
                "name": re.sub(r"\s*\(Capacity \d+\)\s*$", "", str(title)).strip()[:80],
                "grouping": str(_js_field(block, "grouping") or "Study space")[:60],
                "capacity": int(_js_field(block, "capacity") or 0),
            }
        )
    return rooms


def study_spaces(rooms: list[dict[str, Any]], group: int) -> list[dict[str, Any]]:
    return [
        room
        for room in rooms
        if not NOT_STUDY.search(f"{room['grouping']} {room['name']}")
        and not WELLNESS.search(f"{room['grouping']} {room['name']}")
        and (not room["capacity"] or room["capacity"] >= group)
    ]


def wellness_spaces(rooms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        room
        for room in rooms
        if WELLNESS.search(f"{room['grouping']} {room['name']}")
        and not NOT_STUDY.search(f"{room['grouping']} {room['name']}")
    ]


def _minutes(value: datetime) -> int:
    return value.hour * 60 + value.minute


def clock_label(minutes: int) -> str:
    hour, minute = divmod(minutes, 60)
    suffix = "am" if hour < 12 or hour == 24 else "pm"
    return f"{(hour % 12) or 12}:{minute:02d}{suffix}"


def open_runs(slots: list[dict[str, Any]], eid: int, day: date) -> list[tuple[int, int]]:
    """Merge contiguous free slots for one room into (start, end) minute ranges."""
    free = []
    for slot in slots:
        if slot.get("itemId") != eid or slot.get("className"):
            continue
        try:
            opens = datetime.fromisoformat(str(slot["start"]))
            closes = datetime.fromisoformat(str(slot["end"]))
        except (KeyError, ValueError):
            continue
        if opens.date() == day:
            free.append((_minutes(opens), _minutes(closes) if closes.date() == day else 24 * 60))
    runs: list[list[int]] = []
    for start, end in sorted(free):
        if runs and runs[-1][1] == start:
            runs[-1][1] = end
        else:
            runs.append([start, end])
    return [(start, end) for start, end in runs]


def first_opening(
    runs: list[tuple[int, int]], window: tuple[int, int], duration: int
) -> int | None:
    """The first start that fits ``duration`` inside the window."""
    low, high = window
    for start, end in runs:
        start, end = max(start, low), min(end, high)
        if end - start >= duration:
            return start
    return None


def _campus_ok(campus: dict[str, str]) -> bool:
    domain = campus.get("domain", "")
    return bool(domain) and safe_url(f"https://{domain}/", domain)


def libcal_host(campus: dict[str, str]) -> str:
    """The LibCal host to read, only if it is the school's own domain or libcal.com."""
    host = campus.get("libcal_host", "").strip().lower()
    domain = campus.get("domain", "")
    if campus.get("system") != "libcal" or not host:
        return ""
    if on_domain(host, domain) or on_domain(host, "libcal.com"):
        return host
    return ""


def _local_window(request: QuoteRequest | HoldRequest, zone: ZoneInfo) -> tuple[date, int, int]:
    start = parse(request.window_start).astimezone(zone)
    end = parse(request.window_end).astimezone(zone)
    high = _minutes(end) if end.date() == start.date() else 24 * 60
    return start.date(), _minutes(start), high


async def libcal_grid(
    http: Http, host: str, room: dict[str, Any], day: date, *, ttl: int
) -> list[dict[str, Any]]:
    base = f"https://{host}"
    data = await http.post_form_json(
        f"{base}/spaces/availability/grid",
        {
            "lid": room["lid"],
            "gid": room["gid"],
            "eid": room["eid"],
            "seat": 0,
            "seatId": 0,
            "zone": 0,
            "start": day.isoformat(),
            "end": (day + timedelta(days=1)).isoformat(),
            "pageIndex": 0,
            "pageSize": 18,
        },
        provider="LibCal",
        headers={"Referer": f"{base}/allspaces", "X-Requested-With": "XMLHttpRequest"},
        cache_ttl=ttl,
    )
    slots = data.get("slots") if isinstance(data, dict) else None
    if not isinstance(slots, list):
        raise ProviderError("LibCal returned availability in an unexpected format")
    return slots


def _offer(
    listing: SellerListing, campus: dict[str, str], category: str = CATEGORY, **fields: Any
) -> dict[str, Any]:
    school = campus.get("school_name") or campus.get("domain", "your school")
    wellness = category == WELLNESS_CATEGORY
    return {
        "category": category,
        "seller_id": listing.seller_id,
        "unit": "hour",
        "unit_price_cents": 0,
        "subtotal_cents": 0,
        "quantity": 1,
        "capacity": 0,
        "ready_in_hours": 0,
        "deposit_cents": 0,
        "requires_training": False,
        "rating": None,
        "tags": ["wellness", "room"] if wellness else ["study", "room"],
        "available": True,
        "unavailable_reason": "",
        "handoff": True,
        "school_name": school,
        "policy": (
            f"Free. ResourcePilot re-checks this room but cannot reserve it: book it on "
            f"{school}'s site with your own sign-in."
        ),
        **fields,
    }


def page_offer(
    listing: SellerListing,
    campus: dict[str, str],
    hours: float,
    note: str,
    category: str = CATEGORY,
) -> dict:
    label = campus.get("system_label") or "booking page"
    return _offer(
        listing,
        campus,
        category,
        item_id=PAGE_ITEM,
        name=f"Book on {campus.get('school_name') or 'your school'}'s {label}",
        hours=round(hours, 2),
        handoff_url=campus.get("booking_url", ""),
        location_label=note,
        source_label=label,
        live_times=False,
    )


def _not_offered(
    listing: SellerListing, campus: dict[str, str], hours: float, reason: str
) -> list[dict[str, Any]]:
    """A wellness search that found nothing to book: a reason for the card, never a room."""
    return [
        {
            **page_offer(listing, campus, hours, reason, WELLNESS_CATEGORY),
            "available": False,
            "unavailable_reason": reason,
        }
    ]


async def quote_rooms(
    http: Http, listing: SellerListing, request: QuoteRequest
) -> list[dict[str, Any]]:
    campus = dict(request.campus)
    if CATEGORY in request.categories:
        category = CATEGORY
    elif WELLNESS_CATEGORY in request.categories:
        category = WELLNESS_CATEGORY
    else:
        return []
    if not _campus_ok(campus):
        return []
    wellness = category == WELLNESS_CATEGORY
    school = campus.get("school_name") or "your school"
    zone = ZoneInfo(campus.get("timezone") or "America/Los_Angeles")
    day, low, high = _local_window(request, zone)
    duration = int(campus.get("duration_minutes") or (high - low))
    group = 1 if wellness else max(1, int(campus.get("group_size") or 1))
    host = libcal_host(campus)
    if not host:
        if wellness:
            # Only LibCal lists spaces publicly; a link-only page could not say it has any.
            return _not_offered(
                listing,
                campus,
                duration / 60,
                f"{school}'s room system doesn't list wellness rooms",
            )
        if not safe_url(campus.get("booking_url", ""), campus.get("domain", "")):
            return []
        return [page_offer(listing, campus, duration / 60, "Availability isn't public")]
    if not await http.public_host(host):
        return []
    try:
        html = await http.get_text(
            f"https://{host}/allspaces", provider="LibCal", cache_ttl=DIRECTORY_TTL
        )
        listed = parse_libcal_resources(str(html))
        rooms = wellness_spaces(listed) if wellness else study_spaces(listed, group)
        if wellness and not rooms:
            return _not_offered(
                listing, campus, duration / 60, f"No wellness rooms are listed in {school}'s LibCal"
            )
        groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for room in rooms:
            groups.setdefault((room["lid"], room["gid"]), []).append(room)
        offers: list[tuple[int, int, dict[str, Any]]] = []
        ranked = sorted(
            groups.values(),
            key=lambda members: (not STUDY.search(members[0]["grouping"]), -len(members)),
        )
        for members in ranked[:MAX_GROUPS]:
            slots = await libcal_grid(http, host, members[0], day, ttl=GRID_TTL)
            for room in members:
                start = first_opening(open_runs(slots, room["eid"], day), (low, high), duration)
                if start is None:
                    continue
                offers.append(
                    (
                        start,
                        room["capacity"] or 99,
                        _room_offer(
                            listing, campus, host, room, day, zone, start, duration, category
                        ),
                    )
                )
    except ProviderError:
        if wellness:
            return _not_offered(listing, campus, duration / 60, "Live room times unavailable")
        return [page_offer(listing, campus, duration / 60, "Live times unavailable right now")]
    if not offers:
        reason = (
            f"No wellness room at {school} is open for {duration} minutes in that window"
            if wellness
            else f"No room for {group} at {campus.get('school_name')} is open for "
            f"{duration} minutes in that window"
        )
        return [
            {
                **page_offer(
                    listing, campus, duration / 60, "No open room in that window", category
                ),
                "available": False,
                "unavailable_reason": reason,
            }
        ]
    offers.sort(key=lambda item: (item[0], item[1]))
    return [offer for _, _, offer in offers[:MAX_OFFERS]]


def _room_offer(
    listing: SellerListing,
    campus: dict[str, str],
    host: str,
    room: dict[str, Any],
    day: date,
    zone: ZoneInfo,
    start: int,
    duration: int,
    category: str = CATEGORY,
) -> dict[str, Any]:
    opens = datetime.combine(day, datetime.min.time(), zone) + timedelta(minutes=start)
    closes = opens + timedelta(minutes=duration)
    return _offer(
        listing,
        campus,
        category,
        item_id=f"libcal-{room['lid']}-{room['gid']}-{room['eid']}",
        name=room["name"],
        hours=round(duration / 60, 2),
        capacity=int(room["capacity"] or 0),
        handoff_url=f"https://{host}/space/{room['eid']}",
        location_label=room["grouping"],
        source_label="LibCal",
        live_times=True,
        slot_start=opens.isoformat(),
        slot_end=closes.isoformat(),
        slot_label=(
            f"{day:%a %b} {day.day} \u00b7 "
            f"{clock_label(start)}\u2013{clock_label(start + duration)}"
        ),
    )


async def recheck_slot(http: Http, campus: dict[str, str], request: HoldRequest) -> str | None:
    """Return None when the slot is still open, or the reason it is not."""
    match = ITEM_RE.match(request.item_id)
    host = libcal_host(campus)
    if not match or not host:
        return "Unknown room"
    if not await http.public_host(host):
        return "That room system is not reachable"
    lid, gid, eid = (int(value) for value in match.groups())
    zone = ZoneInfo(campus.get("timezone") or "America/Los_Angeles")
    day, low, high = _local_window(request, zone)
    try:
        slots = await libcal_grid(http, host, {"lid": lid, "gid": gid, "eid": eid}, day, ttl=0)
    except ProviderError as exc:
        return str(exc)
    if first_opening(open_runs(slots, eid, day), (low, high), high - low) != low:
        return "Someone just booked that time; pick another room"
    return None


async def hold_room(
    http: Http,
    store: DurableStore,
    listing: SellerListing,
    request: HoldRequest,
    *,
    ttl_seconds: int,
    now: str,
) -> HoldConfirmed | HoldRejected:
    def reject(reason: str) -> HoldRejected:
        return HoldRejected(
            round_id=request.round_id,
            hold_id=request.hold_id,
            seller_id=listing.seller_id,
            reason=reason[:200],
        )

    campus = dict(request.campus)
    if not _campus_ok(campus) or request.quantity != 1:
        return reject("Missing school context for this room")
    if request.item_id != PAGE_ITEM:
        problem = await recheck_slot(http, campus, request)
        if problem:
            return reject(problem)
    expires_at = later(ttl_seconds, now=parse(now))
    status = await store.place_hold(
        {
            "hold_id": request.hold_id,
            "seller_id": listing.seller_id,
            "item_id": request.item_id,
            "workflow_id": request.workflow_id,
            "quantity": 1,
            "window_start": request.window_start,
            "window_end": request.window_end,
            "expires_at": expires_at,
            "subtotal_cents": 0,
        },
        # A marker, not a reservation: the page offer has no slot to contend for.
        1 if request.item_id != PAGE_ITEM else 1_000_000,
        now,
    )
    if status == "unavailable":
        return reject("Another ResourcePilot student is checking out that room right now")
    if status == "duplicate":
        existing = await store.get_hold(request.hold_id)
        if existing is None or existing["status"] != "held":
            return reject("Hold is no longer active")
        expires_at = str(existing["expires_at"])
    return HoldConfirmed(
        round_id=request.round_id,
        hold_id=request.hold_id,
        seller_id=listing.seller_id,
        expires_at=expires_at,
        subtotal_cents=0,
    )


def handoff_code(hold_id: str) -> str:
    return f"HANDOFF-{str(uuid5(NAMESPACE_URL, f'campus_rooms:{hold_id}'))[:6].upper()}"


async def commit_room(
    store: DurableStore, listing: SellerListing, request: CommitHold, *, now: str
) -> BookingConfirmed | HoldRejected:
    """Hand over the link. Never fails a paid bundle: the room was never part of the escrow."""
    hold = await store.get_hold(request.hold_id)
    if hold is None or hold["seller_id"] != listing.seller_id:
        return HoldRejected(
            round_id=request.round_id,
            hold_id=request.hold_id,
            seller_id=listing.seller_id,
            reason="Unknown room hold",
        )
    code = handoff_code(request.hold_id)
    await store.commit_hold(request.hold_id, request.payment_reference, code, now)
    return BookingConfirmed(
        round_id=request.round_id,
        hold_id=request.hold_id,
        seller_id=listing.seller_id,
        confirmation_code=code,
    )
