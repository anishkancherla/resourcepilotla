"""Turn a submitted form into a validated brief for one campus service.

ResourcePilot takes requests only from cards and forms, never free text. Every brief is bounded
and deterministic, and missing fields are reported so the Orchestrator can show the form again.
A school email is reduced to its domain before the request is stored; the part before ``@`` is
never kept, logged, or sent to another agent.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from resourcepilot.clock import CAMPUS_TZ, parse, utc_now
from resourcepilot.registry import CATEGORIES
from resourcepilot.research import WINDOWS, school_domain

SERVICES = ("rooms", "housing", "bundle")
PRIORITIES = {"cheapest": "Lowest price", "closest": "Shortest walk", "best_rated": "Best rated"}
ANCHORS: dict[str, tuple[str, float, float]] = {
    "student_union": ("Student union", 34.0705, -118.4445),
    "main_library": ("Main library", 34.0716, -118.4422),
    "north_dorms": ("North dorms", 34.0729, -118.4520),
}
CAMERA_TYPES = ("any", "dslr", "mirrorless")
ROOM_DURATIONS = {"30": "30 minutes", "60": "1 hour", "90": "90 minutes", "120": "2 hours"}
MAX_GROUP = 12
MAX_POSTERS = 500
MAX_HOURS = 8
MAX_BUDGET_CENTS = 50_000
MAX_DAYS_AHEAD = 14
REQUIRED = ("items", "day", "start", "end", "budget_cents")
EMAIL_FIELDS = ("email", "school_email")


def _local_today(now: datetime) -> date:
    return now.astimezone(CAMPUS_TZ).date()


def scrub_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Replace any email field with its school domain before the request is stored anywhere."""
    clean = {key: value for key, value in fields.items() if key not in EMAIL_FIELDS}
    raw = next((str(fields[key]) for key in EMAIL_FIELDS if fields.get(key)), "")
    if raw:
        try:
            clean["school_domain"] = school_domain(raw)
        except ValueError:
            clean["school_domain"] = ""
            clean["email_invalid"] = True
    return clean


def _require_domain(fields: dict[str, Any], purpose: str) -> str:
    if fields.get("email_invalid"):
        raise ValueError("Missing: a valid school email, for example you@purdue.edu")
    domain = str(fields.get("school_domain") or "").strip().lower()
    if not domain:
        raise ValueError(f"Missing: your school email {purpose}")
    return school_domain(domain)


def from_form(fields: dict[str, Any]) -> dict[str, Any]:
    """Map captured demo-bundle form fields onto a partial request for ``build_brief``."""
    items: dict[str, int] = {}
    camera = str(fields.get("camera", "none"))
    if camera in CAMERA_TYPES:
        items["camera"] = 1
    if _truthy(fields.get("microphone")):
        items["microphone"] = 1
    if _truthy(fields.get("tripod")):
        items["tripod"] = 1
    room = str(fields.get("room", "none"))
    if room in {"podcast_room", "study_room"}:
        items[room] = 1
    posters = _int(fields.get("posters"))
    if posters:
        items["posters"] = posters
    request: dict[str, Any] = {
        "items": items,
        "camera_type": camera if camera in CAMERA_TYPES else "any",
        "priority": str(fields.get("priority") or "cheapest"),
        "anchor": str(fields.get("anchor") or "student_union"),
        "has_training": _truthy(fields.get("has_training")),
        "notes": str(fields.get("notes") or "")[:300],
    }
    if fields.get("day"):
        request["day"] = str(fields["day"])
    start_hour = _int(fields.get("start_hour"))
    hours = _int(fields.get("hours"))
    if start_hour is not None and hours:
        request["start"] = f"{start_hour:02d}:00"
        request["end"] = f"{start_hour + hours:02d}:00" if start_hour + hours < 24 else "23:59"
    budget = fields.get("budget")
    if budget not in (None, ""):
        try:
            request["budget_cents"] = round(float(str(budget).replace("$", "")) * 100)
        except ValueError:
            request["budget_cents"] = -1
    return request


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "on", "checked"}


def _int(value: Any) -> int | None:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def missing_fields(request: dict[str, Any]) -> list[str]:
    missing = [field for field in REQUIRED if request.get(field) in (None, "", {})]
    if request.get("items", {}).get("posters") == 0:
        missing.append("poster quantity")
    return missing


def build_brief(request: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Validate a complete request and return the brief every specialist works from."""
    current = now or utc_now()
    missing = missing_fields(request)
    if missing:
        raise ValueError(f"Missing: {', '.join(missing)}")
    items = {str(key): int(value) for key, value in dict(request["items"]).items()}
    unknown = set(items) - set(CATEGORIES)
    if unknown:
        raise ValueError(f"Unsupported resource: {', '.join(sorted(unknown))}")
    if not 1 <= items.get("posters", 1) <= MAX_POSTERS:
        raise ValueError(f"Poster quantity must be between 1 and {MAX_POSTERS}")
    day = date.fromisoformat(str(request["day"]))
    start = datetime.combine(day, time.fromisoformat(str(request["start"])), CAMPUS_TZ)
    end = datetime.combine(day, time.fromisoformat(str(request["end"])), CAMPUS_TZ)
    hours = (end - start).total_seconds() / 3600
    if not 0.5 <= hours <= MAX_HOURS:
        raise ValueError(f"Bookings must last between 30 minutes and {MAX_HOURS} hours")
    if start <= current:
        raise ValueError("The booking window must start in the future")
    if (day - _local_today(current)).days > MAX_DAYS_AHEAD:
        raise ValueError(f"Bookings open at most {MAX_DAYS_AHEAD} days ahead")
    budget_cents = int(request["budget_cents"])
    if not 0 < budget_cents <= MAX_BUDGET_CENTS:
        raise ValueError("Budget must be greater than $0 and at most $500")
    priority = str(request.get("priority") or "cheapest")
    anchor = str(request.get("anchor") or "student_union")
    camera_type = str(request.get("camera_type") or "any")
    return {
        "items": items,
        "camera_type": camera_type if camera_type in CAMERA_TYPES else "any",
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "hours": round(hours, 2),
        "budget_cents": budget_cents,
        "priority": priority if priority in PRIORITIES else "cheapest",
        "anchor": anchor if anchor in ANCHORS else "student_union",
        "has_training": bool(request.get("has_training", False)),
        "notes": str(request.get("notes", ""))[:300],
    }


SAVED_PREFERENCES = ("priority", "anchor", "has_training")


def room_brief(fields: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """A study-room search at the student's own school. The Researcher sets the exact window."""
    current = now or utc_now()
    domain = _require_domain(fields, "so the Researcher can find your library")
    try:
        day = date.fromisoformat(str(fields.get("day", "")))
    except ValueError:
        raise ValueError("Missing: a day from the list") from None
    today = _local_today(current)
    if not today - timedelta(days=1) <= day <= today + timedelta(days=MAX_DAYS_AHEAD - 1):
        raise ValueError(f"Pick a day within the next {MAX_DAYS_AHEAD} days")
    window = str(fields.get("window") or "afternoon")
    duration = str(fields.get("duration") or "60")
    if window not in WINDOWS or duration not in ROOM_DURATIONS:
        raise ValueError("Missing: a time of day and a duration")
    group = _int(fields.get("group_size") or 1)
    if group is None or not 1 <= group <= MAX_GROUP:
        raise ValueError(f"People must be between 1 and {MAX_GROUP}")
    return {
        "service": "rooms",
        "school_domain": domain,
        "items": {"study_room": 1},
        "day": day.isoformat(),
        "window": window,
        "duration_minutes": int(duration),
        "hours": round(int(duration) / 60, 2),
        "group_size": group,
        "budget_cents": 0,
        "priority": "earliest",
        "camera_type": "any",
        "has_training": False,
        "notes": "",
    }


def housing_brief(fields: dict[str, Any]) -> dict[str, Any]:
    domain = _require_domain(fields, "so the Researcher can find your housing office")
    return {"service": "housing", "school_domain": domain, "items": {}}


def bundle_brief(fields: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """The labeled demo bundle, plus an optional free study room at the student's own school."""
    brief = build_brief(from_form(fields), now=now)
    brief["service"] = "bundle"
    if "study_room" in brief["items"]:
        brief["school_domain"] = _require_domain(fields, "to add a free study room")
        group = _int(fields.get("group_size") or 1)
        brief["group_size"] = group if group and 1 <= group <= MAX_GROUP else 1
    return brief


def intake_handler(payload: dict[str, Any]) -> dict[str, Any]:
    now = parse(str(payload["now"])) if payload.get("now") else None
    fields = dict(payload.get("fields") or {})
    service = str(payload.get("service") or fields.get("service") or "bundle")
    if service == "rooms":
        return {"brief": room_brief(fields, now=now)}
    if service == "housing":
        return {"brief": housing_brief(fields)}
    if service != "bundle":
        raise ValueError(f"Unknown service: {service}")
    # A form left on its defaults falls back to the student's saved bundle preferences.
    saved = dict(payload.get("saved_preferences") or {})
    for key in SAVED_PREFERENCES:
        if fields.get(key) in (None, "") and key in saved:
            fields[key] = saved[key]
    return {"brief": bundle_brief(fields, now=now)}


def brief_zone(brief: dict[str, Any]) -> ZoneInfo:
    """Rooms use the school's own timezone; the demo bundle uses the demo shops' campus."""
    return ZoneInfo(str(brief.get("timezone") or "America/Los_Angeles"))


def describe_window(brief: dict[str, Any]) -> str:
    zone = brief_zone(brief)
    start = parse(str(brief["window_start"])).astimezone(zone)
    end = parse(str(brief["window_end"])).astimezone(zone)
    return f"{start:%a %b %-d}, {start:%-I:%M %p}-{end:%-I:%M %p}".replace(":00", "")


def upcoming_days(now: datetime | None = None, count: int = 7) -> list[tuple[str, str]]:
    today = _local_today(now or utc_now())
    return [
        (
            (today + timedelta(days=offset)).isoformat(),
            f"{today + timedelta(days=offset):%a %b %-d}",
        )
        for offset in range(count)
    ]
