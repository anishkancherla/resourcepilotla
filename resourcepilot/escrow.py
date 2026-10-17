"""Escrow settlement: pay each seller its quoted subtotal only after every booking commits.

Every ledger entry has a deterministic ID, so a replayed commit or a retried settlement can never
pay a seller twice. Mock Stripe settlements are simulated and move no money. FET settlements are
sent as stable-testnet transfers from the Orchestrator's escrow wallet only when
FET_PAYOUTS_ENABLED=true; otherwise they are recorded as scheduled and nothing is sent.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from resourcepilot.config import env_bool
from resourcepilot.payment import (
    ATOMIC_PER_FET,
    FET_DIRECT,
    STRIPE_MOCK,
    TESTNET_DENOM,
    amount_to_atomic,
)
from resourcepilot.pricing import cents_to_decimal
from resourcepilot.registry import LISTING_BY_ID
from resourcepilot.store import DurableStore

STATUS_NOTES = {
    "simulated": "Mock Stripe: simulated, no Stripe API call and no money moved",
    "scheduled": "Recorded only: FET_PAYOUTS_ENABLED is false, so nothing was sent",
    "sent": "Sent as a stable-testnet transfer",
    "retained": "Booking fee kept by ResourcePilot after every booking committed",
    "failed": "Transfer failed or no testnet wallet is on file; retry from the ledger",
    "pending": "Recorded; settlement in progress",
}


class PayoutGateway(Protocol):
    async def transfer(self, recipient: str, atomic_amount: int, memo: str) -> str: ...


class CosmpyPayoutGateway:
    """Sends stable-testnet atestfet from the Orchestrator's escrow wallet."""

    def __init__(self, wallet: Any) -> None:
        self.wallet = wallet

    async def transfer(self, recipient: str, atomic_amount: int, memo: str) -> str:
        def _send() -> str:
            from cosmpy.aerial.client import LedgerClient, NetworkConfig
            from cosmpy.crypto.address import Address

            client = LedgerClient(NetworkConfig.fetchai_stable_testnet())
            submitted = client.send_tokens(
                Address(recipient), atomic_amount, TESTNET_DENOM, self.wallet, memo=memo
            )
            submitted.wait_to_complete()
            return str(submitted.tx_hash)

        return await asyncio.wait_for(asyncio.to_thread(_send), timeout=90)


def seller_subtotals(bundle: dict[str, Any]) -> dict[str, int]:
    subtotals: dict[str, int] = {}
    for line in bundle["lines"]:
        subtotals[line["seller_id"]] = subtotals.get(line["seller_id"], 0) + int(
            line["subtotal_cents"]
        )
    return subtotals


def settlement_plan(payment: dict[str, Any], bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Split the escrowed amount into seller payouts plus the platform fee."""
    reference = str(payment["reference"])
    subtotals = seller_subtotals(bundle)
    entries = []
    if payment["currency"] == "USD":
        for seller_id, cents in subtotals.items():
            entries.append(_entry(reference, "payout", seller_id, cents_to_decimal(cents), "USD"))
        entries.append(
            _entry(reference, "fee", "resourcepilot", cents_to_decimal(bundle["fee_cents"]), "USD")
        )
        return entries
    paid_atomic = amount_to_atomic(Decimal(str(payment["amount"])))
    total_cents = int(bundle["total_cents"])
    paid_out = 0
    for seller_id, cents in subtotals.items():
        share = paid_atomic * cents // total_cents
        paid_out += share
        entries.append(
            _entry(reference, "payout", seller_id, Decimal(share) / ATOMIC_PER_FET, "FET")
        )
    fee = Decimal(paid_atomic - paid_out) / ATOMIC_PER_FET
    entries.append(_entry(reference, "fee", "resourcepilot", fee, "FET"))
    return entries


def _entry(reference: str, kind: str, party: str, amount: Decimal, currency: str) -> dict[str, Any]:
    return {
        "entry_id": f"{reference}:{kind}:{party}",
        "reference": reference,
        "kind": kind,
        "party": party,
        "amount": str(amount.normalize() if currency == "FET" else amount),
        "currency": currency,
        "status": "pending",
        "transaction_id": None,
    }


def _mock_id(kind: str, entry_id: str) -> str:
    return f"STRIPE_MOCK_{kind.upper()}:{uuid5(NAMESPACE_URL, entry_id)}"


async def _execute(
    store: DurableStore, gateway: PayoutGateway | None, entry: dict[str, Any], recipient: str
) -> dict[str, Any]:
    """Record an entry once; only the first recorder may move (test) funds for it."""
    if not await store.add_escrow_entry(entry):
        existing = [
            item
            for item in await store.list_escrow_entries(entry["reference"])
            if item["entry_id"] == entry["entry_id"]
        ]
        return existing[0] if existing else entry
    tx: str | None = None
    if entry["kind"] == "fee":
        status = "retained"
    elif entry["currency"] == "USD":
        status, tx = "simulated", _mock_id(entry["kind"], entry["entry_id"])
    elif not env_bool("FET_PAYOUTS_ENABLED", False) or gateway is None:
        status = "scheduled"
    elif not recipient.startswith("fetch1"):
        status = "failed"
    else:
        try:
            atomic = amount_to_atomic(Decimal(str(entry["amount"])))
            tx = await gateway.transfer(recipient, atomic, f"ResourcePilot {entry['entry_id']}")
            status = "sent"
        except Exception:
            status = "failed"
    await store.update_escrow_entry(entry["entry_id"], status, tx)
    return {**entry, "status": status, "transaction_id": tx}


def note_for(entry: dict[str, Any]) -> str:
    return STATUS_NOTES.get(str(entry.get("status")), str(entry.get("status")))


async def settle_booking(
    store: DurableStore,
    gateway: PayoutGateway | None,
    payment: dict[str, Any],
    bundle: dict[str, Any],
) -> list[dict[str, Any]]:
    results = []
    for entry in settlement_plan(payment, bundle):
        listing = LISTING_BY_ID.get(entry["party"])
        recipient = listing.wallet if listing else ""
        results.append(await _execute(store, gateway, entry, recipient))
    return results


async def refund_buyer(
    store: DurableStore, gateway: PayoutGateway | None, payment: dict[str, Any]
) -> dict[str, Any]:
    entry = _entry(
        str(payment["reference"]),
        "refund",
        "buyer",
        Decimal(str(payment["amount"])),
        str(payment["currency"]),
    )
    recipient = str(payment.get("buyer_wallet") or "")
    return await _execute(store, gateway, entry, recipient)


def rail_label(mode_or_currency: str) -> str:
    if mode_or_currency in {STRIPE_MOCK, "USD"}:
        return "Mock Stripe (no money moved)"
    if mode_or_currency in {FET_DIRECT, "FET"}:
        return "Test FET on stable-testnet"
    return mode_or_currency
