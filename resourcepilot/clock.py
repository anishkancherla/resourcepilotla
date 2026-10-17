from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

# Times shown to people and seller opening hours are campus-local.
CAMPUS_TZ = ZoneInfo("America/Los_Angeles")


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {value}")
    return parsed


def later(seconds: float, *, now: datetime | None = None) -> str:
    return ((now or utc_now()) + timedelta(seconds=seconds)).isoformat()


def hours_between(start: str, end: str) -> float:
    return (parse(end) - parse(start)).total_seconds() / 3600
