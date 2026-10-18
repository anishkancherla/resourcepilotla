"""Turn a submitted form into a validated brief for one campus service: rooms, wellness, housing.

ResourcePilot takes requests only from cards and forms, never free text. Every brief is bounded
and deterministic, and a missing or invalid field raises a ``ValueError`` the Orchestrator shows
with the form again. A school email is reduced to its domain before the request is stored; the
part before ``@`` is never kept, logged, or sent to another agent.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from resourcepilot.clock import CAMPUS_TZ, parse, utc_now
from resourcepilot.research import DEFAULT_TIMEZONE, WINDOWS, school_domain

SERVICES = ("rooms", "wellness", "housing")
ROOM_DURATIONS = {"30": "30 minutes", "60": "1 hour", "90": "90 minutes", "120": "2 hours"}
WELLNESS_DURATIONS = {key: ROOM_DURATIONS[key] for key in ("30", "60", "90")}
MAX_GROUP = 12
MAX_DAYS_AHEAD = 14
# Housing criteria. Each one only narrows what the school's own pages say; nothing is inferred.
HOUSING_WHO = {
    "first_year": "Incoming first-year",
    "transfer": "Transfer student",
    "continuing": "Continuing student",
    "graduate": "Graduate student",
    "family": "Student with family",
}
HOUSING_STYLES = {
    "any": "Any style",
    "traditional": "Traditional hall",
    "suite": "Suite",
    "apartment": "Apartment",
}
HOUSING_FEATURES = {
    "ac": "Air conditioning",
    "kitchen": "Kitchen",
    "private_bath": "Private or suite bath",
    "gender_inclusive": "Gender-inclusive option",
    "accessible": "Accessible rooms",
}
MAX_HOUSING_BUDGET = 40_000
EMAIL_FIELDS = ("email", "school_email")


def _local_today(now: datetime) -> date:
    return now.astimezone(CAMPUS_TZ).date()


def _int(value: Any) -> int | None:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


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


def room_brief(
    fields: dict[str, Any], *, now: datetime | None = None, service: str = "rooms"
) -> dict[str, Any]:
    """A study- or wellness-room search at the student's own school.

    The Researcher sets the exact window. A wellness search is for one person and never asks
    why: no health details are collected.
    """
    current = now or utc_now()
    wellness = service == "wellness"
    domain = _require_domain(
        fields,
        "so the Researcher can find your school's wellness rooms and events"
        if wellness
        else "so the Researcher can find your library",
    )
    try:
        day = date.fromisoformat(str(fields.get("day", "")))
    except ValueError:
        raise ValueError("Missing: a day from the list") from None
    today = _local_today(current)
    if not today - timedelta(days=1) <= day <= today + timedelta(days=MAX_DAYS_AHEAD - 1):
        raise ValueError(f"Pick a day within the next {MAX_DAYS_AHEAD} days")
    window = str(fields.get("window") or "afternoon")
    duration = str(fields.get("duration") or ("30" if wellness else "60"))
    durations = WELLNESS_DURATIONS if wellness else ROOM_DURATIONS
    if window not in WINDOWS or duration not in durations:
        raise ValueError("Missing: a time of day and a duration")
    group = 1 if wellness else _int(fields.get("group_size") or 1)
    if group is None or not 1 <= group <= MAX_GROUP:
        raise ValueError(f"People must be between 1 and {MAX_GROUP}")
    return {
        "service": service,
        "school_domain": domain,
        "day": day.isoformat(),
        "window": window,
        "duration_minutes": int(duration),
        "hours": round(int(duration) / 60, 2),
        "group_size": group,
    }


def _checked(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "on", "checked"}


def housing_brief(fields: dict[str, Any]) -> dict[str, Any]:
    """The school plus the student's housing criteria. Nothing here is about the student's health,
    identity, or money beyond an optional yearly budget."""
    domain = _require_domain(fields, "so the Researcher can find your housing office")
    who = str(fields.get("who") or "first_year")
    style = str(fields.get("style") or "any")
    if who not in HOUSING_WHO:
        raise ValueError("Missing: who the housing is for")
    if style not in HOUSING_STYLES:
        raise ValueError("Missing: a housing style")
    budget_cents = 0
    raw = str(fields.get("max_budget") or "").replace("$", "").replace(",", "").strip()
    if raw:
        dollars = _int(raw)
        if dollars is None or not 0 < dollars <= MAX_HOUSING_BUDGET:
            raise ValueError(f"Budget must be between $1 and ${MAX_HOUSING_BUDGET:,} a year")
        budget_cents = dollars * 100
    return {
        "service": "housing",
        "school_domain": domain,
        "who": who,
        "style": style,
        "must_haves": [key for key in HOUSING_FEATURES if _checked(fields.get(f"need_{key}"))],
        "max_budget_cents": budget_cents,
    }


def intake_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a submitted form for its service; any other service is refused."""
    now = parse(str(payload["now"])) if payload.get("now") else None
    fields = dict(payload.get("fields") or {})
    service = str(payload.get("service") or fields.get("service") or "")
    if service in ("rooms", "wellness"):
        return {"brief": room_brief(fields, now=now, service=service)}
    if service == "housing":
        return {"brief": housing_brief(fields)}
    raise ValueError(f"Unknown service: {service or 'none'}")


def brief_zone(brief: dict[str, Any]) -> ZoneInfo:
    """The school's own timezone once the Researcher has set it; Pacific time until then."""
    return ZoneInfo(str(brief.get("timezone") or DEFAULT_TIMEZONE))


def describe_window(brief: dict[str, Any]) -> str:
    """The search window in the school's local time, for example ``Thu Oct 22, 12 PM-5 PM``."""
    zone = brief_zone(brief)
    start = parse(str(brief["window_start"])).astimezone(zone)
    end = parse(str(brief["window_end"])).astimezone(zone)
    return f"{start:%a %b %-d}, {start:%-I:%M %p}-{end:%-I:%M %p}".replace(":00", "")


def upcoming_days(now: datetime | None = None, count: int = 7) -> list[tuple[str, str]]:
    """``(day, label)`` select options starting today, campus-local."""
    today = _local_today(now or utc_now())
    return [
        (
            (today + timedelta(days=offset)).isoformat(),
            f"{today + timedelta(days=offset):%a %b %-d}",
        )
        for offset in range(count)
    ]
