"""Campus Rooms: real open library study rooms at any school, handed over as booking links.

For LibCal schools the room list comes from the public ``/allspaces`` page and open slots from the
``/spaces/availability/grid`` endpoint the public booking page itself calls. That endpoint is not
a documented API and may change; failures fall back to a plain link, never to made-up rooms. Other
systems (EMS, 25Live, Skedda, Robin, or a booking page) get one offer that links to the school's
page, because their availability is not public.

ResourcePilot never reserves a room. Before it hands over the booking link it re-checks the slot
live, and the student books it on the school's site with their own sign-in. Nothing is paid.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from resourcepilot.clock import parse
from resourcepilot.http import Http, ProviderError
from resourcepilot.research import on_domain, safe_url

RESOURCE_RE = re.compile(r"resources\.push\(\{(.*?)\}\);", re.S)
ITEM_RE = re.compile(r"^libcal-(\d+)-(\d+)-(\d+)$")
PAGE_ITEM = "handoff-page"
MAX_GROUPS = 6
MAX_OFFERS = 6
DIRECTORY_TTL = 24 * 3600
GRID_TTL = 120
DEFAULT_TIMEZONE = "America/Los_Angeles"
TAKEN = "Someone just booked that time; pick another room"
# What every offer (and every planned line) carries; Planner, Verification, and cards read these.
LINE_KEYS = (
    "item_id",
    "name",
    "capacity",
    "slot_start",
    "slot_end",
    "slot_label",
    "location_label",
    "handoff_url",
    "live_times",
    "source_label",
    "school_name",
    "available",
    "unavailable_reason",
    "policy",
)
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


def _js_field(block: str, name: str) -> Any:
    match = re.search(rf'\b{name}:\s*("(?:[^"\\]|\\.)*"|-?\d+)', block)
    if not match:
        return None
    raw = match.group(1)
    return json.loads(raw) if raw.startswith('"') else int(raw)


def parse_libcal_resources(html: str) -> list[dict[str, Any]]:
    """The bookable spaces a LibCal ``/allspaces`` page lists, as plain fields only."""
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
    """Rooms to study in that seat the group (an unlisted capacity is kept)."""
    return [
        room
        for room in rooms
        if not NOT_STUDY.search(f"{room['grouping']} {room['name']}")
        and (not room["capacity"] or room["capacity"] >= group)
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


def _zone(campus: dict[str, str]) -> ZoneInfo:
    return ZoneInfo(campus.get("timezone") or DEFAULT_TIMEZONE)


def _local_window(start_iso: str, end_iso: str, zone: ZoneInfo) -> tuple[date, int, int]:
    start = parse(start_iso).astimezone(zone)
    end = parse(end_iso).astimezone(zone)
    high = _minutes(end) if end.date() == start.date() else 24 * 60
    return start.date(), _minutes(start), high


async def libcal_grid(
    http: Http, host: str, room: dict[str, Any], day: date, *, ttl: int
) -> list[dict[str, Any]]:
    """One day of slots for a room group, from the endpoint LibCal's own booking page calls."""
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


def _offer(campus: dict[str, str], **fields: Any) -> dict[str, Any]:
    school = campus.get("school_name") or campus.get("domain", "your school")
    offer: dict[str, Any] = {
        "capacity": 0,
        "slot_start": "",
        "slot_end": "",
        "slot_label": "",
        "available": True,
        "unavailable_reason": "",
        "school_name": school,
        "policy": (
            f"Free. ResourcePilot re-checks this room but cannot reserve it: book it on "
            f"{school}'s site with your own sign-in."
        ),
        **fields,
    }
    return {key: offer[key] for key in LINE_KEYS}


def _page_offer(campus: dict[str, str], note: str) -> dict[str, Any]:
    """One link to the school's booking page, for when live times are not available."""
    label = campus.get("system_label") or "booking page"
    return _offer(
        campus,
        item_id=PAGE_ITEM,
        name=f"Book on {campus.get('school_name') or 'your school'}'s {label}",
        handoff_url=campus.get("booking_url", ""),
        location_label=note,
        source_label=label,
        live_times=False,
    )


def _room_offer(
    campus: dict[str, str],
    host: str,
    room: dict[str, Any],
    day: date,
    zone: ZoneInfo,
    start: int,
    duration: int,
) -> dict[str, Any]:
    opens = datetime.combine(day, datetime.min.time(), zone) + timedelta(minutes=start)
    closes = opens + timedelta(minutes=duration)
    return _offer(
        campus,
        item_id=f"libcal-{room['lid']}-{room['gid']}-{room['eid']}",
        name=room["name"],
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


async def find_rooms(
    http: Http, campus: dict[str, str], window_start: str, window_end: str
) -> list[dict[str, Any]]:
    """Open rooms in the window, earliest first; or one booking-page link when times are not live.

    Returns ``[]`` when the school context or its booking link cannot be trusted, and a single
    unavailable link offer (with the reason) when LibCal shows no room open in the window.
    """
    campus = dict(campus)
    if not _campus_ok(campus):
        return []
    zone = _zone(campus)
    day, low, high = _local_window(window_start, window_end, zone)
    duration = int(campus.get("duration_minutes") or (high - low))
    group = max(1, int(campus.get("group_size") or 1))
    host = libcal_host(campus)
    if not host:
        if not safe_url(campus.get("booking_url", ""), campus.get("domain", "")):
            return []
        return [_page_offer(campus, "Availability isn't public")]
    if not await http.public_host(host):
        return []
    try:
        html = await http.get_text(
            f"https://{host}/allspaces", provider="LibCal", cache_ttl=DIRECTORY_TTL
        )
        rooms = study_spaces(parse_libcal_resources(str(html)), group)
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
                        _room_offer(campus, host, room, day, zone, start, duration),
                    )
                )
    except ProviderError:
        return [_page_offer(campus, "Live times unavailable right now")]
    if not offers:
        reason = (
            f"No room for {group} at {campus.get('school_name')} is open for "
            f"{duration} minutes in that window"
        )
        return [
            {
                **_page_offer(campus, "No open room in that window"),
                "available": False,
                "unavailable_reason": reason,
            }
        ]
    offers.sort(key=lambda item: (item[0], item[1]))
    return [offer for _, _, offer in offers[:MAX_OFFERS]]


async def recheck_room(
    http: Http, campus: dict[str, str], item_id: str, slot_start: str, slot_end: str
) -> str | None:
    """Return None when the slot is still open, or a card-safe reason it is not.

    The booking-page link has no slot to contend for, so it is always still open.
    """
    if item_id == PAGE_ITEM:
        return None
    campus = dict(campus)
    if not _campus_ok(campus):
        return "Missing school context for this room"
    match = ITEM_RE.match(item_id)
    host = libcal_host(campus)
    if not match or not host:
        return "Unknown room"
    if not await http.public_host(host):
        return "That room system is not reachable"
    lid, gid, eid = (int(value) for value in match.groups())
    try:
        day, low, high = _local_window(slot_start, slot_end, _zone(campus))
    except ValueError:
        return "Unknown time for this room"
    if high <= low:
        return "Unknown time for this room"
    try:
        slots = await libcal_grid(http, host, {"lid": lid, "gid": gid, "eid": eid}, day, ttl=0)
    except ProviderError as exc:
        return str(exc)
    if first_opening(open_runs(slots, eid, day), (low, high), high - low) != low:
        return TAKEN
    return None


def _payload_campus(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("campus")
    campus = {str(key): str(value) for key, value in raw.items()} if isinstance(raw, dict) else {}
    if not _campus_ok(campus):
        raise ValueError("Missing school context for rooms")
    return campus


async def campus_rooms_handler(payload: dict[str, Any], http: Http) -> dict[str, Any]:
    """Campus Rooms' task: ``find`` open rooms for the brief, or ``recheck`` one chosen line."""
    campus = _payload_campus(payload)
    if payload.get("phase") == "recheck":
        line = payload.get("line")
        if not isinstance(line, dict) or not line.get("item_id"):
            raise ValueError("Missing the room to re-check")
        item_id = str(line["item_id"])
        reason = await recheck_room(
            http,
            campus,
            item_id,
            str(line.get("slot_start") or ""),
            str(line.get("slot_end") or ""),
        )
        return {"open": reason is None, "reason": reason or "", "item_id": item_id}
    brief = payload.get("brief")
    brief = brief if isinstance(brief, dict) else {}
    window_start, window_end = brief.get("window_start"), brief.get("window_end")
    if not window_start or not window_end:
        raise ValueError("Missing the time window for rooms")
    offers = await find_rooms(http, campus, str(window_start), str(window_end))
    return {**payload, "offers": offers}
