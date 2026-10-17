from __future__ import annotations

from datetime import datetime

import pytest

from resourcepilot.clock import CAMPUS_TZ
from resourcepilot.intake import (
    build_brief,
    describe_window,
    from_form,
    intake_handler,
    missing_fields,
    parse_request_text,
    upcoming_days,
)

# Wednesday Oct 21 2026, 10 AM campus time.
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=CAMPUS_TZ)
EXAMPLE = "I need a DSLR, a podcast room, and 20 color posters Friday from 3-6 PM, under $35"


def test_example_sentence_parses_into_a_complete_request() -> None:
    request = parse_request_text(EXAMPLE, now=NOW)
    assert request["items"] == {"camera": 1, "podcast_room": 1, "posters": 20}
    assert request["camera_type"] == "dslr"
    assert request["day"] == "2026-10-23"
    assert (request["start"], request["end"]) == ("15:00", "18:00")
    assert request["budget_cents"] == 3500
    assert missing_fields(request) == []


def test_brief_uses_campus_local_window_and_defaults() -> None:
    brief = build_brief(parse_request_text(EXAMPLE, now=NOW), now=NOW)
    assert brief["window_start"] == "2026-10-23T15:00:00-07:00"
    assert brief["hours"] == 3
    assert brief["priority"] == "cheapest"
    assert brief["anchor"] == "student_union"
    assert brief["has_training"] is False
    assert describe_window(brief) == "Fri Oct 23, 3 PM-6 PM"


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("tomorrow 11-1 pm", "11:00", "13:00"),
        ("tomorrow 3 to 5", "15:00", "17:00"),
        ("tomorrow 9:30am-11am", "09:30", "11:00"),
        ("on 2026-10-24 from 2pm until 4pm", "14:00", "16:00"),
    ],
)
def test_time_ranges(text: str, start: str, end: str) -> None:
    request = parse_request_text(text, now=NOW)
    assert (request["start"], request["end"]) == (start, end)


def test_priority_anchor_training_and_other_items() -> None:
    request = parse_request_text(
        "Cheapest study room and a mic near the library tomorrow 1-3pm, max $20. I have training.",
        now=NOW,
    )
    assert request["items"] == {"microphone": 1, "study_room": 1}
    assert request["priority"] == "cheapest"
    assert request["anchor"] == "main_library"
    assert request["has_training"] is True
    assert request["budget_cents"] == 2000


def test_incomplete_sentence_reports_missing_fields() -> None:
    request = parse_request_text("I want some posters", now=NOW)
    assert set(missing_fields(request)) == {
        "day",
        "start",
        "end",
        "budget_cents",
        "poster quantity",
    }
    with pytest.raises(ValueError, match="Missing"):
        build_brief(request, now=NOW)


def test_form_fields_build_the_same_brief() -> None:
    fields = {
        "camera": "dslr",
        "room": "podcast_room",
        "posters": "20",
        "day": "2026-10-23",
        "start_hour": "15",
        "hours": "3",
        "budget": "35",
        "priority": "closest",
        "anchor": "north_dorms",
        "has_training": "true",
    }
    brief = build_brief(from_form(fields), now=NOW)
    assert brief["items"] == {"camera": 1, "podcast_room": 1, "posters": 20}
    assert brief["priority"] == "closest"
    assert brief["has_training"] is True
    assert intake_handler({"fields": fields, "now": NOW.isoformat()})["brief"] == brief


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("DSLR yesterday... no, today 8-9 am under $10", "future"),
        ("DSLR tomorrow 3-6pm under $900", "Budget"),
        ("DSLR tomorrow 9am-9pm under $30", "between 30 minutes"),
        ("600 posters tomorrow 3-4pm under $30", "Poster quantity"),
    ],
)
def test_brief_validation(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_brief(parse_request_text(text, now=NOW), now=NOW)


def test_upcoming_days_are_local_select_options() -> None:
    days = upcoming_days(NOW, 3)
    assert days[0] == ("2026-10-21", "Wed Oct 21")
    assert len(days) == 3
