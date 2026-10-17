"""Turn a sentence or a submitted form into a validated booking brief.

The parser is deterministic and bounded on purpose: it extracts only the fields ResourcePilot
can act on and reports what is missing, so the Orchestrator can ask with a form instead of
guessing.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any

from resourcepilot.clock import CAMPUS_TZ, parse, utc_now
from resourcepilot.registry import CATEGORIES

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
PRIORITIES = {"cheapest": "Lowest price", "closest": "Shortest walk", "best_rated": "Best rated"}
ANCHORS: dict[str, tuple[str, float, float]] = {
    "student_union": ("Student union", 34.0705, -118.4445),
    "main_library": ("Main library", 34.0716, -118.4422),
    "north_dorms": ("North dorms", 34.0729, -118.4520),
}
CAMERA_TYPES = ("any", "dslr", "mirrorless")
MAX_POSTERS = 500
MAX_HOURS = 8
MAX_BUDGET_CENTS = 50_000
MAX_DAYS_AHEAD = 14
REQUIRED = ("items", "day", "start", "end", "budget_cents")

_TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?"
# Hyphen, en dash, or em dash between the two times.
_RANGE = re.compile(_TIME + "\\s*(?:-|\u2013|\u2014|to|until|till)\\s*" + _TIME, re.IGNORECASE)
_BUDGET = re.compile(
    r"(?:under|below|less than|max(?:imum)?|budget(?: of| is)?|up to|at most)\s*\$?\s*"
    r"(\d+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_DOLLARS = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)")
_POSTERS = re.compile(r"(\d+)\s*(?:color\s+|colour\s+)?(?:posters?|prints?|flyers?)", re.I)


def _local_today(now: datetime) -> date:
    return now.astimezone(CAMPUS_TZ).date()


def _hour(raw_hour: str, raw_minute: str | None, meridiem: str | None) -> tuple[int, int]:
    hour, minute = int(raw_hour), int(raw_minute or 0)
    marker = (meridiem or "").replace(".", "").lower()
    if marker == "pm" and hour < 12:
        hour += 12
    if marker == "am" and hour == 12:
        hour = 0
    return hour, minute


def _window_from_match(match: re.Match[str]) -> tuple[time, time] | None:
    sh, sm, smer, eh, em, emer = match.groups()
    if not smer and emer:
        # "3-6 PM" means both ends are PM unless that would put the start after the end.
        start_pm = _hour(sh, sm, emer)
        smer = emer if start_pm[0] < _hour(eh, em, emer)[0] else None
    start_hour, start_minute = _hour(sh, sm, smer)
    end_hour, end_minute = _hour(eh, em, emer)
    if not smer and not emer and start_hour < 8:
        # Bare "3-6" during the day almost always means the afternoon.
        start_hour, end_hour = start_hour + 12, end_hour + 12
    if not (0 <= start_hour < 24 and 0 <= end_hour < 24 and start_minute < 60 > end_minute):
        return None
    return time(start_hour, start_minute), time(end_hour, end_minute)


def _parse_window(text: str) -> tuple[time, time] | None:
    # Skip ISO dates such as 2026-10-23 so their digits are not read as a time range.
    cleaned = re.sub(r"\b20\d\d-\d\d-\d\d\b", " ", text)
    for match in _RANGE.finditer(cleaned):
        if window := _window_from_match(match):
            return window
    return None


def _parse_day(text: str, today: date) -> date | None:
    lowered = text.lower()
    if "tomorrow" in lowered:
        return today + timedelta(days=1)
    if "today" in lowered or "tonight" in lowered:
        return today
    for index, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name}\b|\b{name[:3]}\b", lowered):
            return today + timedelta(days=(index - today.weekday()) % 7)
    match = re.search(r"\b(20\d\d-\d\d-\d\d)\b", lowered)
    return date.fromisoformat(match.group(1)) if match else None


def parse_request_text(text: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Extract a partial request from free text; missing fields are simply absent."""
    lowered = f" {text.lower()} "
    current = now or utc_now()
    found: dict[str, Any] = {"items": {}, "camera_type": "any", "notes": text.strip()[:300]}
    items: dict[str, int] = found["items"]
    if "dslr" in lowered:
        items["camera"], found["camera_type"] = 1, "dslr"
    elif "mirrorless" in lowered:
        items["camera"], found["camera_type"] = 1, "mirrorless"
    elif re.search(r"\bcamera\b", lowered):
        items["camera"] = 1
    if re.search(r"\bmic(rophone)?s?\b", lowered):
        items["microphone"] = 1
    if "tripod" in lowered:
        items["tripod"] = 1
    if re.search(r"podcast\s*(room|studio|booth)?|recording (room|studio)", lowered):
        items["podcast_room"] = 1
    elif re.search(r"study\s*room|meeting room", lowered):
        items["study_room"] = 1
    if poster := _POSTERS.search(lowered):
        items["posters"] = int(poster.group(1))
    elif re.search(r"\bposters?\b", lowered):
        items["posters"] = 0

    if budget := (_BUDGET.search(lowered) or _DOLLARS.search(lowered)):
        found["budget_cents"] = round(float(budget.group(1)) * 100)
    if day := _parse_day(lowered, _local_today(current)):
        found["day"] = day.isoformat()
    if window := _parse_window(lowered):
        found["start"], found["end"] = window[0].strftime("%H:%M"), window[1].strftime("%H:%M")
    if re.search(r"\b(trained|have training|completed training|training done)\b", lowered):
        found["has_training"] = True
    if re.search(r"cheap|lowest price|save money", lowered):
        found["priority"] = "cheapest"
    elif re.search(r"closest|nearby|short walk|near me", lowered):
        found["priority"] = "closest"
    elif re.search(r"best rated|highest rated|best quality", lowered):
        found["priority"] = "best_rated"
    for key, (label, _, _) in ANCHORS.items():
        if label.split()[-1].lower().rstrip("s") in lowered:
            found["anchor"] = key
    if not items:
        found.pop("items")
    return found


def from_form(fields: dict[str, Any]) -> dict[str, Any]:
    """Map captured form fields onto the same partial request shape as free text."""
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


def intake_handler(payload: dict[str, Any]) -> dict[str, Any]:
    now = parse(str(payload["now"])) if payload.get("now") else None
    if isinstance(payload.get("fields"), dict):
        request = from_form(dict(payload["fields"]))
    else:
        request = parse_request_text(str(payload.get("text", "")), now=now)
        # A sentence rarely restates preferences, so fall back to the saved ones.
        saved = dict(payload.get("saved_preferences") or {})
        for key in SAVED_PREFERENCES:
            if key not in request and key in saved:
                request[key] = saved[key]
    return {"brief": build_brief(request, now=now)}


def describe_window(brief: dict[str, Any]) -> str:
    start = parse(str(brief["window_start"])).astimezone(CAMPUS_TZ)
    end = parse(str(brief["window_end"])).astimezone(CAMPUS_TZ)
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
