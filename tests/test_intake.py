from __future__ import annotations

from datetime import datetime

import pytest

from resourcepilot.clock import CAMPUS_TZ
from resourcepilot.intake import (
    build_brief,
    bundle_brief,
    describe_window,
    from_form,
    housing_brief,
    intake_handler,
    missing_fields,
    room_brief,
    scrub_fields,
    upcoming_days,
)

# Wednesday Oct 21 2026, 10 AM campus time.
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=CAMPUS_TZ)
BUNDLE = {
    "camera": "dslr",
    "room": "podcast_room",
    "posters": "20",
    "day": "2026-10-23",
    "start_hour": "15",
    "hours": "3",
    "budget": "35",
    "priority": "cheapest",
    "anchor": "student_union",
}
ROOM = {"school_domain": "purdue.edu", "day": "2026-10-22", "window": "afternoon"}


def test_bundle_form_builds_a_campus_local_brief() -> None:
    brief = bundle_brief(BUNDLE, now=NOW)
    assert brief["service"] == "bundle"
    assert brief["items"] == {"camera": 1, "podcast_room": 1, "posters": 20}
    assert brief["camera_type"] == "dslr"
    assert brief["window_start"] == "2026-10-23T15:00:00-07:00"
    assert brief["hours"] == 3
    assert brief["budget_cents"] == 3500
    assert brief["has_training"] is False
    assert describe_window(brief) == "Fri Oct 23, 3 PM-6 PM"
    assert "school_domain" not in brief


def test_form_choices_carry_through() -> None:
    fields = {**BUNDLE, "priority": "closest", "anchor": "north_dorms", "has_training": "true"}
    brief = bundle_brief(fields, now=NOW)
    assert (brief["priority"], brief["anchor"], brief["has_training"]) == (
        "closest",
        "north_dorms",
        True,
    )
    payload = {"service": "bundle", "fields": fields, "now": NOW.isoformat()}
    assert intake_handler(payload)["brief"] == brief


def test_saved_preferences_fill_only_blank_choices() -> None:
    fields = {**BUNDLE, "priority": "", "anchor": "main_library"}
    saved = {"priority": "best_rated", "anchor": "north_dorms"}
    brief = intake_handler(
        {"service": "bundle", "fields": fields, "saved_preferences": saved, "now": NOW.isoformat()}
    )["brief"]
    assert brief["priority"] == "best_rated"
    assert brief["anchor"] == "main_library"


def test_incomplete_form_reports_missing_fields() -> None:
    request = from_form({"posters": "0"})
    assert set(missing_fields(request)) >= {"day", "start", "end", "budget_cents"}
    with pytest.raises(ValueError, match="Missing"):
        build_brief(request, now=NOW)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"day": "2026-10-21", "start_hour": "8", "hours": "1"}, "future"),
        ({"budget": "900"}, "Budget"),
        ({"start_hour": "9", "hours": "12"}, "between 30 minutes"),
        ({"posters": "600"}, "Poster quantity"),
    ],
)
def test_bundle_validation(overrides: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        bundle_brief({**BUNDLE, **overrides}, now=NOW)


def test_free_study_room_in_a_bundle_needs_a_school_email() -> None:
    fields = {**BUNDLE, "room": "study_room"}
    with pytest.raises(ValueError, match="school email"):
        bundle_brief(fields, now=NOW)
    brief = bundle_brief(scrub_fields({**fields, "email": "pat@purdue.edu"}), now=NOW)
    assert brief["school_domain"] == "purdue.edu"
    assert brief["items"]["study_room"] == 1


def test_email_is_reduced_to_its_school_domain_before_anything_else() -> None:
    # The Researcher maps a department subdomain to its institution; the name never travels.
    clean = scrub_fields({"email": "Pat.Lee+rooms@CS.Purdue.edu", "day": "2026-10-22"})
    assert clean == {"school_domain": "cs.purdue.edu", "day": "2026-10-22"}
    bad = scrub_fields({"email": "not an email"})
    assert bad == {"school_domain": "", "email_invalid": True}
    with pytest.raises(ValueError, match="valid school email"):
        housing_brief(bad)


def test_room_brief_is_free_and_leaves_the_window_to_the_researcher() -> None:
    brief = room_brief({**ROOM, "duration": "90", "group_size": "4"}, now=NOW)
    assert brief["service"] == "rooms"
    assert brief["items"] == {"study_room": 1}
    assert brief["budget_cents"] == 0
    assert (brief["duration_minutes"], brief["group_size"]) == (90, 4)
    assert "window_start" not in brief


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"school_domain": ""}, "school email"),
        ({"day": "2026-11-30"}, "within the next"),
        ({"window": "midnight"}, "time of day"),
        ({"group_size": "40"}, "People"),
    ],
)
def test_room_validation(overrides: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        room_brief({**ROOM, **overrides}, now=NOW)


def test_housing_needs_only_the_school() -> None:
    assert housing_brief({"school_domain": "purdue.edu"}) == {
        "service": "housing",
        "school_domain": "purdue.edu",
        "items": {},
    }
    with pytest.raises(ValueError, match="Unknown service"):
        intake_handler({"service": "pharmacy", "fields": {}})


def test_upcoming_days_are_local_select_options() -> None:
    days = upcoming_days(NOW, 3)
    assert days[0] == ("2026-10-21", "Wed Oct 21")
    assert len(days) == 3
