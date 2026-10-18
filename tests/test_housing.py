"""On-campus housing: reading halls from a school's own pages, and matching them to criteria."""

from __future__ import annotations

import json

import pytest
from provider_fakes import fake_http

from resourcepilot.housing import find_housing_options, normalize, recheck_halls
from resourcepilot.intake import housing_brief
from resourcepilot.planning import housing_checks, plan_housing
from resourcepilot.store import MemoryStore

OFFICE = "https://housing.purdue.edu/"


async def purdue_halls() -> list[dict]:
    found = await find_housing_options(fake_http(), "purdue.edu", OFFICE)
    assert found["status"] == "found"
    return found["options"]


async def test_halls_are_read_from_the_schools_own_pages_and_noise_is_skipped() -> None:
    halls = {hall["name"]: hall for hall in await purdue_halls()}
    # Association, winter break, faculty housing, the home link, and another school are skipped;
    # a hall listed twice (once with #photos) appears once.
    assert list(halls) == ["Hillenbrand Hall", "Earhart", "Meredith South"]
    hillenbrand = halls["Hillenbrand Hall"]
    assert hillenbrand["style"] == "suite"
    assert hillenbrand["who"] == ["continuing"]
    assert hillenbrand["rooms"] == ["single", "double"]
    assert hillenbrand["features"] == ["kitchen", "private_bath"]
    assert (hillenbrand["price_label"], hillenbrand["yearly_cents"]) == (
        "$4,512 per semester",
        902400,
    )
    assert hillenbrand["photo"] == "https://housing.purdue.edu/images/hillenbrand.jpg"


async def test_a_generic_heading_never_replaces_the_halls_own_name() -> None:
    halls = {hall["name"]: hall for hall in await purdue_halls()}
    earhart = halls["Earhart"]  # its page heading is "Welcome Boilermakers!"
    assert (earhart["style"], earhart["who"], earhart["features"]) == (
        "traditional",
        ["first_year"],
        ["ac"],
    )
    assert earhart["price_label"] == "" and earhart["yearly_cents"] == 0


async def test_menus_photos_and_injected_text_are_not_trusted() -> None:
    halls = {hall["name"]: hall for hall in await purdue_halls()}
    meredith = halls["Meredith South"]
    # Menus name every group and feature; only the page's main content counts.
    assert "gender_inclusive" not in str(halls)
    assert meredith["features"] == []
    assert meredith["who"] == ["graduate", "family"]
    assert meredith["photo"] == ""  # off the school's domain
    assert (meredith["price_label"], meredith["yearly_cents"]) == ("$1,350 per month", 1215000)
    text = json.dumps(list(halls.values()))
    assert "attacker" not in text and "Ignore all previous" not in text


async def test_a_school_without_readable_halls_says_so() -> None:
    found = await find_housing_options(fake_http(), "mit.edu", "")
    assert (found["status"], found["options"]) == ("not_found", [])
    assert "mit.edu" in found["evidence"]


async def test_results_are_cached_per_school() -> None:
    cache = MemoryStore()
    calls: list[str] = []
    await find_housing_options(fake_http(cache, calls=calls), "purdue.edu", OFFICE)
    before = len(calls)
    await find_housing_options(fake_http(cache, calls=calls), "purdue.edu", OFFICE)
    assert len(calls) == before


async def test_verification_drops_halls_whose_page_no_longer_opens() -> None:
    halls = await purdue_halls()
    gone = {**halls[0], "name": "Gone Hall", "url": "https://housing.purdue.edu/gone.html"}
    kept, dropped = await recheck_halls(fake_http(), [*halls, gone], "purdue.edu")
    assert [hall["name"] for hall in kept] == [hall["name"] for hall in halls]
    assert dropped == ["Gone Hall"]


def test_urls_are_normalized_so_a_hall_is_listed_once() -> None:
    assert normalize("https://www.housing.x.edu/a/b/#top") == "https://housing.x.edu/a/b"
    assert normalize("https://housing.x.edu/a/b") == "https://housing.x.edu/a/b"


def hall(name: str, **fields) -> dict:
    return {
        "option_id": f"hall_{name}",
        "name": name,
        "style": "",
        "who": [],
        "features": [],
        "yearly_cents": 0,
        **fields,
    }


HALLS = [
    hall("Apartments", style="apartment", who=["continuing"], features=["kitchen"],
         yearly_cents=900000),
    hall("First-year Hall", style="traditional", who=["first_year"], features=["ac"],
         yearly_cents=700000),
    hall("Suite Hall", style="suite", features=["ac", "kitchen"], yearly_cents=1100000),
    hall("Mystery Hall"),
]  # fmt: skip


def brief(**fields) -> dict:
    return housing_brief({"school_domain": "purdue.edu", **fields})


def test_each_criterion_is_match_unknown_or_ruled_out() -> None:
    criteria = brief(who="first_year", style="traditional", need_ac="true", max_budget="8000")
    assert housing_checks(criteria, HALLS[1]) == {
        "who": "match",
        "style": "match",
        "ac": "match",
        "budget": "match",
    }
    assert housing_checks(criteria, HALLS[3]) == {
        "who": "unknown",
        "style": "unknown",
        "ac": "unknown",  # pages rarely say what a hall lacks
        "budget": "unknown",
    }
    assert housing_checks(criteria, HALLS[0])["who"] == "out"
    assert housing_checks(criteria, HALLS[2])["budget"] == "out"


def test_transfer_students_fit_continuing_halls() -> None:
    assert housing_checks(brief(who="transfer"), HALLS[0])["who"] == "match"


def test_matches_come_first_and_ruled_out_halls_are_hidden() -> None:
    plan = plan_housing(brief(who="first_year", need_ac="on"), HALLS)
    assert [h["name"] for h in plan["halls"]] == ["First-year Hall", "Suite Hall", "Mystery Hall"]
    assert plan["halls"][0]["unknown"] == [] and plan["halls"][0]["ruled_out"] == []
    assert plan["hidden_count"] == 1
    assert [h["name"] for h in plan["all_halls"]][-1] == "Apartments"
    assert plan["all_halls"][-1]["ruled_out"] == ["who"]


def test_cheaper_halls_rank_first_among_equal_matches() -> None:
    plan = plan_housing({"style": "any"}, HALLS)  # no criteria set, so price decides
    assert [h["name"] for h in plan["halls"]] == [
        "First-year Hall",
        "Apartments",
        "Suite Hall",
        "Mystery Hall",
    ]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"who": "alien"}, "who the housing is for"),
        ({"style": "castle"}, "housing style"),
        ({"max_budget": "999999"}, "Budget"),
        ({"max_budget": "-5"}, "Budget"),
    ],
)
def test_housing_criteria_are_validated(fields, message) -> None:
    with pytest.raises(ValueError, match=message):
        brief(**fields)


def test_housing_brief_defaults_and_must_haves() -> None:
    criteria = brief(need_kitchen="true", need_ac="false", max_budget="$12,500", day="ignored")
    assert criteria == {
        "service": "housing",
        "school_domain": "purdue.edu",
        "who": "first_year",
        "style": "any",
        "must_haves": ["kitchen"],
        "max_budget_cents": 1250000,
    }
