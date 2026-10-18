"""On-campus housing: reading halls from a school's own pages, and matching them to criteria."""

from __future__ import annotations

import json

import pytest
from provider_fakes import fake_http

from resourcepilot.cards import hall_price, typical_text
from resourcepilot.housing import find_housing_options, normalize, recheck_halls
from resourcepilot.intake import housing_brief
from resourcepilot.planning import housing_checks, plan_housing
from resourcepilot.rates import price_halls, priced, read_rate_pdf, room_range
from resourcepilot.research import safe_page, typical_cost
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
    assert list(halls) == ["Hillenbrand Hall", "Earhart", "Meredith South", "Cary Quadrangle"]
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


RATES = "https://housing.purdue.edu/my-housing/info/housing-rates.html"


async def test_rates_table_prices_named_halls_and_skips_summer_and_fees() -> None:
    halls = {hall["name"]: hall for hall in await purdue_halls()}
    earhart = halls["Earhart"]
    # Two regular rows name Earhart ($3,919 and $3,762): the lowest wins. The $900 summer row,
    # the deposit row, and the fee table are not regular academic-year rates.
    assert (earhart["price_label"], earhart["yearly_cents"]) == ("$3,762 per semester", 752400)
    assert (earhart["price_source"], earhart["rates_url"]) == ("rates table", RATES)
    # A hall's own published price is never replaced by the rates page.
    assert halls["Hillenbrand Hall"]["price_source"] == "hall page"


async def test_rate_pdf_gives_the_room_price_range_without_dining() -> None:
    cary = {hall["name"]: hall for hall in await purdue_halls()}["Cary Quadrangle"]
    assert cary["price_label"] == "Room $12,708\u2013$17,649 a year"
    assert (cary["yearly_cents"], cary["price_high_cents"]) == (1270800, 1764900)
    assert cary["price_source"] == "rates PDF"
    assert cary["rates_url"] == "https://housing.purdue.edu/rates/cary.pdf"


async def test_room_type_rows_stay_school_wide() -> None:
    found = await find_housing_options(fake_http(), "purdue.edu", OFFICE)
    assert found["rates_url"] == RATES
    assert found["school_rates"] == [
        {"label": "Doubles", "price": "$3,900 per semester", "column": "Rate"}
    ]


async def test_unreadable_or_offsite_pdfs_never_give_a_number() -> None:
    http = fake_http()
    halls = [
        {"option_id": "h1", "name": "Hillenbrand Hall"},  # its schedule is a broken PDF
        {"option_id": "h2", "name": "Meredith South"},  # its schedule is on another domain
    ]
    from resourcepilot.research import Budget

    rates = await safe_page(http, RATES, "purdue.edu", Budget())
    assert rates is not None
    await price_halls(http, "purdue.edu", halls, [rates])
    assert "price_label" not in halls[0]
    assert halls[0]["rates_url"] == "https://housing.purdue.edu/rates/broken.pdf"
    assert "price_label" not in halls[1] and "rates_url" not in halls[1]
    assert read_rate_pdf(b"<html>not a pdf</html>") is None
    assert read_rate_pdf(b"%PDF-1.7 truncated") is None


def test_room_range_reads_only_the_room_columns_of_the_yearly_row() -> None:
    schedule = "\n".join(
        [
            "Glen Mor",
            "Single Single Scotty's Apartment Plan Flex Apartment Plan",
            "October 1, 2026 $1,565 $1,541 $100 $326",
            "ACADEMIC YEAR COST $15,650 $15,410 $900 $2,934 $6,786 $7,866 $69",
        ]
    )
    assert room_range(schedule) == (1541000, 1565000)
    # No room header, or no yearly row: no number rather than a guess.
    assert room_range("ANNUAL COST $17,649.00 $14,886.00") is None
    assert room_range("Single Double\nFall $2,018 $1,702") is None
    # A yearly row whose room amounts are not plausible yearly prices is not used.
    assert room_range("Single Double\nANNUAL COST $270 $69") is None


def test_unknown_periods_are_shown_as_listed_and_not_annualized() -> None:
    assert priced(397000, "") == (0, "$3,970 (period not stated)")
    assert priced(397000, "semester") == (794000, "$3,970 per semester")
    assert priced(135000, "month") == (1215000, "$1,350 per month")
    assert priced(10000, "semester") == (0, "")  # $100 a semester is a fee, not a room


def test_ipeds_typical_cost_prefers_room_then_room_and_board() -> None:
    assert typical_cost({"typical_room_charge": 6480}, 2023) == {
        "cents": 648000,
        "kind": "room",
        "year": 2023,
    }
    ucr = {"typical_room_charge": -1, "room_board_charge": 19433}
    assert typical_cost(ucr, 2023)["kind"] == "room and board"
    assert typical_cost({"typical_room_charge": -1}, 2023) == {}


def test_cards_label_every_price_with_where_it_came_from() -> None:
    typical = {"cents": 1943300, "kind": "room and board", "year": 2023}
    assert typical_text(typical) == "$19,433/yr room and board (IPEDS 2023)"
    assert hall_price({"price_label": "Room $12,708\u2013$17,649 a year", "yearly_cents": 1})
    assert (
        hall_price({"price_label": "$3,762 per semester", "yearly_cents": 752400})
        == "From $3,762 per semester (about $7,524 a year)"
    )
    assert hall_price({"price_label": "$3,970 (period not stated)"}) == (
        "$3,970 (period not stated)"
    )
    assert hall_price({}, typical) == (
        "Hall rate not published \u00b7 school typical $19,433/yr room and board (IPEDS 2023)"
    )
    assert hall_price({}) == "Hall rate not published online"


def test_budget_fits_when_the_cheapest_room_fits() -> None:
    criteria = brief(max_budget="13000")
    pdf_hall = hall("PDF Hall", yearly_cents=1270800, price_high_cents=1764900)
    assert housing_checks(criteria, pdf_hall)["budget"] == "match"
    assert housing_checks(brief(max_budget="12000"), pdf_hall)["budget"] == "out"
    assert housing_checks(criteria, hall("Unknown period", price_label="$3,970"))["budget"] == (
        "unknown"
    )
