from __future__ import annotations

from datetime import timedelta

import pytest
from helpers import NOW, brief, seller_quotes

from resourcepilot.clock import parse
from resourcepilot.logistics import analysis_handler, walk_minutes
from resourcepilot.store import SELLER_COUNTERS
from resourcepilot.vetting import track_record_problem, verification_handler, vet_quotes


def reasons(result: dict, seller_id: str) -> list[str]:
    return [r["reason"] for r in result["rejections"] if r["seller_id"] == seller_id]


def record(**counts: int) -> dict[str, int]:
    return {**dict.fromkeys(SELLER_COUNTERS, 0), **counts}


async def test_unverified_seller_and_untrained_dslr_are_rejected(registry_env) -> None:
    selected = brief()
    result = verification_handler(
        {"brief": selected, "quotes": await seller_quotes(selected, registry_env)}, {}
    )
    assert reasons(result, "flash_prints") == ["Identity not verified"]
    assert "Requires gear-share training" in reasons(result, "gear_share")
    kept = {(offer["seller_id"], offer["item_id"]) for offer in result["offers"]}
    assert ("gear_share", "dslr_kit") not in kept
    assert ("gear_share", "mirrorless_kit") in kept
    assert {("westwood_print", "poster_color_18x24"), ("gayley_copy", "poster_color_18x24")} <= kept
    assert ("studio_rooms", "podcast_studio") in kept
    mirrorless = next(o for o in result["offers"] if o["item_id"] == "mirrorless_kit")
    assert mirrorless["exact_match"] is False
    assert result["uncovered"] == []


async def test_trained_buyer_keeps_the_exact_dslr_match(registry_env) -> None:
    selected = brief(has_training=True)
    result = verification_handler(
        {"brief": selected, "quotes": await seller_quotes(selected, registry_env)}, {}
    )
    dslr = next(o for o in result["offers"] if o["item_id"] == "dslr_kit")
    assert dslr["exact_match"] is True


async def test_impersonation_and_unlisted_agents_fail_identity(registry_env) -> None:
    selected = brief()
    quotes = await seller_quotes(selected, registry_env)
    westwood = next(q for q in quotes if q["seller_id"] == "westwood_print")
    impostor = {**westwood, "sender": "agent1qimpostor"}
    rogue = {**westwood, "seller_id": "cheap_rogue", "sender": "agent1qrogue"}
    offers, rejections, summary = vet_quotes(selected, [impostor, rogue], {})
    assert offers == []
    assert [r["stage"] for r in rejections] == ["identity", "identity"]
    assert {item["status"] for item in summary} == {"rejected"}


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ({"protocol_version": "0.9.0"}, "ResourceHold 0.9.0"),
        ({"hold_ttl_seconds": 30}, "Hold window too short"),
        ({"currency": "EUR"}, "unsupported currency"),
    ],
)
async def test_protocol_conformance(registry_env, tamper: dict, reason: str) -> None:
    selected = brief()
    quotes = [
        {**q, **tamper} if q["seller_id"] == "studio_rooms" else q
        for q in await seller_quotes(selected, registry_env)
    ]
    result = verification_handler({"brief": selected, "quotes": quotes}, {})
    assert any(reason in item for item in reasons(result, "studio_rooms"))
    assert "podcast_room" in result["uncovered"]


async def test_tampered_quote_arithmetic_is_rejected(registry_env) -> None:
    selected = brief()
    quotes = await seller_quotes(selected, registry_env)
    for quote in quotes:
        if quote["seller_id"] == "westwood_print":
            quote["offers"][0]["subtotal_cents"] = 1
    result = verification_handler({"brief": selected, "quotes": quotes}, {})
    assert "Quote arithmetic does not add up" in reasons(result, "westwood_print")


async def test_failing_track_record_excludes_a_seller(registry_env) -> None:
    selected = brief()
    result = verification_handler(
        {"brief": selected, "quotes": await seller_quotes(selected, registry_env)},
        {"seller_records": {"westwood_print": record(holds_failed=3, holds_confirmed=1)}},
    )
    assert reasons(result, "westwood_print") == ["Track record: 3 of 4 recent requests failed"]
    assert any(offer["seller_id"] == "gayley_copy" for offer in result["offers"])


def test_track_record_needs_enough_attempts() -> None:
    assert track_record_problem(record(holds_failed=2)) is None
    assert track_record_problem(record(holds_failed=2, no_responses=1)) is not None
    assert track_record_problem(record(holds_failed=1, holds_confirmed=5)) is None


async def test_new_seller_spending_cap(registry_env, monkeypatch) -> None:
    monkeypatch.setenv("NEW_SELLER_CAP_CENTS", "1150")
    selected = brief()
    quotes = await seller_quotes(selected, registry_env)
    capped = verification_handler({"brief": selected, "quotes": quotes}, {})
    assert any("New seller cap" in item for item in reasons(capped, "studio_rooms"))
    trusted = verification_handler(
        {"brief": selected, "quotes": quotes},
        {"seller_records": {"studio_rooms": record(bookings_committed=5)}},
    )
    assert any(offer["seller_id"] == "studio_rooms" for offer in trusted["offers"])


async def test_silent_sellers_and_empty_results(registry_env) -> None:
    selected = brief()
    result = verification_handler(
        {
            "brief": selected,
            "quotes": await seller_quotes(selected, registry_env),
            "no_response": ["gayley_copy"],
        },
        {},
    )
    assert reasons(result, "gayley_copy") == ["Did not answer in time"]
    with pytest.raises(ValueError, match="No trustworthy seller"):
        verification_handler({"brief": selected, "quotes": []}, {})


async def test_analysis_enforces_lead_time_and_adds_walks(registry_env) -> None:
    selected = brief()
    vetted = verification_handler(
        {"brief": selected, "quotes": await seller_quotes(selected, registry_env)}, {}
    )
    one_hour_before = parse(selected["window_start"]) - timedelta(hours=1)
    result = analysis_handler(vetted, {"now": one_hour_before.isoformat()})
    sellers = {offer["seller_id"] for offer in result["offers"]}
    assert "westwood_print" not in sellers
    assert "gayley_copy" in sellers
    assert any("lead time" in r["reason"] for r in result["rejections"])
    assert all(offer["walk_minutes"] >= 1 for offer in result["offers"])
    early = analysis_handler(vetted, {"now": NOW.isoformat()})
    assert "westwood_print" in {offer["seller_id"] for offer in early["offers"]}


def test_walk_estimate_is_monotonic() -> None:
    near = walk_minutes(34.0705, -118.4445, 34.0700, -118.4430)
    far = walk_minutes(34.0705, -118.4445, 34.0617, -118.4469)
    assert 1 <= near < far
