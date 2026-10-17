from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from network import USER
from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent, TextContent
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    RequestPayment,
)

from resourcepilot.cards import EXAMPLE_REQUEST
from resourcepilot.checkout import hold_id_for, hold_plan, round_complete, round_outcome
from resourcepilot.clock import later
from resourcepilot.payment import TxEvidence, amount_to_atomic
from resourcepilot.protocols import HoldConfirmed

LINES = [
    {"seller_id": "gear_share", "seller_name": "Campus Gear Share", "item_id": "mirrorless_kit",
     "quantity": 1, "subtotal_cents": 900},
    {"seller_id": "studio_rooms", "seller_name": "Hilltop Studio Rooms",
     "item_id": "podcast_studio", "quantity": 1, "subtotal_cents": 1200},
]  # fmt: skip


def test_hold_plan_targets_registered_addresses_with_stable_ids(registry_env) -> None:
    plan = hold_plan("req-1", {"lines": LINES})
    assert list(plan) == [hold_id_for("req-1", 0), hold_id_for("req-1", 1)]
    assert hold_plan("req-1", {"lines": LINES}) == plan
    assert [hold["address"] for hold in plan.values()] == [
        registry_env["gear_share"],
        registry_env["studio_rooms"],
    ]


def test_hold_plan_refuses_unregistered_sellers(registry_env, monkeypatch) -> None:
    monkeypatch.delenv("STUDIO_ROOMS_ADDRESS")
    with pytest.raises(ValueError, match="No registered agent address"):
        hold_plan("req-1", {"lines": LINES})


def _round(replies: dict) -> dict:
    holds = {"a": {"seller_id": "gear_share", "seller_name": "Gear"},
             "b": {"seller_id": "studio_rooms", "seller_name": "Studio"}}  # fmt: skip
    return {"phase": "hold", "holds": holds, "replies": replies}


def test_round_ends_on_first_rejection_or_when_everyone_answered() -> None:
    assert not round_complete(_round({"a": {"status": "held"}}))
    assert round_complete(_round({"a": {"status": "rejected", "reason": "busy"}}))
    assert round_complete(_round({"a": {"status": "held"}, "b": {"status": "held"}}))


def test_silence_counts_as_failure_only_after_the_deadline() -> None:
    declined = _round({"a": {"status": "rejected", "reason": "busy"}})
    early = round_outcome(declined, timed_out=False)
    assert [h["seller_id"] for h in early["failed"]] == ["gear_share"]
    assert [h["seller_id"] for h in early["cancelled"]] == ["studio_rooms"]
    late = round_outcome(_round({"a": {"status": "held", "expires_at": "x"}}), timed_out=True)
    assert late["confirmed"][0]["expires_at"] == "x"
    assert late["failed"][0]["reason"] == "Did not answer in time"


# ---------------------------------------------------------------------------
# End-to-end checkout through the real agent handlers
# ---------------------------------------------------------------------------


def chat(body: str | dict) -> ChatMessage:
    text = body if isinstance(body, str) else json.dumps(body)
    return ChatMessage(
        timestamp=datetime.now(UTC), msg_id=uuid4(), content=[TextContent(type="text", text=text)]
    )


def last_card(network, kind: str | None = None) -> tuple[str, dict, str]:
    found = [card for card in network.cards() if kind is None or card[0] == kind]
    assert found, f"no {kind or 'card'} received; texts: {network.texts()}"
    return found[-1]


def workflow(network) -> dict:
    return next(iter(network.store.workflows.values()))


def payment_requests(network) -> list[RequestPayment]:
    return [message for message in network.inbox if isinstance(message, RequestPayment)]


def mock_commit(request: RequestPayment, *, buyer: str = USER) -> CommitPayment:
    tx = f"STRIPE_MOCK:{uuid4()}"
    return CommitPayment(
        funds=request.accepted_funds[0],
        recipient=request.recipient,
        transaction_id=tx,
        reference=request.reference,
        description="Mock Stripe test commit",
        metadata={
            "stripe_mock_buyer": buyer,
            "stripe_mock_payment_intent": tx,
            "moves_funds": "false",
        },
    )


async def hold_first_bundle(network) -> None:
    await network.send_user(chat(EXAMPLE_REQUEST))
    _, carousel, _ = last_card(network, "carousel")
    await network.send_user(chat(carousel["items"][0]["primary_cta"]["selection"]))
    _, detail, _ = last_card(network, "detail")
    assert detail["ctas"][0]["selection"]["action"] == "place_holds"
    await network.send_user(chat(detail["ctas"][0]["selection"]))


async def request_payment(network, method: str) -> RequestPayment:
    _, review, _ = last_card(network, "review")
    await network.send_user(chat(review["approve_cta"]["selection"]))
    _, options, _ = last_card(network, "custom")
    buttons = [child for child in options["root"]["children"] if child["type"] == "button"]
    choice = next(
        button
        for button in buttons
        if button["action"]["selection"].get("payment_method") == method
    )
    await network.send_user(chat(choice["action"]["selection"]))
    return payment_requests(network)[-1]


def terminal_flags(network) -> list[str]:
    return [
        content.metadata.get("is_terminal", "false")
        for message in network.inbox
        if isinstance(message, ChatMessage)
        for content in message.content
        if isinstance(content, MetadataContent)
    ]


async def test_mock_stripe_checkout_books_every_seller_exactly_once(network) -> None:
    await hold_first_bundle(network)
    narration = last_card(network, "review")[2]
    assert "holding" in narration
    holds = list(network.store.holds.values())
    assert holds and {hold["status"] for hold in holds} == {"held"}
    assert network.store.payments == {}  # nothing is requested until every hold exists

    request = await request_payment(network, "stripe_mock")
    assert request.accepted_funds[0].currency == "USD"
    assert request.metadata["moves_funds"] == "false"
    assert request.metadata["stripe_mock"] == "true"
    commit = mock_commit(request)
    await network.send_user(commit)

    assert any(isinstance(message, CompletePayment) for message in network.inbox)
    _, receipt, _ = last_card(network, "detail")
    assert receipt["title"].startswith("Booked with")
    assert terminal_flags(network)[-1] == "true"
    state = workflow(network)
    assert state["status"] == "booked"
    assert network.store.payments[request.reference]["status"] == "delivered"
    assert {hold["status"] for hold in network.store.holds.values()} == {"committed"}

    ledger = list(network.store.escrow.values())
    payouts = [entry for entry in ledger if entry["kind"] == "payout"]
    assert {entry["party"] for entry in payouts} == {
        line["seller_id"] for line in state["bundle"]["lines"]
    }
    assert {entry["status"] for entry in payouts} == {"simulated"}
    assert sum(Decimal(entry["amount"]) for entry in ledger) == Decimal(
        request.accepted_funds[0].amount
    )
    codes = [row["value"] for row in receipt["summary_rows"]]
    assert all(
        booking["confirmation_code"] in " ".join(codes) for booking in state["receipt"]["bookings"]
    )

    # A duplicate CommitPayment returns the same receipt and never books or pays twice.
    stats_before = {key: dict(value) for key, value in network.store.stats.items()}
    await network.send_user(commit)
    assert "already booked" in last_card(network, "detail")[2]
    assert len(network.store.escrow) == len(ledger)
    assert network.store.stats == stats_before

    await network.send_user(chat({"action": "show_bookings"}))
    _, bookings, _ = last_card(network, "detail")
    assert bookings["title"] == "Your recent bookings"
    assert bookings["summary_rows"][0]["label"] != "Bookings"


async def test_test_fet_is_verified_on_chain_before_booking(network, monkeypatch) -> None:
    orchestrator = network.by_key["orchestrator"]
    wallet = "fetch1testbuyerwallet"
    evidence: dict = {}

    class FakeLedger:
        async def query(self, transaction_id: str, network_name: str) -> TxEvidence:
            assert network_name == "stable-testnet"
            return TxEvidence(successful=True, events={"transfer": evidence})

    monkeypatch.setattr(orchestrator, "ledger", FakeLedger())
    await hold_first_bundle(network)
    request = await request_payment(network, "fet_direct")
    funds = request.accepted_funds[0]
    assert (funds.currency, request.recipient) == ("FET", orchestrator.ESCROW_WALLET)
    assert Decimal(funds.amount) <= Decimal("0.01")

    def commit(tx: str) -> CommitPayment:
        return CommitPayment(
            funds=funds,
            recipient=request.recipient,
            transaction_id=tx,
            reference=request.reference,
            description="test FET",
            metadata={"buyer_fet_wallet": wallet, "fet_network": "stable-testnet"},
        )

    # Paid to the wrong wallet: cancelled, and nothing is booked.
    evidence.update(sender=wallet, recipient="fetch1elsewhere",
                    amount=f"{amount_to_atomic(Decimal(funds.amount))}atestfet")  # fmt: skip
    await network.send_user(commit("TX_WRONG"))
    assert isinstance(network.inbox[-1], CancelPayment)
    assert workflow(network)["status"] == "payment_requested"
    assert {hold["status"] for hold in network.store.holds.values()} == {"held"}

    evidence.update(recipient=orchestrator.ESCROW_WALLET)
    await network.send_user(commit("TX_GOOD"))
    assert workflow(network)["status"] == "booked"
    payouts = [entry for entry in network.store.escrow.values() if entry["kind"] == "payout"]
    assert {entry["status"] for entry in payouts} == {"scheduled"}  # FET_PAYOUTS_ENABLED=false


async def test_one_declined_hold_releases_the_rest_and_charges_nothing(network) -> None:
    await network.store.set_fault_mode("studio_rooms", "reject_holds")
    await hold_first_bundle(network)
    assert not [card for card in network.cards() if card[0] == "review"]
    assert any("Hilltop Studio Rooms could not hold" in text for text in network.texts())
    kind, _, narration = last_card(network)
    assert kind == "carousel"
    # The Planner rerouted, and says plainly that no other trusted seller has a podcast room.
    assert "Rerouted around: Hilltop Studio Rooms" in narration
    assert "No trusted seller had: podcast_room" in narration
    assert {hold["status"] for hold in network.store.holds.values()} == {"released"}
    assert network.store.payments == {}
    assert workflow(network)["status"] == "awaiting_selection"
    assert network.store.stats["studio_rooms"]["holds_failed"] == 1
    assert network.store.stats["gear_share"]["holds_confirmed"] == 1


async def test_a_silent_seller_times_out_the_hold_round(network) -> None:
    await network.store.set_fault_mode("studio_rooms", "ghost_holds")
    await hold_first_bundle(network)
    assert workflow(network)["stage"] == "holds"
    await network.advance(6)
    assert any("Did not answer in time" in text for text in network.texts())
    assert {hold["status"] for hold in network.store.holds.values()} == {"released"}
    assert network.store.stats["studio_rooms"]["holds_failed"] == 1


async def test_failed_commit_cancels_every_booking_and_pays_no_seller(network) -> None:
    await network.store.set_fault_mode("studio_rooms", "ghost_commits")
    await hold_first_bundle(network)
    request = await request_payment(network, "stripe_mock")
    await network.send_user(mock_commit(request))
    assert workflow(network)["stage"] == "booking"
    await network.advance(6)
    assert workflow(network)["status"] == "booking_failed"
    assert network.store.payments[request.reference]["status"] == "released"
    assert {hold["status"] for hold in network.store.holds.values()} == {"released"}
    assert network.store.escrow == {}
    assert "no seller was paid" in last_card(network)[2]


async def test_buyer_can_release_holds_before_paying(network) -> None:
    await hold_first_bundle(network)
    request = await request_payment(network, "stripe_mock")
    _, options, _ = last_card(network, "custom")
    release = next(
        child for child in options["root"]["children"] if child.get("label") == "Release holds"
    )
    await network.send_user(chat(release["action"]["selection"]))
    assert {hold["status"] for hold in network.store.holds.values()} == {"released"}
    assert network.store.payments[request.reference]["status"] == "cancelled"
    assert last_card(network)[0] == "carousel"
    # Paying the cancelled request afterwards is refused.
    await network.send_user(mock_commit(request))
    assert isinstance(network.inbox[-1], CancelPayment)


async def test_double_clicking_hold_everything_places_one_set_of_holds(network) -> None:
    await network.store.set_fault_mode("studio_rooms", "ghost_holds")
    await hold_first_bundle(network)
    _, detail, _ = last_card(network, "detail")
    await network.send_user(chat(detail["ctas"][0]["selection"]))
    assert "already in progress" in network.texts()[-1]
    rounds = [r for r in network.store.rounds.values() if r["owner_role"] == "fulfillment"]
    assert len(rounds) == 1


async def test_hold_replies_are_authenticated_and_price_checked(network) -> None:
    await network.store.set_fault_mode("studio_rooms", "ghost_holds")
    await hold_first_bundle(network)
    current = next(r for r in network.store.rounds.values() if r["owner_role"] == "fulfillment")
    hold = next(h for h in current["holds"].values() if h["seller_id"] == "studio_rooms")

    def confirmation(subtotal: int) -> HoldConfirmed:
        return HoldConfirmed(
            round_id=current["round_id"],
            hold_id=hold["hold_id"],
            seller_id="studio_rooms",
            expires_at=later(600),
            subtotal_cents=subtotal,
        )

    await network.send_user(confirmation(hold["subtotal_cents"]), to="fulfillment")
    assert hold["hold_id"] not in network.store.rounds[current["round_id"]]["replies"]

    network.queue.append(
        (hold["address"], network.address("fulfillment"), confirmation(1), "session-under-test")
    )
    await network.run()
    assert any("different price" in text for text in network.texts())
    assert workflow(network)["status"] == "awaiting_selection"


DEMO_BUYER = Path(__file__).resolve().parents[1] / "scripts" / "demo_payment_buyer.py"


def load_demo_buyer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_demo_payment_buyer", DEMO_BUYER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_demo_buyer_script_clicks_through_the_real_cards(network, monkeypatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_ADDRESS", network.address("orchestrator"))
    demo = load_demo_buyer()
    await network.send_user(chat(EXAMPLE_REQUEST))
    while not payment_requests(network):
        kind, payload, _ = last_card(network)
        selection = demo.next_selection(kind, payload)
        assert selection, f"demo buyer stuck on a {kind} card"
        await network.send_user(chat(selection))
    request = payment_requests(network)[-1]
    assert request.accepted_funds[0].payment_method == "stripe_mock"
    await network.send_user(mock_commit(request))
    assert workflow(network)["status"] == "booked"


async def test_full_checkout_and_duplicate_commit_on_postgres(postgres_network) -> None:
    network = postgres_network
    await hold_first_bundle(network)
    request = await request_payment(network, "stripe_mock")
    commit = mock_commit(request)
    await network.send_user(commit)
    await network.send_user(commit)

    payment = await network.store.get_payment(request.reference)
    assert payment["status"] == "delivered"
    state = await network.store.get_workflow(payment["workflow_id"])
    assert state["status"] == "booked"
    entries = await network.store.list_escrow_entries(request.reference)
    assert len(entries) == 4
    assert {entry["status"] for entry in entries if entry["kind"] == "payout"} == {"simulated"}
    for hold in state["holds"]:
        assert (await network.store.get_hold(hold["hold_id"]))["status"] == "committed"
    records = await network.store.seller_records(sorted(sellers_in(state["bundle"])))
    assert {record["bookings_committed"] for record in records.values()} == {1}
    events = [
        event["event_type"] for event in await network.store.list_events(state["workflow_id"])
    ]
    assert events.count("booked") == 1


async def test_reroute_after_a_silent_seller_on_postgres(postgres_network) -> None:
    network = postgres_network
    await network.store.set_fault_mode("westwood_print", "ghost_holds")
    await hold_first_bundle(network)
    await network.advance(6)
    _, carousel, narration = last_card(network, "carousel")
    assert "Rerouted around: Westwood Print Lab" in narration
    workflow_id = carousel["items"][0]["primary_cta"]["selection"]["workflow_id"]
    state = await network.store.get_workflow(workflow_id)
    assert state["status"] == "awaiting_selection"
    assert all("westwood_print" not in sellers_in(bundle) for bundle in state["data"]["bundles"])


def sellers_in(bundle: dict) -> set[str]:
    return {line["seller_id"] for line in bundle["lines"]}
