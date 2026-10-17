from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from uagents_core.contrib.protocols.payment import CommitPayment, Funds

from resourcepilot import escrow
from resourcepilot.models import PaymentRecord
from resourcepilot.payment import (
    FET_DIRECT,
    STRIPE_MOCK,
    PaymentConfig,
    TxEvidence,
    amount_to_atomic,
    fet_for_usd_cents,
    verify_commit,
    verify_tx_evidence,
)
from resourcepilot.store import MemoryStore

BUYER = "agent1qbuyer"
BUYER_WALLET = "fetch1buyerwallet"
ESCROW = "fetch1escrowwallet"
BUNDLE = {
    "lines": [
        {"seller_id": "gear_share", "subtotal_cents": 900},
        {"seller_id": "studio_rooms", "subtotal_cents": 1200},
        {"seller_id": "westwood_print", "subtotal_cents": 1100},
    ],
    "subtotal_cents": 3200,
    "fee_cents": 126,
    "total_cents": 3326,
}


class FakeLedger:
    def __init__(self, evidence: TxEvidence | None) -> None:
        self.evidence = evidence
        self.calls: list[tuple[str, str]] = []

    async def query(self, transaction_id: str, network: str) -> TxEvidence | None:
        self.calls.append((transaction_id, network))
        return self.evidence


def transfer(amount: Decimal, *, sender: str = BUYER_WALLET, recipient: str = ESCROW) -> TxEvidence:
    return TxEvidence(
        successful=True,
        events={
            "transfer": {
                "sender": sender,
                "recipient": recipient,
                "amount": f"{amount_to_atomic(amount)}atestfet",
            }
        },
    )


async def requested(store: MemoryStore, config: PaymentConfig, requester: str = BUYER) -> str:
    reference = str(uuid4())
    await store.create_payment(
        PaymentRecord(
            reference=reference,
            workflow_id="wf",
            requester=requester,
            amount=str(config.amount),
            currency=config.currency,
            recipient=config.recipient,
            network=config.network,
            status="requested",
        )
    )
    return reference


def mock_commit(config: PaymentConfig, reference: str, **metadata: str) -> CommitPayment:
    tx = metadata.pop("tx", f"STRIPE_MOCK:{uuid4()}")
    return CommitPayment(
        funds=Funds(amount=str(config.amount), currency="USD", payment_method=STRIPE_MOCK),
        recipient=config.recipient,
        transaction_id=tx,
        reference=reference,
        description="test",
        metadata={
            "stripe_mock_buyer": BUYER,
            "stripe_mock_payment_intent": tx,
            "moves_funds": "false",
            **metadata,
        },
    )


def fet_commit(config: PaymentConfig, reference: str, tx: str = "TX1") -> CommitPayment:
    return CommitPayment(
        funds=Funds(amount=str(config.amount), currency="FET", payment_method=FET_DIRECT),
        recipient=config.recipient,
        transaction_id=tx,
        reference=reference,
        description="test",
        metadata={"buyer_fet_wallet": BUYER_WALLET, "fet_network": "stable-testnet"},
    )


def test_amounts_follow_the_bundle_total() -> None:
    mock = PaymentConfig.for_total(STRIPE_MOCK, 3326, ESCROW)
    assert (mock.amount, mock.currency, mock.recipient) == (
        Decimal("33.26"),
        "USD",
        "acct_mock_resourcepilot",
    )
    fet = PaymentConfig.for_total(FET_DIRECT, 3326, ESCROW)
    assert (fet.amount, fet.currency, fet.network, fet.recipient) == (
        Decimal("0.003326"),
        "FET",
        "stable-testnet",
        ESCROW,
    )
    assert fet_for_usd_cents(3326) == Decimal("0.003326")


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"FET_NETWORK": "mainnet"}, "mainnet is permanently disabled"),
        ({"FET_DENOM": "afet"}, "requires denom atestfet"),
        ({"FET_MAX_AMOUNT": "0.001"}, "exceeds FET_MAX_AMOUNT"),
    ],
)
def test_fet_is_stable_testnet_only_and_capped(monkeypatch, env, message) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=message):
        PaymentConfig.for_total(FET_DIRECT, 3326, ESCROW)


def test_unknown_method_and_missing_wallet_are_refused() -> None:
    with pytest.raises(ValueError, match="stripe_mock or fet_direct"):
        PaymentConfig.for_total("card", 3326, ESCROW)
    with pytest.raises(ValueError, match="wallet is not configured"):
        PaymentConfig.for_total(FET_DIRECT, 3326, "")
    with pytest.raises(ValueError, match="disabled network"):
        PaymentConfig.from_record({"network": "fetchai-mainnet", "amount": "1", "recipient": "x"})


async def test_valid_stripe_mock_commit_verifies_once() -> None:
    store = MemoryStore()
    config = PaymentConfig.for_total(STRIPE_MOCK, 3326, ESCROW)
    reference = await requested(store, config)
    commit = mock_commit(config, reference)
    ledger = FakeLedger(None)
    assert await verify_commit(store, commit, sender=BUYER, config=config, gateway=ledger) == (
        True,
        "verified",
    )
    again = await verify_commit(store, commit, sender=BUYER, config=config, gateway=ledger)
    assert again == (True, "already verified")
    assert ledger.calls == []  # Mock Stripe never touches a ledger
    assert (await store.get_payment(reference))["status"] == "verified"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"sender": "agent1qsomeoneelse"}, "requester did not match"),
        ({"amount": "1.00"}, "declared amount did not match"),
        ({"moves_funds": "true"}, "moves no funds"),
        ({"tx": "pi_real_looking"}, "transaction marker missing"),
        ({"stripe_mock_payment_intent": "STRIPE_MOCK:other"}, "payment intent did not match"),
    ],
)
async def test_invalid_stripe_mock_commits_are_rejected(change, reason) -> None:
    store = MemoryStore()
    config = PaymentConfig.for_total(STRIPE_MOCK, 3326, ESCROW)
    reference = await requested(store, config)
    sender = change.pop("sender", BUYER)
    amount = change.pop("amount", None)
    commit = mock_commit(config, reference, **change)
    if amount:
        commit.funds.amount = amount
    ok, why = await verify_commit(
        store, commit, sender=sender, config=config, gateway=FakeLedger(None)
    )
    assert not ok and reason in why
    assert (await store.get_payment(reference))["status"] == "requested"


async def test_one_transaction_cannot_pay_two_references() -> None:
    store = MemoryStore()
    config = PaymentConfig.for_total(STRIPE_MOCK, 3326, ESCROW)
    first, second = await requested(store, config), await requested(store, config)
    commit = mock_commit(config, first, tx="STRIPE_MOCK:shared")
    assert (
        await verify_commit(store, commit, sender=BUYER, config=config, gateway=FakeLedger(None))
    )[0]
    replay = mock_commit(config, second, tx="STRIPE_MOCK:shared")
    ok, why = await verify_commit(
        store, replay, sender=BUYER, config=config, gateway=FakeLedger(None)
    )
    assert not ok and "transaction replay" in why


async def test_fet_commit_requires_matching_on_chain_transfer() -> None:
    store = MemoryStore()
    config = PaymentConfig.for_total(FET_DIRECT, 3326, ESCROW)
    reference = await requested(store, config)
    ledger = FakeLedger(transfer(config.amount))
    ok, why = await verify_commit(
        store, fet_commit(config, reference), sender=BUYER, config=config, gateway=ledger
    )
    assert (ok, why) == (True, "verified")
    assert ledger.calls == [("TX1", "stable-testnet")]
    assert (await store.get_payment(reference))["buyer_wallet"] == BUYER_WALLET


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        (None, "not found"),
        (TxEvidence(successful=False, events={}), "failed on chain"),
        (transfer(Decimal("0.000001")), "did not match"),
        (transfer(Decimal("0.003326"), recipient="fetch1elsewhere"), "did not match"),
        (transfer(Decimal("0.003326"), sender="fetch1stranger"), "did not match"),
    ],
)
async def test_bad_fet_evidence_never_verifies(evidence, reason) -> None:
    store = MemoryStore()
    config = PaymentConfig.for_total(FET_DIRECT, 3326, ESCROW)
    reference = await requested(store, config)
    ok, why = await verify_commit(
        store,
        fet_commit(config, reference),
        sender=BUYER,
        config=config,
        gateway=FakeLedger(evidence),
    )
    assert not ok and reason in why
    assert (await store.get_payment(reference))["status"] == "requested"


def test_evidence_accepts_multi_coin_and_list_attributes() -> None:
    evidence = TxEvidence(
        successful=True,
        events={
            "transfer": [
                {"sender": "fetch1fee", "recipient": "fetch1validator", "amount": "5atestfet"},
                {
                    "sender": [BUYER_WALLET],
                    "recipient": [ESCROW],
                    "amount": ["10uother,3326000000000000atestfet"],
                },
            ]
        },
    )
    assert verify_tx_evidence(
        evidence,
        expected_sender=BUYER_WALLET,
        expected_recipient=ESCROW,
        expected_amount=Decimal("0.003326"),
        expected_denom="atestfet",
    ) == (True, "verified")


# ---------------------------------------------------------------------------
# Escrow settlement
# ---------------------------------------------------------------------------


class FakePayouts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int]] = []

    async def transfer(self, recipient: str, atomic_amount: int, memo: str) -> str:
        self.sent.append((recipient, atomic_amount))
        return f"TESTNET_TX_{len(self.sent)}"


def test_usd_settlement_pays_each_seller_exactly_its_quote() -> None:
    payment = {"reference": "ref", "currency": "USD", "amount": "33.26"}
    plan = escrow.settlement_plan(payment, BUNDLE)
    payouts = {e["party"]: Decimal(e["amount"]) for e in plan if e["kind"] == "payout"}
    assert payouts == {
        "gear_share": Decimal("9.00"),
        "studio_rooms": Decimal("12.00"),
        "westwood_print": Decimal("11.00"),
    }
    assert sum(Decimal(e["amount"]) for e in plan) == Decimal("33.26")
    assert len({e["entry_id"] for e in plan}) == len(plan)


def test_fet_settlement_never_pays_out_more_than_was_paid() -> None:
    payment = {"reference": "ref", "currency": "FET", "amount": "0.003326"}
    plan = escrow.settlement_plan(payment, BUNDLE)
    atomic = [amount_to_atomic(Decimal(e["amount"])) for e in plan]
    assert sum(atomic) == amount_to_atomic(Decimal("0.003326"))
    fee = next(e for e in plan if e["kind"] == "fee")
    assert Decimal(fee["amount"]) > 0


async def test_settlement_is_idempotent_and_mock_moves_nothing(registry_env) -> None:
    store = MemoryStore()
    payment = {"reference": "ref", "currency": "USD", "amount": "33.26"}
    gateway = FakePayouts()
    first = await escrow.settle_booking(store, gateway, payment, BUNDLE)
    second = await escrow.settle_booking(store, gateway, payment, BUNDLE)
    assert gateway.sent == []
    assert [e["transaction_id"] for e in first] == [e["transaction_id"] for e in second]
    payouts = [e for e in first if e["kind"] == "payout"]
    assert all(e["status"] == "simulated" for e in payouts)
    assert all(e["transaction_id"].startswith("STRIPE_MOCK_PAYOUT:") for e in payouts)
    assert "no money moved" in escrow.note_for(payouts[0])
    assert len(await store.list_escrow_entries("ref")) == 4


async def test_fet_payouts_are_only_recorded_unless_enabled(registry_env, monkeypatch) -> None:
    payment = {"reference": "ref", "currency": "FET", "amount": "0.003326"}
    gateway = FakePayouts()
    recorded = await escrow.settle_booking(MemoryStore(), gateway, payment, BUNDLE)
    assert {e["status"] for e in recorded if e["kind"] == "payout"} == {"scheduled"}
    assert gateway.sent == []

    monkeypatch.setenv("FET_PAYOUTS_ENABLED", "true")
    store = MemoryStore()
    sent = await escrow.settle_booking(store, gateway, payment, BUNDLE)
    await escrow.settle_booking(store, gateway, payment, BUNDLE)
    assert {e["status"] for e in sent if e["kind"] == "payout"} == {"sent"}
    assert len(gateway.sent) == 3  # once per seller, even when settlement is retried
    assert {recipient for recipient, _ in gateway.sent} == {
        "fetch1gearshare",
        "fetch1studiorooms",
        "fetch1westwoodprint",
    }


async def test_refund_is_recorded_once() -> None:
    store = MemoryStore()
    payment = {"reference": "ref", "currency": "USD", "amount": "33.26", "buyer_wallet": BUYER}
    first = await escrow.refund_buyer(store, None, payment)
    second = await escrow.refund_buyer(store, None, payment)
    assert first["status"] == "simulated"
    assert first["transaction_id"] == second["transaction_id"]
    assert first["transaction_id"].startswith("STRIPE_MOCK_REFUND:")
