"""Seller-side quoting and hold logic, kept separate from the uAgent runtime for testing."""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from resourcepilot.clock import CAMPUS_TZ, hours_between, later, parse
from resourcepilot.config import SELLER_BY_KEY
from resourcepilot.protocols import (
    BookingConfirmed,
    CommitHold,
    HoldConfirmed,
    HoldRejected,
    HoldRequest,
    QuoteRequest,
)
from resourcepilot.registry import SellerListing
from resourcepilot.store import DurableStore

CATALOG_DIR = Path(__file__).resolve().parent / "catalogs"
FAULT_MODES = ("none", "ghost_quotes", "ghost_holds", "reject_holds", "ghost_commits")
MAX_QUANTITY = 1000


@cache
def load_catalog(seller_id: str) -> tuple[dict[str, Any], ...]:
    return tuple(json.loads((CATALOG_DIR / f"{seller_id}.json").read_text()))


def catalog_item(seller_id: str, item_id: str) -> dict[str, Any] | None:
    return next((dict(i) for i in load_catalog(seller_id) if i["item_id"] == item_id), None)


def price_cents(item: dict[str, Any], quantity: int, hours: float) -> int:
    unit = int(item["unit_price_cents"])
    if item["unit"] == "hour":
        return round(unit * hours * quantity)
    return unit * quantity


def within_opening_hours(listing: SellerListing, start: str, end: str) -> bool:
    local_start = parse(start).astimezone(CAMPUS_TZ)
    local_end = parse(end).astimezone(CAMPUS_TZ)
    if local_end <= local_start or local_start.date() != local_end.date():
        return False
    closes = local_end.hour + local_end.minute / 60
    return local_start.hour >= listing.open_hour and closes <= listing.close_hour


def confirmation_code(seller_id: str, hold_id: str) -> str:
    prefix = "".join(part[0] for part in seller_id.split("_")).upper()
    return f"{prefix}-{str(uuid5(NAMESPACE_URL, f'{seller_id}:{hold_id}'))[:6].upper()}"


async def resolve_fault_mode(store: DurableStore, seller_id: str) -> str:
    stored = await store.get_fault_mode(seller_id)
    if stored in FAULT_MODES:
        return str(stored)
    configured = os.getenv(f"{SELLER_BY_KEY[seller_id].env_prefix}_FAULT_MODE", "none").strip()
    return configured if configured in FAULT_MODES else "none"


async def build_offers(
    store: DurableStore, listing: SellerListing, request: QuoteRequest, now: str
) -> list[dict[str, Any]]:
    hours = hours_between(request.window_start, request.window_end)
    open_now = within_opening_hours(listing, request.window_start, request.window_end)
    offers = []
    for item in load_catalog(listing.seller_id):
        if item["category"] not in request.categories:
            continue
        quantity = max(1, min(MAX_QUANTITY, int(request.quantities.get(item["category"], 1))))
        used = await store.used_quantity(
            listing.seller_id, item["item_id"], request.window_start, request.window_end, now
        )
        reason = ""
        if not open_now:
            reason = f"Outside opening hours ({listing.open_hour}:00-{listing.close_hour}:00)"
        elif used + quantity > int(item["capacity"]):
            reason = "Already booked for that window"
        offers.append(
            {
                **item,
                "seller_id": listing.seller_id,
                "quantity": quantity,
                "hours": round(hours, 2),
                "subtotal_cents": price_cents(item, quantity, hours),
                "available": not reason,
                "unavailable_reason": reason,
            }
        )
    return offers


async def place_hold(
    store: DurableStore,
    listing: SellerListing,
    request: HoldRequest,
    *,
    ttl_seconds: int,
    now: str,
) -> HoldConfirmed | HoldRejected:
    def reject(reason: str) -> HoldRejected:
        return HoldRejected(
            round_id=request.round_id,
            hold_id=request.hold_id,
            seller_id=listing.seller_id,
            reason=reason,
        )

    item = catalog_item(listing.seller_id, request.item_id)
    if item is None:
        return reject("Unknown item")
    if not 1 <= request.quantity <= MAX_QUANTITY:
        return reject("Invalid quantity")
    if not within_opening_hours(listing, request.window_start, request.window_end):
        return reject("Outside opening hours")
    hours = hours_between(request.window_start, request.window_end)
    subtotal = price_cents(item, request.quantity, hours)
    expires_at = later(ttl_seconds, now=parse(now))
    status = await store.place_hold(
        {
            "hold_id": request.hold_id,
            "seller_id": listing.seller_id,
            "item_id": request.item_id,
            "workflow_id": request.workflow_id,
            "quantity": request.quantity,
            "window_start": request.window_start,
            "window_end": request.window_end,
            "expires_at": expires_at,
            "subtotal_cents": subtotal,
        },
        int(item["capacity"]),
        now,
    )
    if status == "unavailable":
        return reject("No longer available for that window")
    if status == "duplicate":
        existing = await store.get_hold(request.hold_id)
        if existing is None or existing["status"] != "held":
            return reject("Hold is no longer active")
        expires_at = str(existing["expires_at"])
        subtotal = int(existing["subtotal_cents"])
    return HoldConfirmed(
        round_id=request.round_id,
        hold_id=request.hold_id,
        seller_id=listing.seller_id,
        expires_at=expires_at,
        subtotal_cents=subtotal,
    )


async def commit_hold(
    store: DurableStore, listing: SellerListing, request: CommitHold, *, now: str
) -> BookingConfirmed | HoldRejected:
    code = confirmation_code(listing.seller_id, request.hold_id)
    hold = await store.get_hold(request.hold_id)
    if hold is None or hold["seller_id"] != listing.seller_id:
        status = "missing"
    else:
        status = await store.commit_hold(request.hold_id, request.payment_reference, code, now)
    if status in {"committed", "duplicate"}:
        return BookingConfirmed(
            round_id=request.round_id,
            hold_id=request.hold_id,
            seller_id=listing.seller_id,
            confirmation_code=code,
        )
    return HoldRejected(
        round_id=request.round_id,
        hold_id=request.hold_id,
        seller_id=listing.seller_id,
        reason=f"Hold could not be committed: {status}",
    )
