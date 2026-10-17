"""Buyer payment into ResourcePilot escrow over the official Agent Payment Protocol.

Two rails, neither of which moves real money:

- ``stripe_mock``: a simulated Stripe-style checkout. No Stripe API call, no funds moved.
- ``fet_direct``: faucet-issued Fetch stable-testnet tokens, verified on chain with cosmpy.

FET mainnet is permanently unsupported.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Protocol

from uagents_core.contrib.protocols.payment import CommitPayment

from resourcepilot.pricing import cents_to_decimal
from resourcepilot.store import DurableStore

ATOMIC_PER_FET = Decimal(10) ** 18
FET_DIRECT = "fet_direct"
STRIPE_MOCK = "stripe_mock"
STRIPE_MOCK_NETWORK = "stripe-mock-no-network"
STRIPE_MOCK_RECIPIENT = "acct_mock_resourcepilot"
TESTNET = "stable-testnet"
TESTNET_DENOM = "atestfet"
FET_QUANTUM = Decimal("0.000001")


def _testnet_only() -> None:
    network = os.getenv("FET_NETWORK", TESTNET).strip().lower()
    if network != TESTNET:
        raise ValueError("only FET stable-testnet is supported; mainnet is permanently disabled")
    if os.getenv("FET_DENOM", TESTNET_DENOM).strip() != TESTNET_DENOM:
        raise ValueError(f"{TESTNET} requires denom {TESTNET_DENOM}")


def fet_for_usd_cents(total_cents: int) -> Decimal:
    """Convert a USD total to a small test-token amount at the fixed demo test rate."""
    _testnet_only()
    rate = Decimal(os.getenv("FET_PER_USD", "0.0001"))
    cap = Decimal(os.getenv("FET_MAX_AMOUNT", "0.01"))
    if rate <= 0 or cap <= 0:
        raise ValueError("FET_PER_USD and FET_MAX_AMOUNT must be positive")
    amount = (cents_to_decimal(total_cents) * rate).quantize(FET_QUANTUM, rounding=ROUND_DOWN)
    if amount <= 0:
        raise ValueError("test FET amount rounds to zero")
    if amount > cap:
        raise ValueError(f"test FET amount {amount} exceeds FET_MAX_AMOUNT {cap}")
    return amount


@dataclass(frozen=True)
class PaymentConfig:
    mode: str
    network: str
    amount: Decimal
    denom: str
    recipient: str

    @classmethod
    def for_total(cls, mode: str, total_cents: int, fet_recipient: str) -> PaymentConfig:
        selected = mode.strip().lower()
        if total_cents <= 0:
            raise ValueError("payment total must be positive")
        if selected == STRIPE_MOCK:
            recipient = os.getenv("STRIPE_MOCK_RECIPIENT", STRIPE_MOCK_RECIPIENT).strip()
            if not recipient:
                raise ValueError("STRIPE_MOCK_RECIPIENT must not be empty")
            return cls(
                STRIPE_MOCK, STRIPE_MOCK_NETWORK, cents_to_decimal(total_cents), "", recipient
            )
        if selected != FET_DIRECT:
            raise ValueError("payment method must be stripe_mock or fet_direct")
        if not fet_recipient.startswith("fetch1"):
            raise ValueError("escrow FET wallet is not configured")
        return cls(
            FET_DIRECT, TESTNET, fet_for_usd_cents(total_cents), TESTNET_DENOM, fet_recipient
        )

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> PaymentConfig:
        network = str(record["network"])
        if network == STRIPE_MOCK_NETWORK:
            mode, denom = STRIPE_MOCK, ""
        elif network == TESTNET:
            mode, denom = FET_DIRECT, TESTNET_DENOM
        else:
            raise ValueError("payment record uses a disabled network")
        return cls(mode, network, Decimal(str(record["amount"])), denom, str(record["recipient"]))

    @property
    def currency(self) -> str:
        return "USD" if self.mode == STRIPE_MOCK else "FET"


@dataclass(frozen=True)
class TxEvidence:
    successful: bool
    events: dict[str, Any]


class LedgerGateway(Protocol):
    async def query(self, transaction_id: str, network: str) -> TxEvidence | None: ...


class CosmpyLedgerGateway:
    async def query(self, transaction_id: str, network: str) -> TxEvidence | None:
        def _query() -> TxEvidence | None:
            from cosmpy.aerial.client import LedgerClient, NetworkConfig

            if network != TESTNET:
                raise ValueError("only FET stable-testnet is supported")
            response = LedgerClient(NetworkConfig.fetchai_stable_testnet()).query_tx(transaction_id)
            if response is None:
                return None
            successful = not hasattr(response, "is_successful") or response.is_successful()
            return TxEvidence(
                successful=successful, events=dict(getattr(response, "events", {}) or {})
            )

        return await asyncio.wait_for(asyncio.to_thread(_query), timeout=20)


def amount_to_atomic(amount: Decimal) -> int:
    atomic = amount * ATOMIC_PER_FET
    if atomic != atomic.to_integral_value():
        raise ValueError("FET amount has more than 18 decimal places")
    return int(atomic)


def _values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def _amount_at_least(attributes: dict[str, Any], denom: str, minimum: int) -> bool:
    for raw in _values(attributes.get("amount")):
        for number, coin_denom in re.findall(r"(\d+)([a-zA-Z][a-zA-Z0-9/]*)", raw):
            if coin_denom == denom and int(number) >= minimum:
                return True
    return False


def verify_tx_evidence(
    evidence: TxEvidence | None,
    *,
    expected_sender: str,
    expected_recipient: str,
    expected_amount: Decimal,
    expected_denom: str,
) -> tuple[bool, str]:
    if evidence is None:
        return False, "transaction not found"
    if not evidence.successful:
        return False, "transaction failed on chain"
    transfer = evidence.events.get("transfer")
    transfers = transfer if isinstance(transfer, list) else [transfer]
    for attributes in transfers:
        if not isinstance(attributes, dict):
            continue
        if (
            expected_sender in _values(attributes.get("sender"))
            and expected_recipient in _values(attributes.get("recipient"))
            and _amount_at_least(attributes, expected_denom, amount_to_atomic(expected_amount))
        ):
            return True, "verified"
    return False, "sender, recipient, amount, or denom did not match"


async def verify_commit(
    store: DurableStore,
    commit: CommitPayment,
    *,
    sender: str,
    config: PaymentConfig,
    gateway: LedgerGateway,
) -> tuple[bool, str]:
    """Independently verify a buyer's commit before anything is reserved or booked."""
    reference = str(commit.reference or "").strip()
    transaction_id = str(commit.transaction_id or "").strip()
    record = await store.get_payment(reference)
    if record is None:
        return False, "unknown payment reference"
    if record["requester"] != sender:
        return False, "payment requester did not match"
    if str(commit.recipient) != config.recipient or record["recipient"] != config.recipient:
        return False, "recipient did not match"
    if str(commit.funds.currency) != config.currency:
        return False, "currency did not match"
    if str(commit.funds.payment_method) != config.mode:
        return False, "payment method did not match"
    try:
        declared = Decimal(str(commit.funds.amount))
    except InvalidOperation:
        return False, "invalid payment amount"
    if declared != config.amount:
        return False, "declared amount did not match"
    metadata = commit.metadata if isinstance(commit.metadata, dict) else {}
    if not transaction_id:
        return False, "transaction id is required"

    if config.mode == STRIPE_MOCK:
        buyer_wallet = str(metadata.get("stripe_mock_buyer") or "").strip()
        if buyer_wallet != sender:
            return False, "Stripe mock buyer did not match requester"
        if not transaction_id.startswith("STRIPE_MOCK:"):
            return False, "Stripe mock transaction marker missing"
        if str(metadata.get("moves_funds", "")).lower() != "false":
            return False, "Stripe mock must declare that it moves no funds"
        if str(metadata.get("stripe_mock_payment_intent", "")) != transaction_id:
            return False, "Stripe mock payment intent did not match"
    else:
        buyer_wallet = str(
            metadata.get("buyer_fet_wallet") or metadata.get("buyer_fet_address") or ""
        ).strip()
        if not buyer_wallet:
            return False, "buyer_fet_wallet is required"
        if str(metadata.get("fet_network") or config.network) != config.network:
            return False, "network did not match"
        evidence = await gateway.query(transaction_id, config.network)
        verified, reason = verify_tx_evidence(
            evidence,
            expected_sender=buyer_wallet,
            expected_recipient=config.recipient,
            expected_amount=config.amount,
            expected_denom=config.denom,
        )
        if not verified:
            return False, reason

    status = await store.verify_payment(reference, transaction_id, buyer_wallet)
    if status == "verified":
        return True, "verified"
    if status == "replay":
        return True, "already verified"
    return False, status.replace("_", " ")
