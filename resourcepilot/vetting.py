"""Seller vetting: decide which quotes ResourcePilot is willing to put in front of a buyer.

Identity is not trustworthiness, so a quote has to pass four independent layers:

1. Identity: the quote came from the address ResourcePilot's registry lists for that seller, and
   the listing's identity is verified. uAgents envelopes are signed, so the sender address is
   authenticated by the transport.
2. Protocol conformance: the quote speaks ResourceHold 1.1.0, prices in USD, offers a usable hold
   window, stays inside the seller's listed categories, and its arithmetic checks out.
3. Track record: sellers that keep failing holds or bookings are excluded, and new sellers have
   a per-order spending cap.
4. Policy: each offer must be available and the buyer must be eligible for it. A live seller
   (Campus Rooms) may only offer $0 handoff lines whose booking link is https on the school's
   own domain or a known booking vendor.
"""

from __future__ import annotations

from typing import Any

from resourcepilot.config import env_int
from resourcepilot.protocols import PROTOCOL_VERSION, quote_subtotal_cents
from resourcepilot.registry import CATEGORIES, LISTING_BY_ID
from resourcepilot.research import safe_url

MIN_HOLD_TTL_SECONDS = 120
TRACK_RECORD_MIN_ATTEMPTS = 3
TRACK_RECORD_MAX_FAILURE_RATE = 0.5
NEW_SELLER_BOOKINGS = 3


def _reject(
    rejections: list[dict[str, str]], seller_id: str, stage: str, reason: str, item: str = ""
) -> None:
    listing = LISTING_BY_ID.get(seller_id)
    rejections.append(
        {
            "seller_id": seller_id,
            "seller_name": listing.display_name if listing else seller_id or "Unknown agent",
            "stage": stage,
            "item": item,
            "reason": reason,
        }
    )


def track_record_problem(record: dict[str, int]) -> str | None:
    failures = record["holds_failed"] + record["bookings_failed"] + record["no_responses"]
    attempts = failures + record["holds_confirmed"] + record["bookings_committed"]
    if attempts >= TRACK_RECORD_MIN_ATTEMPTS and failures / attempts > (
        TRACK_RECORD_MAX_FAILURE_RATE
    ):
        return f"Track record: {failures} of {attempts} recent requests failed"
    return None


def _arithmetic_ok(offer: dict[str, Any], hours: float) -> bool:
    try:
        expected = quote_subtotal_cents(offer, int(offer["quantity"]), hours)
        return abs(int(offer["subtotal_cents"]) - expected) <= 1
    except (KeyError, TypeError, ValueError):
        return False


def _identity_problem(quote: dict[str, Any]) -> str | None:
    listing = LISTING_BY_ID.get(str(quote.get("seller_id", "")))
    if listing is None or not listing.address or listing.address != quote.get("sender"):
        return "Not in ResourcePilot's seller registry, or the address does not match it"
    if not listing.identity_verified:
        return "Identity not verified"
    return None


def _protocol_problem(quote: dict[str, Any]) -> str | None:
    if quote.get("protocol_version") != PROTOCOL_VERSION:
        return f"Speaks ResourceHold {quote.get('protocol_version')}, not {PROTOCOL_VERSION}"
    if quote.get("currency") != "USD":
        return "Quoted in an unsupported currency"
    if int(quote.get("hold_ttl_seconds") or 0) < MIN_HOLD_TTL_SECONDS:
        return "Hold window too short to complete checkout"
    if not isinstance(quote.get("offers"), list):
        return "Malformed offers"
    return None


def _handoff_problem(offer: dict[str, Any], campus: dict[str, Any]) -> str | None:
    if not offer.get("handoff") or int(offer.get("subtotal_cents") or 0) != 0:
        return "Live rooms must be free handoff lines"
    if not safe_url(str(offer.get("handoff_url") or ""), str(campus.get("domain") or "")):
        return "Booking link is not on the school's site or a known booking system"
    return None


def vet_quotes(
    brief: dict[str, Any],
    quotes: list[dict[str, Any]],
    records: dict[str, dict[str, int]],
    campus: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
    """Return (trusted offers, rejections, per-seller vetting summary)."""
    offers: list[dict[str, Any]] = []
    rejections: list[dict[str, str]] = []
    summary: list[dict[str, Any]] = []
    new_seller_cap = env_int("NEW_SELLER_CAP_CENTS", 2500, minimum=0)
    hours = float(brief["hours"])
    for quote in quotes:
        seller_id = str(quote.get("seller_id", ""))
        problem = _identity_problem(quote)
        stage = "identity"
        if problem is None:
            problem, stage = _protocol_problem(quote), "protocol"
        record = records.get(seller_id)
        if problem is None and record is not None:
            problem, stage = track_record_problem(record), "track_record"
        if problem:
            _reject(rejections, seller_id, stage, problem)
            summary.append({"seller_id": seller_id, "status": "rejected", "reason": problem})
            continue

        listing = LISTING_BY_ID[seller_id]
        is_new = (record or {}).get("bookings_committed", 0) < NEW_SELLER_BOOKINGS
        kept = 0
        for raw in quote["offers"]:
            offer = dict(raw)
            name = str(offer.get("name", "item"))
            category = str(offer.get("category", ""))
            if category not in listing.categories or category not in CATEGORIES:
                _reject(rejections, seller_id, "protocol", "Offer outside listed categories", name)
                continue
            if category not in brief["items"]:
                continue
            if not _arithmetic_ok(offer, hours):
                _reject(rejections, seller_id, "protocol", "Quote arithmetic does not add up", name)
                continue
            if not offer.get("available"):
                reason = str(offer.get("unavailable_reason") or "Unavailable")
                _reject(rejections, seller_id, "policy", reason, name)
                continue
            if listing.live:
                problem = _handoff_problem(offer, campus or {})
                if problem:
                    _reject(rejections, seller_id, "policy", problem, name)
                    continue
            elif offer.get("handoff"):
                _reject(rejections, seller_id, "policy", "Only live sellers offer handoffs", name)
                continue
            if offer.get("requires_training") and not brief.get("has_training"):
                _reject(rejections, seller_id, "policy", "Requires gear-share training", name)
                continue
            if is_new and int(offer["subtotal_cents"]) > new_seller_cap:
                _reject(
                    rejections,
                    seller_id,
                    "track_record",
                    f"New seller cap is ${new_seller_cap / 100:.2f} per item",
                    name,
                )
                continue
            wanted = str(brief.get("camera_type", "any"))
            offer.update(
                seller_name=listing.display_name,
                location_label=str(offer.get("location_label") or listing.location_label)
                if listing.live
                else listing.location_label,
                lat=listing.lat,
                lon=listing.lon,
                new_seller=is_new,
                exact_match=category != "camera" or wanted == "any" or wanted in offer["tags"],
            )
            offers.append(offer)
            kept += 1
        summary.append({"seller_id": seller_id, "status": "trusted", "offers": kept})
    return offers, rejections, summary


def verification_handler(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    brief = dict(payload["brief"])
    records = dict(context.get("seller_records", {}))
    campus = dict(payload.get("campus") or {})
    offers, rejections, summary = vet_quotes(
        brief, list(payload.get("quotes", [])), records, campus
    )
    for seller_id in payload.get("no_response", []):
        _reject(rejections, str(seller_id), "availability", "Did not answer in time")
    covered = {offer["category"] for offer in offers}
    if not covered:
        reasons = list(dict.fromkeys(r["reason"] for r in rejections if r["stage"] == "policy"))
        if brief.get("service") == "rooms" and reasons:
            raise ValueError(reasons[0])
        raise ValueError("No trustworthy seller could offer any of the requested resources")
    carried = {key: payload[key] for key in ("campus", "school", "rooms") if key in payload}
    return {
        **carried,
        "brief": brief,
        "offers": offers,
        "rejections": rejections,
        "vetting": summary,
        "uncovered": sorted(set(brief["items"]) - covered),
    }
