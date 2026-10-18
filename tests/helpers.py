"""Build study-room briefs for specialist and card tests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from resourcepilot.clock import CAMPUS_TZ
from resourcepilot.intake import room_brief
from resourcepilot.research import room_window

# Wednesday Oct 21 2026, 10 AM campus time.
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=CAMPUS_TZ)
SCHOOL_TIMEZONE = "America/Indiana/Indianapolis"
# The rooms form as a Purdue student submits it, after the email is reduced to its domain.
ROOM_FIELDS = {
    "school_domain": "purdue.edu",
    "day": "2026-10-22",
    "window": "afternoon",
    "duration": "60",
    "group_size": "2",
}


def brief(fields: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    """A rooms brief after Intake and the Researcher: Thu Oct 22, 12 PM-5 PM Eastern."""
    selected = room_brief({**ROOM_FIELDS, **(fields or {})}, now=NOW)
    start, end = room_window(selected["day"], selected["window"], SCHOOL_TIMEZONE, now=NOW)
    selected.update(window_start=start, window_end=end, timezone=SCHOOL_TIMEZONE)
    return {**selected, **overrides}
