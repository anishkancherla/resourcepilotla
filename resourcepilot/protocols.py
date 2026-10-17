"""ResourceHold: the protocol a seller agent must speak to be sourced from.

A seller quotes, places a time-limited hold, and turns the hold into a booking only when the
buyer commits. A hold can always be released. No message in this protocol moves money; escrow
payment and payouts stay with the ResourcePilot Orchestrator.
"""

from __future__ import annotations

from typing import Any

from pydantic.v1 import Field
from uagents import Model
from uagents_core.protocol import ProtocolSpecification

PROTOCOL_NAME = "ResourceHold"
PROTOCOL_VERSION = "1.0.0"


class QuoteRequest(Model):
    round_id: str
    workflow_id: str
    categories: list[str]
    quantities: dict[str, int]
    window_start: str
    window_end: str


class Quote(Model):
    round_id: str
    seller_id: str
    protocol_version: str = PROTOCOL_VERSION
    currency: str = "USD"
    hold_ttl_seconds: int
    offers: list[dict[str, Any]] = Field(default_factory=list)


class HoldRequest(Model):
    round_id: str
    workflow_id: str
    hold_id: str
    item_id: str
    quantity: int
    window_start: str
    window_end: str


class HoldConfirmed(Model):
    round_id: str
    hold_id: str
    seller_id: str
    expires_at: str
    subtotal_cents: int


class HoldRejected(Model):
    round_id: str
    hold_id: str
    seller_id: str
    reason: str


class CommitHold(Model):
    round_id: str
    hold_id: str
    payment_reference: str


class BookingConfirmed(Model):
    round_id: str
    hold_id: str
    seller_id: str
    confirmation_code: str


class ReleaseHold(Model):
    hold_id: str
    reason: str


resource_hold_spec = ProtocolSpecification(
    name=PROTOCOL_NAME,
    version=PROTOCOL_VERSION,
    interactions={
        QuoteRequest: {Quote},
        Quote: set(),
        HoldRequest: {HoldConfirmed, HoldRejected},
        HoldConfirmed: set(),
        HoldRejected: set(),
        CommitHold: {BookingConfirmed, HoldRejected},
        BookingConfirmed: set(),
        ReleaseHold: set(),
    },
    roles={
        "seller": {QuoteRequest, HoldRequest, CommitHold, ReleaseHold},
        "buyer": {Quote, HoldConfirmed, HoldRejected, BookingConfirmed},
    },
)


def quote_subtotal_cents(offer: dict[str, Any], quantity: int, hours: float) -> int:
    """ResourceHold pricing rule: hourly items scale with hours, other items with quantity."""
    unit = int(offer["unit_price_cents"])
    if offer["unit"] == "hour":
        return round(unit * hours * quantity)
    return unit * quantity


def seller_protocol_digest() -> str:
    """Digest a seller publishes; Discovery searches the Almanac for it."""
    return str(resource_hold_spec.manifest(role="seller")["metadata"]["digest"])
