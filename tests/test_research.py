from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from provider_fakes import SEARCH_DAY, fake_http

from resourcepilot.http import ProviderError
from resourcepilot.research import (
    build_directory_index,
    find_housing_pages,
    find_room_system,
    lookup_school,
    recheck_link,
    research,
    room_window,
    school_domain,
    verify_booking_link,
)
from resourcepilot.store import MemoryStore

NOW = datetime.now(UTC)


def room_payload(domain: str, **brief: str) -> dict:
    return {
        "brief": {
            "service": "rooms",
            "school_domain": domain,
            "day": SEARCH_DAY.isoformat(),
            "window": "afternoon",
            "duration_minutes": 60,
            "group_size": 2,
            **brief,
        }
    }


@pytest.mark.parametrize(
    ("raw", "domain"),
    [
        ("pat@purdue.edu", "purdue.edu"),
        ("Pat.Lee+x@CS.Purdue.EDU", "cs.purdue.edu"),
        ("https://www.ucr.edu/about", "ucr.edu"),
    ],
)
def test_school_domain_never_keeps_the_local_part(raw: str, domain: str) -> None:
    assert school_domain(raw) == domain


@pytest.mark.parametrize("raw", ["", "pat@", "not an email", "pat@localhost", "@@x.edu"])
def test_school_domain_rejects_junk(raw: str) -> None:
    with pytest.raises(ValueError):
        school_domain(raw)


async def test_ipeds_matches_parent_domains_and_names_shared_systems() -> None:
    http = fake_http(MemoryStore())
    purdue = await lookup_school(http, "cs.purdue.edu")
    assert (purdue["domain"], purdue["unitid"], purdue["state"]) == ("purdue.edu", "243780", "IN")
    assert purdue["timezone"] == "America/Indiana/Indianapolis"
    mit = await lookup_school(http, "mit.edu")  # IPEDS lists web.mit.edu
    assert mit["school_name"] == "Massachusetts Institute of Technology"
    uw = await lookup_school(http, "uw.edu")  # two branch campuses share uw.edu
    assert (uw["school_name"], uw["unitid"]) == ("University of Washington", "")
    with pytest.raises(ValueError, match="not a US college"):
        await lookup_school(http, "gmail.com")


def test_directory_index_prefers_exact_hosts() -> None:
    index = build_directory_index(
        [
            {"unitid": 1, "inst_name": "Main", "url_school": "www.school.edu", "inst_size": 1},
            {
                "unitid": 2,
                "inst_name": "Big Branch",
                "url_school": "law.school.edu",
                "inst_size": 9,
            },
        ]
    )
    assert index["school.edu"]["unitid"] == 1
    assert index["law.school.edu"]["unitid"] == 2


async def test_ipeds_outage_fails_instead_of_guessing_a_timezone() -> None:
    calls: list[str] = []
    http = fake_http(failing={"educationdata.urban.org"}, calls=calls)
    with pytest.raises(ProviderError, match="IPEDS"):
        await lookup_school(http, "purdue.edu")
    assert len(calls) == 2  # one retry


async def test_purdue_style_custom_domain_libcal_is_found_by_crawl() -> None:
    found = await find_room_system(fake_http(), "purdue.edu")
    assert found["status"] == "found"
    assert (found["system"], found["host"], found["found_by"]) == (
        "libcal",
        "calendar.lib.purdue.edu",
        "crawl",
    )
    assert found["rooms_listed"] == "3"  # every LibCal space; Campus Rooms filters equipment


async def test_ucr_style_slug_probe_needs_the_site_to_mention_the_school() -> None:
    found = await find_room_system(fake_http(), "ucr.edu")
    assert (found["host"], found["found_by"]) == ("ucr.libcal.com", "libcal probe")


async def test_registry_fast_path_needs_no_fetch() -> None:
    calls: list[str] = []
    found = await find_room_system(fake_http(calls=calls), "ucla.edu")
    assert (found["host"], found["found_by"]) == ("calendar.library.ucla.edu", "checked list")
    assert calls == []


async def test_mit_style_miss_asks_for_a_link() -> None:
    found = await find_room_system(fake_http(), "mit.edu")
    assert found["status"] == "needs_link"
    assert found["booking_url"] == ""


async def test_results_are_cached_per_domain() -> None:
    cache, calls = MemoryStore(), []
    http = fake_http(cache, calls=calls)
    await find_room_system(http, "purdue.edu")
    before = len(calls)
    again = await find_room_system(http, "purdue.edu")
    assert again["host"] == "calendar.lib.purdue.edu"
    assert len(calls) == before


async def test_pasted_link_is_checked_not_trusted() -> None:
    http = fake_http()
    page = await verify_booking_link(
        http, "mit.edu", "https://libraries.mit.edu/study/reserve-a-room"
    )
    assert (page["system"], page["found_by"]) == ("page", "student link")
    libcal = await verify_booking_link(
        http, "purdue.edu", "https://calendar.lib.purdue.edu/reserve/spaces"
    )
    assert (libcal["system"], libcal["host"]) == ("libcal", "calendar.lib.purdue.edu")


@pytest.mark.parametrize(
    ("domain", "link", "message"),
    [
        ("mit.edu", "http://libraries.mit.edu/study/reserve-a-room", "https link"),
        ("mit.edu", "https://evil.example.com/rooms", "https link"),
        ("mit.edu", "https://libraries.mit.edu:8443/rooms", "https link"),
        ("mit.edu", "https://user@libraries.mit.edu/rooms", "https link"),
        ("mit.edu", "https://libraries.mit.edu/moved", "did not open"),  # off-domain redirect
        ("purdue.edu", "https://intranet.purdue.edu/rooms", "did not open"),  # private address
        ("mit.edu", "https://libraries.mit.edu/missing", "did not open"),
    ],
)
async def test_unsafe_or_dead_links_are_refused(domain: str, link: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        await verify_booking_link(fake_http(), domain, link)


async def test_housing_portal_skips_admissions_and_maintenance_links() -> None:
    pages = await find_housing_pages(fake_http(), "purdue.edu")
    assert pages == {
        "office_url": "https://housing.purdue.edu/",
        "portal_url": "https://purdue.starrezhousing.com/StarRezPortalX/",
        "portal_label": "My Housing portal",
    }
    assert await recheck_link(fake_http(), pages["portal_url"], "purdue.edu")
    assert not await recheck_link(fake_http(), "https://housing.purdue.edu/gone", "purdue.edu")
    assert not await recheck_link(fake_http(), "https://evil.example.com/", "purdue.edu")


async def test_housing_research_reports_ipeds_facts() -> None:
    payload = {"brief": {"service": "housing", "school_domain": "purdue.edu", "items": {}}}
    out = await research(payload, fake_http(), now=NOW)
    facts = dict(out["housing"]["facts"])
    assert facts["Dorm beds"] == "16,056"
    assert facts["Room"] == "$5,970 per year (typical)"
    assert facts["Meal plan"] == "Offered, up to 19 meals a week"
    assert out["housing"]["facts_source"].startswith("IPEDS 2022")


async def test_unreported_ipeds_values_say_so() -> None:
    payload = {"brief": {"service": "housing", "school_domain": "mit.edu", "items": {}}}
    out = await research(payload, fake_http(), now=NOW)
    facts = dict(out["housing"]["facts"])
    assert facts["Dorm beds"] == "Not reported"
    assert facts["Meal plan"] == "Not offered"


async def test_room_research_sets_the_school_window_and_campus_context() -> None:
    out = await research(room_payload("purdue.edu"), fake_http(), now=NOW)
    zone = ZoneInfo("America/Indiana/Indianapolis")
    start = datetime.fromisoformat(out["brief"]["window_start"]).astimezone(zone)
    assert (start.date(), start.hour) == (SEARCH_DAY, 12)
    assert out["campus"]["libcal_host"] == "calendar.lib.purdue.edu"
    assert out["campus"]["duration_minutes"] == "60"
    assert all(isinstance(value, str) for value in out["campus"].values())
    missing = await research(room_payload("mit.edu"), fake_http(), now=NOW)
    assert missing["needs_link"] is True
    assert "campus" not in missing


def test_room_window_rounds_up_once_the_window_has_started() -> None:
    zone = "America/Indiana/Indianapolis"
    now = datetime(2026, 10, 22, 13, 10, tzinfo=ZoneInfo(zone))
    start, end = room_window("2026-10-22", "afternoon", zone, now=now)
    assert datetime.fromisoformat(start) == now.replace(minute=30)
    assert datetime.fromisoformat(end).hour == 17
    late = now + timedelta(hours=5)
    start, end = room_window("2026-10-22", "afternoon", zone, now=late)
    assert start >= end
