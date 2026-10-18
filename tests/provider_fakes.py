"""Offline stand-ins for every live source, served through ``httpx.MockTransport``.

Shapes mirror live responses seen 2026-10-17: the IPEDS directory and institutional
characteristics from the Urban Institute API, LibCal ``/allspaces`` pages and the availability
grid, and small school library and housing sites. No test ever touches the network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx

from resourcepilot.http import Http
from resourcepilot.research import IPEDS_CHARACTERISTICS, IPEDS_DIRECTORY

# Two days out, so the school's "today" never lands on it in any machine timezone.
SEARCH_DAY = date.today() + timedelta(days=2)
PRIVATE_HOSTS = frozenset({"intranet.purdue.edu"})


def libcal_html(*mentions: str) -> str:
    links = "".join(f'<a href="https://www.{domain}/">{domain}</a>' for domain in mentions)
    return (
        r"""<html><body><script>
        var resources = [];
        resources.push({
    id: "eid_101",
    title: "HSSE 2101 (Capacity 6)",
    url: "/space/101",
    eid: 101,
    gid: 7,
    lid: 9,
    grouping: "HSSE Library",
    capacity: 6,
});
        resources.push({
    id: "eid_102",
    title: "WALC 3138 (Capacity 2)",
    url: "/space/102",
    eid: 102,
    gid: 7,
    lid: 9,
    grouping: "HSSE Library",
    capacity: 2,
});
        resources.push({
    id: "eid_103",
    title: "Bernette b64 Serger",
    url: "/space/103",
    eid: 103,
    gid: 8,
    lid: 9,
    grouping: "Knowledge\u0020Lab\u0020Specialty\u0020Textile\u0020Machines",
    capacity: 0,
});
</script>"""
        + links
        + "<p>Powered by Springshare LibCal</p></body></html>"
    )


def libcal_grid(day: date, booked: set[str]) -> dict[str, Any]:
    """Half-hour slots from 8am to 10pm; ``booked`` holds ``"<eid>@HH:MM"`` taken slots."""
    slots = []
    for eid in (101, 102, 103):
        for minutes in range(8 * 60, 22 * 60, 30):
            start = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minutes)
            slot = {
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": (start + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
                "itemId": eid,
                "checksum": "x",
            }
            if f"{eid}@{start:%H:%M}" in booked:
                slot["className"] = "s-lc-eq-checkout"
            slots.append(slot)
    return {"slots": slots, "bookings": [], "isPreCreatedBooking": False, "windowEnd": False}


DIRECTORY = [
    {"unitid": 243780, "inst_name": "Purdue University-Main Campus", "url_school": "www.purdue.edu",
     "city": "West Lafayette", "state_abbr": "IN", "inst_size": 5, "currently_active_ipeds": 1},
    {"unitid": 110671, "inst_name": "University of California-Riverside",
     "url_school": "www.ucr.edu", "city": "Riverside", "state_abbr": "CA", "inst_size": 5,
     "currently_active_ipeds": 1},
    {"unitid": 166683, "inst_name": "Massachusetts Institute of Technology",
     "url_school": "web.mit.edu", "city": "Cambridge", "state_abbr": "MA", "inst_size": 4,
     "currently_active_ipeds": 1},
    {"unitid": 110662, "inst_name": "University of California-Los Angeles",
     "url_school": "www.ucla.edu", "city": "Los Angeles", "state_abbr": "CA", "inst_size": 5,
     "currently_active_ipeds": 1},
    {"unitid": 377555, "inst_name": "University of Washington-Tacoma Campus",
     "url_school": "www.tacoma.uw.edu", "city": "Tacoma", "state_abbr": "WA", "inst_size": 3,
     "currently_active_ipeds": 1},
    {"unitid": 377564, "inst_name": "University of Washington-Bothell Campus",
     "url_school": "www.bothell.uw.edu", "city": "Bothell", "state_abbr": "WA", "inst_size": 3,
     "currently_active_ipeds": 1},
]  # fmt: skip
CHARACTERISTICS = {
    "243780": {"unitid": 243780, "oncampus_housing": 1, "dormitory_capacity": 16056,
               "typical_room_charge": 5970, "typical_board_charge": 5000, "meal_plan": 1,
               "meals_per_week": 19, "oncampus_required": 2},
    "166683": {"unitid": 166683, "oncampus_housing": 1, "dormitory_capacity": -2,
               "typical_room_charge": -1, "typical_board_charge": -1, "meal_plan": 3},
}  # fmt: skip

PURDUE_LIBRARY = """<html><body><h1>Purdue Libraries</h1>
<a href="/hours">Hours</a>
<a href="https://calendar.lib.purdue.edu/reserve/spaces">Reserve a study room</a>
</body></html>"""
MIT_LIBRARY = """<html><body><h1>MIT Libraries</h1>
<a href="/hours">Hours</a><a href="/study">Places to study</a></body></html>"""
MIT_STUDY = "<html><body>Quiet study spaces are first come, first served.</body></html>"
MIT_BOOKING = "<html><body><h1>Reserve a group study room</h1></body></html>"
PURDUE_HOUSING = """<html><body><h1>University Residences: housing at Purdue</h1>
<a href="https://www.purdue.edu/purdue/apply/">Apply</a>
<a href="/maintenance">Maintenance request form</a>
<a href="https://purdue.starrezhousing.com/StarRezPortalX/">My Housing portal</a>
<a href="/apply">Apply for housing</a></body></html>"""

PAGES: dict[str, tuple[int, str, str]] = {
    # url: (status, body, location)
    "https://lib.purdue.edu/": (200, PURDUE_LIBRARY, ""),
    "https://calendar.lib.purdue.edu/allspaces": (200, libcal_html(), ""),
    "https://calendar.lib.purdue.edu/reserve/spaces": (200, libcal_html(), ""),
    "https://ucr.libcal.com/allspaces": (200, libcal_html("ucr.edu"), ""),
    "https://calendar.library.ucla.edu/allspaces": (200, libcal_html("ucla.edu"), ""),
    "https://libraries.mit.edu/": (200, MIT_LIBRARY, ""),
    "https://libraries.mit.edu/study": (200, MIT_STUDY, ""),
    "https://libraries.mit.edu/study/reserve-a-room": (200, MIT_BOOKING, ""),
    "https://libraries.mit.edu/moved": (302, "", "https://evil.example.com/rooms"),
    "https://housing.purdue.edu/": (200, PURDUE_HOUSING, ""),
    "https://housing.purdue.edu/apply": (200, "<html>Apply for housing</html>", ""),
    "https://purdue.starrezhousing.com/StarRezPortalX/": (200, "<html>Sign in</html>", ""),
    "https://intranet.purdue.edu/rooms": (200, "<html>Reserve a room</html>", ""),
}


def provider_handler(
    *,
    booked: set[str] | None = None,
    failing: set[str] | None = None,
    calls: list[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """``failing`` holds hosts that answer 503; ``calls`` records every URL requested."""
    taken = booked if booked is not None else set()

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        host, path = request.url.host, request.url.path
        if calls is not None:
            calls.append(url)
        if failing and host in failing:
            return httpx.Response(503, text="unavailable")
        if url.startswith(IPEDS_DIRECTORY):
            return httpx.Response(200, json={"results": DIRECTORY})
        if url.startswith(IPEDS_CHARACTERISTICS):
            row = CHARACTERISTICS.get(request.url.params.get("unitid", ""))
            return httpx.Response(200, json={"results": [row] if row else []})
        if path == "/spaces/availability/grid" and request.method == "POST":
            form = parse_qs(request.content.decode())
            return httpx.Response(
                200, json=libcal_grid(date.fromisoformat(form["start"][0]), taken)
            )
        key = f"https://{host}{path}"
        if key in PAGES:
            status, body, location = PAGES[key]
            headers = {"location": location} if location else {}
            return httpx.Response(status, text=body, headers=headers)
        return httpx.Response(404, text="not found")

    return handle


async def fake_resolver(host: str) -> list[str]:
    return ["10.0.0.5"] if host in PRIVATE_HOSTS else ["93.184.216.34"]


def fake_http(cache: Any = None, **options: Any) -> Http:
    transport = httpx.MockTransport(provider_handler(**options))
    return Http(httpx.AsyncClient(transport=transport), cache, resolver=fake_resolver)


def campus(**overrides: str) -> dict[str, str]:
    """The context the Campus Researcher hands Campus Rooms for a Purdue LibCal school."""
    return {
        "domain": "purdue.edu",
        "school_name": "Purdue University-Main Campus",
        "timezone": "America/Indiana/Indianapolis",
        "system": "libcal",
        "system_label": "LibCal",
        "booking_url": "https://calendar.lib.purdue.edu/allspaces",
        "libcal_host": "calendar.lib.purdue.edu",
        "group_size": "1",
        "duration_minutes": "60",
        **overrides,
    }


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)
