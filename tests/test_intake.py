from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from resourcepilot import intake
from resourcepilot.clock import CAMPUS_TZ
from resourcepilot.intake import (
    MAX_DAYS_AHEAD,
    MAX_GROUP,
    ROOM_DURATIONS,
    SERVICES,
    brief_zone,
    describe_window,
    housing_brief,
    intake_handler,
    room_brief,
    scrub_fields,
    upcoming_days,
)

# Wednesday Oct 21 2026, 10 AM campus time.
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=CAMPUS_TZ)
ROOM = {"school_domain": "purdue.edu", "day": "2026-10-22", "window": "afternoon"}


def test_only_rooms_wellness_and_housing_are_offered() -> None:
    assert SERVICES == ("rooms", "wellness", "housing")
    for removed in ("bundle_brief", "build_brief", "from_form", "SAVED_PREFERENCES", "ANCHORS"):
        assert not hasattr(intake, removed), removed


def test_room_brief_is_bounded_and_leaves_the_window_to_the_researcher() -> None:
    brief = room_brief({**ROOM, "duration": "90", "group_size": "4"}, now=NOW)
    assert brief == {
        "service": "rooms",
        "school_domain": "purdue.edu",
        "day": "2026-10-22",
        "window": "afternoon",
        "duration_minutes": 90,
        "hours": 1.5,
        "group_size": 4,
    }


def test_room_brief_defaults_to_one_person_for_an_hour_in_the_afternoon() -> None:
    brief = room_brief({"school_domain": "PURDUE.EDU", "day": "2026-10-21"}, now=NOW)
    assert (brief["school_domain"], brief["window"]) == ("purdue.edu", "afternoon")
    assert (brief["duration_minutes"], brief["hours"], brief["group_size"]) == (60, 1.0, 1)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"school_domain": ""}, "school email"),
        ({"email_invalid": True}, "valid school email"),
        ({"school_domain": "localhost"}, "school email"),
        ({"day": ""}, "a day from the list"),
        ({"day": "Friday"}, "a day from the list"),
        ({"day": "2026-10-19"}, "within the next"),
        ({"day": "2026-11-30"}, "within the next"),
        ({"window": "midnight"}, "time of day"),
        ({"duration": "45"}, "time of day and a duration"),
        ({"group_size": "40"}, "People"),
        ({"group_size": "0.5"}, "People"),
        ({"group_size": "many"}, "People"),
    ],
)
def test_room_validation(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        room_brief({**ROOM, **overrides}, now=NOW)


def test_room_day_range_matches_the_form() -> None:
    # Yesterday is still accepted (a late-evening search can straddle midnight), as is the last
    # day the form offers.
    assert room_brief({**ROOM, "day": "2026-10-20"}, now=NOW)["day"] == "2026-10-20"
    last = (NOW.date() + timedelta(days=MAX_DAYS_AHEAD - 1)).isoformat()
    assert room_brief({**ROOM, "day": last}, now=NOW)["day"] == last
    too_far = (NOW.date() + timedelta(days=MAX_DAYS_AHEAD)).isoformat()
    with pytest.raises(ValueError, match="within the next"):
        room_brief({**ROOM, "day": too_far}, now=NOW)
    assert room_brief({**ROOM, "group_size": str(MAX_GROUP)}, now=NOW)["group_size"] == MAX_GROUP
    for duration in ROOM_DURATIONS:
        assert room_brief({**ROOM, "duration": duration}, now=NOW)["duration_minutes"] == int(
            duration
        )


def test_housing_needs_only_the_school() -> None:
    assert housing_brief({"school_domain": "purdue.edu", "day": "ignored"}) == {
        "service": "housing",
        "school_domain": "purdue.edu",
    }
    with pytest.raises(ValueError, match="housing office"):
        housing_brief({})


def test_email_is_reduced_to_its_school_domain_before_anything_else() -> None:
    # The Researcher maps a department subdomain to its institution; the name never travels.
    clean = scrub_fields({"email": "Pat.Lee+rooms@CS.Purdue.edu", "day": "2026-10-22"})
    assert clean == {"school_domain": "cs.purdue.edu", "day": "2026-10-22"}
    assert "pat" not in str(clean).lower()
    assert scrub_fields({"school_email": "sam@mit.edu"}) == {"school_domain": "mit.edu"}
    assert scrub_fields({"day": "2026-10-22"}) == {"day": "2026-10-22"}
    bad = scrub_fields({"email": "not an email"})
    assert bad == {"school_domain": "", "email_invalid": True}
    with pytest.raises(ValueError, match="valid school email"):
        housing_brief(bad)


def test_handler_routes_by_service_and_never_keeps_the_local_part() -> None:
    fields = scrub_fields({**ROOM, "school_domain": "", "email": "pat@cs.purdue.edu"})
    payload = {"service": "rooms", "fields": fields, "now": NOW.isoformat()}
    brief = intake_handler(payload)["brief"]
    assert brief == room_brief(fields, now=NOW)
    assert brief["school_domain"] == "cs.purdue.edu"
    assert "pat" not in str(brief)
    housing = intake_handler({"fields": {"service": "housing", "school_domain": "purdue.edu"}})
    assert housing["brief"]["service"] == "housing"


@pytest.mark.parametrize("service", ["bundle", "pharmacy", ""])
def test_unknown_service_is_refused(service: str) -> None:
    with pytest.raises(ValueError, match="Unknown service"):
        intake_handler({"service": service, "fields": dict(ROOM), "now": NOW.isoformat()})


def test_window_is_described_in_the_schools_timezone() -> None:
    brief = {
        "window_start": "2026-10-22T12:00:00-04:00",
        "window_end": "2026-10-22T17:30:00-04:00",
        "timezone": "America/Indiana/Indianapolis",
    }
    assert str(brief_zone(brief)) == "America/Indiana/Indianapolis"
    assert describe_window(brief) == "Thu Oct 22, 12 PM-5:30 PM"
    pacific = {key: value for key, value in brief.items() if key != "timezone"}
    assert str(brief_zone(pacific)) == "America/Los_Angeles"
    assert describe_window(pacific) == "Thu Oct 22, 9 AM-2:30 PM"


def test_upcoming_days_are_local_select_options() -> None:
    days = upcoming_days(NOW, 3)
    assert days == [
        ("2026-10-21", "Wed Oct 21"),
        ("2026-10-22", "Thu Oct 22"),
        ("2026-10-23", "Fri Oct 23"),
    ]
    assert len(upcoming_days(NOW)) == 7


def test_wellness_brief_is_for_one_person_and_asks_nothing_about_health() -> None:
    brief = intake_handler(
        {"service": "wellness", "fields": {**ROOM, "group_size": "6"}, "now": NOW.isoformat()}
    )["brief"]
    assert brief == {
        "service": "wellness",
        "school_domain": "purdue.edu",
        "day": "2026-10-22",
        "window": "afternoon",
        "duration_minutes": 30,
        "hours": 0.5,
        "group_size": 1,
    }
    with pytest.raises(ValueError, match="duration"):
        room_brief({**ROOM, "duration": "120"}, now=NOW, service="wellness")
    with pytest.raises(ValueError, match="wellness rooms and events"):
        room_brief({**ROOM, "school_domain": ""}, now=NOW, service="wellness")
