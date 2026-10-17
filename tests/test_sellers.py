from __future__ import annotations

from datetime import datetime

import pytest
from uagents import Protocol

from resourcepilot.clock import CAMPUS_TZ
from resourcepilot.config import SELLER_AGENTS
from resourcepilot.protocols import (
    BookingConfirmed,
    CommitHold,
    HoldConfirmed,
    HoldRejected,
    HoldRequest,
    QuoteRequest,
    quote_subtotal_cents,
    resource_hold_spec,
    seller_protocol_digest,
)
from resourcepilot.registry import CATEGORIES, LISTING_BY_ID, LISTINGS, listings_for
from resourcepilot.store import MemoryStore
from sellers.inventory import (
    build_offers,
    commit_hold,
    confirmation_code,
    load_catalog,
    place_hold,
    resolve_fault_mode,
    within_opening_hours,
)


def local(hour: int, minute: int = 0) -> str:
    return datetime(2026, 10, 23, hour, minute, tzinfo=CAMPUS_TZ).isoformat()


NOW = local(12)
START, END = local(15), local(18)


def quote_request(categories: list[str], quantities: dict[str, int] | None = None) -> QuoteRequest:
    return QuoteRequest(
        round_id="round-1",
        workflow_id="wf-1",
        categories=categories,
        quantities=quantities or {},
        window_start=START,
        window_end=END,
    )


def hold_request(hold_id: str, item_id: str, quantity: int = 1) -> HoldRequest:
    return HoldRequest(
        round_id="round-2",
        workflow_id="wf-1",
        hold_id=hold_id,
        item_id=item_id,
        quantity=quantity,
        window_start=START,
        window_end=END,
    )


def test_every_seller_has_a_listing_and_valid_catalog() -> None:
    assert {seller.key for seller in SELLER_AGENTS} == set(LISTING_BY_ID)
    for listing in LISTINGS:
        catalog = load_catalog(listing.seller_id)
        assert catalog
        for item in catalog:
            assert item["category"] in CATEGORIES
            assert item["category"] in listing.categories
            assert item["unit"] in {"hour", "item"}
            assert int(item["unit_price_cents"]) > 0
            assert int(item["capacity"]) > 0
            assert item["policy"]


def test_registry_includes_exactly_one_unverified_seller() -> None:
    unverified = [listing.seller_id for listing in LISTINGS if not listing.identity_verified]
    assert unverified == ["flash_prints"]
    assert all("Demo listing" in listing.verification_basis for listing in LISTINGS)
    assert {listing.seller_id for listing in listings_for(["posters"])} == {
        "westwood_print",
        "gayley_copy",
        "flash_prints",
    }


def test_seller_protocol_digest_matches_published_role() -> None:
    protocol = Protocol(spec=resource_hold_spec, role="seller")
    assert protocol.digest == seller_protocol_digest()
    assert seller_protocol_digest().startswith("proto:")


def test_hourly_and_per_item_pricing() -> None:
    dslr = next(i for i in load_catalog("gear_share") if i["item_id"] == "dslr_kit")
    poster = load_catalog("westwood_print")[0]
    assert quote_subtotal_cents(dslr, 1, 3) == 750
    assert quote_subtotal_cents(poster, 20, 3) == 1100


def test_opening_hours_use_campus_time() -> None:
    gear = LISTING_BY_ID["gear_share"]
    assert within_opening_hours(gear, START, END)
    assert not within_opening_hours(gear, local(20), local(22))
    assert not within_opening_hours(gear, local(7), local(9))


async def test_quote_reports_price_quantity_and_availability() -> None:
    store = MemoryStore()
    offers = await build_offers(
        store, LISTING_BY_ID["westwood_print"], quote_request(["posters"], {"posters": 20}), NOW
    )
    [offer] = offers
    assert offer["available"] is True
    assert offer["quantity"] == 20
    assert offer["subtotal_cents"] == 1100
    assert offer["seller_id"] == "westwood_print"


async def test_quote_only_includes_requested_categories() -> None:
    offers = await build_offers(
        MemoryStore(), LISTING_BY_ID["gear_share"], quote_request(["camera"]), NOW
    )
    assert {offer["category"] for offer in offers} == {"camera"}
    assert {offer["item_id"] for offer in offers} == {"dslr_kit", "mirrorless_kit"}


async def test_hold_then_commit_is_idempotent() -> None:
    store = MemoryStore()
    listing = LISTING_BY_ID["studio_rooms"]
    confirmed = await place_hold(
        store, listing, hold_request("h1", "podcast_studio"), ttl_seconds=600, now=NOW
    )
    assert isinstance(confirmed, HoldConfirmed)
    assert confirmed.subtotal_cents == 1200
    again = await place_hold(
        store, listing, hold_request("h1", "podcast_studio"), ttl_seconds=600, now=NOW
    )
    assert isinstance(again, HoldConfirmed)
    assert again.expires_at == confirmed.expires_at

    competing = await place_hold(
        store, listing, hold_request("h2", "podcast_studio"), ttl_seconds=600, now=NOW
    )
    assert isinstance(competing, HoldRejected)
    assert "No longer available" in competing.reason

    commit = CommitHold(round_id="round-3", hold_id="h1", payment_reference="ref-1")
    booked = await commit_hold(store, listing, commit, now=NOW)
    assert isinstance(booked, BookingConfirmed)
    assert booked.confirmation_code == confirmation_code("studio_rooms", "h1")
    replay = await commit_hold(store, listing, commit, now=NOW)
    assert isinstance(replay, BookingConfirmed)
    assert replay.confirmation_code == booked.confirmation_code

    offers = await build_offers(store, listing, quote_request(["podcast_room"]), NOW)
    assert offers[0]["available"] is False


async def test_commit_rejects_expired_or_foreign_holds() -> None:
    store = MemoryStore()
    rooms, gear = LISTING_BY_ID["studio_rooms"], LISTING_BY_ID["gear_share"]
    await place_hold(store, rooms, hold_request("h1", "study_room"), ttl_seconds=60, now=NOW)
    foreign = await commit_hold(
        store, gear, CommitHold(round_id="r", hold_id="h1", payment_reference="p"), now=NOW
    )
    assert isinstance(foreign, HoldRejected)
    expired = await commit_hold(
        store,
        rooms,
        CommitHold(round_id="r", hold_id="h1", payment_reference="p"),
        now=local(12, 5),
    )
    assert isinstance(expired, HoldRejected)
    assert "expired" in expired.reason


@pytest.mark.parametrize(
    ("item_id", "quantity", "reason"),
    [("not_real", 1, "Unknown item"), ("tripod", 0, "Invalid quantity")],
)
async def test_hold_validates_item_and_quantity(item_id: str, quantity: int, reason: str) -> None:
    result = await place_hold(
        MemoryStore(),
        LISTING_BY_ID["gear_share"],
        hold_request("h1", item_id, quantity),
        ttl_seconds=600,
        now=NOW,
    )
    assert isinstance(result, HoldRejected)
    assert result.reason == reason


async def test_fault_mode_prefers_demo_toggle_then_env(monkeypatch) -> None:
    store = MemoryStore()
    assert await resolve_fault_mode(store, "westwood_print") == "none"
    monkeypatch.setenv("WESTWOOD_PRINT_FAULT_MODE", "ghost_holds")
    assert await resolve_fault_mode(store, "westwood_print") == "ghost_holds"
    await store.set_fault_mode("westwood_print", "none")
    assert await resolve_fault_mode(store, "westwood_print") == "none"
    monkeypatch.setenv("GAYLEY_COPY_FAULT_MODE", "steal_money")
    assert await resolve_fault_mode(store, "gayley_copy") == "none"
