"""The paid hall match: Stripe test mode and testnet FET, all mocked. No real payment is made."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from network import SESSION, _load, build_env, click, last_with
from test_network import EMAIL_USER, chat, completed_roles, housing_form, last_card, submit
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    Funds,
    RejectPayment,
    RequestPayment,
)

from resourcepilot import payments
from resourcepilot.http import Http, ProviderError
from resourcepilot.payments import LedgerTx, PaymentConfig, Transfer

SECRET = "sk_test_FAKEKEY0001"
PUBLISHABLE = "pk_test_FAKEKEY0001"
BUYER = "fetch1qyqszqgpqyqszqgpqyqszqgpqyqszqgpjnp7du"
OTHER_WALLET = "fetch1zcjduepqpqyqszqgpqyqszqgpqyqszqgx3c8nn"
TX = "A" * 64
STRIPE = PaymentConfig(price_cents=100, stripe_secret=SECRET, stripe_publishable=PUBLISHABLE)
FET = PaymentConfig(price_fet="1", accept_fet=True)
BOTH = PaymentConfig(
    price_cents=100,
    price_fet="1",
    stripe_secret=SECRET,
    stripe_publishable=PUBLISHABLE,
    accept_fet=True,
)


# ---------------------------------------------------------------------------
# Settings: test mode only
# ---------------------------------------------------------------------------


def test_payments_are_off_until_configured() -> None:
    config = payments.load_config({})
    assert not config.enabled and config.price_labels() == []
    assert (config.price_cents, config.price_fet) == (100, "1")


@pytest.mark.parametrize(
    "env",
    [
        {
            "STRIPE_SECRET_KEY": "sk_live_REALMONEY01",
            "STRIPE_PUBLISHABLE_KEY": PUBLISHABLE,
        },
        {
            "STRIPE_SECRET_KEY": "rk_live_REALMONEY01",
            "STRIPE_PUBLISHABLE_KEY": PUBLISHABLE,
        },
        {"STRIPE_SECRET_KEY": SECRET, "STRIPE_PUBLISHABLE_KEY": "pk_live_REALMONEY01"},
        {"STRIPE_SECRET_KEY": SECRET},
        {"HOUSING_MATCH_PRICE_CENTS": "10"},
        {"HOUSING_MATCH_PRICE_CENTS": "99999"},
        {"HOUSING_MATCH_PRICE_FET": "-1"},
        {"HOUSING_MATCH_PRICE_FET": "0.0000001"},
        {"HOUSING_MATCH_PRICE_FET": "lots"},
    ],
)
def test_live_keys_and_bad_prices_are_refused(env) -> None:
    with pytest.raises(ValueError) as error:
        payments.load_config(env)
    for value in env.values():
        if value.startswith(("sk_", "rk_")):
            assert value not in str(error.value)


def test_test_mode_settings_load_and_never_show_the_secret() -> None:
    config = payments.load_config(
        {
            "STRIPE_SECRET_KEY": SECRET,
            "STRIPE_PUBLISHABLE_KEY": PUBLISHABLE,
            "ACCEPT_TESTNET_FET": "true",
            "HOUSING_MATCH_PRICE_CENTS": "150",
            "HOUSING_MATCH_PRICE_FET": "2.50",
        }
    )
    assert config.stripe_enabled and config.accept_fet
    assert config.price_labels() == [
        "$1.50 by card (Stripe test mode)",
        "2.5 FET on the Fetch testnet",
    ]
    assert config.price_atto == 25 * 10**17
    assert SECRET not in repr(config)


def test_a_live_stripe_key_stops_the_orchestrator_from_starting(monkeypatch) -> None:
    from network import ROOT

    for key, value in build_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AGENT_ROLE", "orchestrator")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_REALMONEY01")
    monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", PUBLISHABLE)
    with pytest.raises(ValueError, match="test-mode"):
        _load(ROOT / "agents" / "run.py", "_test_agent_live_key")


# ---------------------------------------------------------------------------
# Stripe checks
# ---------------------------------------------------------------------------


def paid_session(workflow_id: str, **changes: Any) -> dict[str, Any]:
    session = {
        "id": "cs_test_a1b2c3d4e5f6g7h8",
        "livemode": False,
        "payment_status": "paid",
        "currency": "usd",
        "amount_total": 100,
        "client_reference_id": workflow_id,
        "metadata": {"workflow_id": workflow_id, "service": "housing_match"},
    }
    return {**session, **changes}


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({}, None),
        ({"payment_status": "unpaid"}, "not marked that checkout as paid"),
        ({"livemode": True}, "not a Stripe test-mode payment"),
        ({"amount_total": 1}, "not for the hall match price"),
        ({"currency": "eur"}, "not for the hall match price"),
        ({"client_reference_id": "other"}, "different request"),
        ({"metadata": {"workflow_id": "other"}}, "different request"),
    ],
)
def test_stripe_session_must_be_paid_test_mode_and_bound_to_the_workflow(changes, problem) -> None:
    found = payments.stripe_problem(paid_session("wf-1", **changes), STRIPE, workflow_id="wf-1")
    assert found is None if problem is None else problem in str(found)


def test_checkout_is_one_priced_line_bound_to_the_workflow() -> None:
    form = payments.checkout_form(STRIPE, workflow_id="wf-1", school="Purdue")
    assert form["ui_mode"] == "embedded" and form["mode"] == "payment"
    assert form["line_items[0][price_data][unit_amount]"] == "100"
    assert form["line_items[0][price_data][currency]"] == "usd"
    assert form["client_reference_id"] == form["metadata[workflow_id]"] == "wf-1"
    lifetime = int(form["expires_at"]) - int(datetime.now(UTC).timestamp())
    assert lifetime >= 30 * 60 - 5


async def test_stripe_errors_never_include_the_key_or_body() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"Invalid API Key {SECRET}"}})

    http = Http(httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    with pytest.raises(ProviderError) as error:
        await payments.create_checkout(http, STRIPE, workflow_id="wf-1", school="Purdue")
    assert "STRIPE_SECRET_KEY" in str(error.value)
    assert SECRET not in str(error.value) and "Invalid" not in str(error.value)


async def test_a_live_mode_checkout_from_stripe_is_refused() -> None:
    def live(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "cs_live_a1b2c3d4e5f6", "client_secret": "x", "livemode": True}
        )

    http = Http(httpx.AsyncClient(transport=httpx.MockTransport(live)))
    with pytest.raises(ProviderError, match="unexpected checkout"):
        await payments.create_checkout(http, STRIPE, workflow_id="wf-1", school="Purdue")


# ---------------------------------------------------------------------------
# FET checks: the testnet transaction, never the buyer's word
# ---------------------------------------------------------------------------

WALLET = "fetch1resourcepilotwallet"
PRICE = 10**18


def tx(*transfers: Transfer, succeeded: bool = True) -> LedgerTx:
    return LedgerTx(succeeded=succeeded, transfers=transfers)


@pytest.mark.parametrize(
    ("transaction", "problem"),
    [
        (tx(Transfer(BUYER, WALLET, "atestfet", PRICE)), None),
        (
            tx(
                Transfer(BUYER, WALLET, "atestfet", PRICE // 2),
                Transfer(BUYER, WALLET, "atestfet", PRICE // 2),
            ),
            None,
        ),
        (None, "does not show that transaction"),
        (tx(Transfer(BUYER, WALLET, "atestfet", PRICE), succeeded=False), "failed"),
        (tx(Transfer(BUYER, WALLET, "atestfet", PRICE - 1)), "less than"),
        (tx(Transfer(BUYER, OTHER_WALLET, "atestfet", PRICE)), "did not send"),
        (tx(Transfer(OTHER_WALLET, WALLET, "atestfet", PRICE)), "did not send"),
        (tx(Transfer(BUYER, WALLET, "afet", PRICE)), "did not send"),
    ],
)
def test_fet_transfer_must_reach_our_wallet_from_the_buyer_on_testnet(transaction, problem) -> None:
    found = payments.fet_problem(transaction, buyer=BUYER, wallet=WALLET, price_atto=PRICE)
    assert found is None if problem is None else problem in str(found)


async def test_fet_verification_rejects_bad_input_before_reading_the_chain(monkeypatch) -> None:
    def never(tx_hash: str) -> LedgerTx:
        raise AssertionError("the testnet must not be read")

    monkeypatch.setattr(payments, "TX_READER", never)
    assert "transaction hash" in str(
        await payments.verify_fet(FET, tx_hash="not-a-hash", buyer=BUYER, wallet=WALLET)
    )
    assert "paying FET wallet" in str(
        await payments.verify_fet(FET, tx_hash=TX, buyer="", wallet=WALLET)
    )


async def test_an_unreachable_testnet_is_a_card_safe_error(monkeypatch) -> None:
    def down(tx_hash: str) -> LedgerTx:
        raise RuntimeError("grpc details with internals")

    monkeypatch.setattr(payments, "TX_READER", down)
    with pytest.raises(ProviderError) as error:
        await payments.verify_fet(FET, tx_hash=TX, buyer=BUYER, wallet=WALLET)
    assert "internals" not in str(error.value)


def test_the_payment_request_offers_test_rails_to_the_orchestrators_wallet() -> None:
    checkout = {
        "client_secret": "cs_secret",
        "checkout_session_id": "cs_test_1",
        "amount_cents": "100",
    }
    request = payments.payment_request(
        BOTH, workflow_id="wf-1", wallet=WALLET, school="Purdue", checkout=checkout
    )
    assert [(f.payment_method, f.currency, f.amount) for f in request.accepted_funds] == [
        ("stripe", "USD", "1.00"),
        ("fet_direct", "FET", "1"),
    ]
    assert request.recipient == WALLET and request.reference == "wf-1"
    assert request.metadata["fet_network"] == "stable-testnet"
    assert request.metadata["mainnet"] == "false"
    assert request.metadata["provider_agent_wallet"] == WALLET
    assert request.metadata["stripe"] == checkout
    assert "never your school" in str(request.metadata["content"])
    with pytest.raises(ValueError):
        payments.payment_request(
            PaymentConfig(), workflow_id="wf-1", wallet=WALLET, school="x", checkout=None
        )


# ---------------------------------------------------------------------------
# The whole flow across the eight agents
# ---------------------------------------------------------------------------


class FakeStripe:
    """Records every Stripe call; a session is paid only when a test says so."""

    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []
        self.paid: set[str] = set()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.fail_create = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.stripe.com"
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        if request.method == "POST" and request.url.path == "/v1/checkout/sessions":
            if self.fail_create:
                return httpx.Response(500, json={"error": {"message": "boom"}})
            form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            self.created.append(form)
            session_id = f"cs_test_fake{len(self.created):012d}"
            self.sessions[session_id] = form
            return httpx.Response(
                200,
                json={"id": session_id, "client_secret": f"{session_id}_secret", "livemode": False},
            )
        session_id = request.url.path.rsplit("/", 1)[-1]
        form = self.sessions.get(session_id)
        if request.method != "GET" or form is None:
            return httpx.Response(404, json={})
        return httpx.Response(
            200,
            json={
                "id": session_id,
                "livemode": False,
                "payment_status": "paid" if session_id in self.paid else "unpaid",
                "currency": "usd",
                "amount_total": int(form["line_items[0][price_data][unit_amount]"]),
                "client_reference_id": form["client_reference_id"],
                "metadata": {"workflow_id": form["metadata[workflow_id]"]},
            },
        )


def enable_payments(network, monkeypatch, config: PaymentConfig) -> FakeStripe:
    orchestrator = network.by_key["orchestrator"]
    monkeypatch.setattr(orchestrator, "PAYMENTS", config)
    fake = FakeStripe()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    monkeypatch.setattr(orchestrator, "HTTP", Http(client, network.store))
    return fake


def wallet(network) -> str:
    return str(network.by_key["orchestrator"].agent.wallet.address())


def sent(network, model: type) -> list[Any]:
    return [message for message in network.inbox if isinstance(message, model)]


def workflow(network) -> dict[str, Any]:
    return list(network.store.workflows.values())[-1]


def stripe_commit(session_id: str, reference: str | None) -> CommitPayment:
    return CommitPayment(
        funds=Funds(amount="1.00", currency="USD", payment_method="stripe"),
        recipient="ignored",
        transaction_id=session_id,
        reference=reference,
    )


def fet_commit(reference: str | None, tx_hash: str = TX, claimed: str = "1") -> CommitPayment:
    return CommitPayment(
        funds=Funds(amount=claimed, currency="FET", payment_method="fet_direct"),
        recipient="ignored",
        transaction_id=tx_hash,
        reference=reference,
        metadata={"buyer_fet_wallet": BUYER},
    )


async def open_offer(network) -> tuple[str, dict[str, Any]]:
    """Search Purdue housing and stop at the paid-match offer."""
    await submit(network, housing_form(who="first_year", need_ac="true"))
    _, offer, narration = last_with(network, "unlock_housing")
    assert "Test mode only" in narration
    return str(workflow(network)["workflow_id"]), offer


async def test_stripe_payment_unlocks_the_planners_hall_match(network, monkeypatch) -> None:
    fake = enable_payments(network, monkeypatch, STRIPE)
    workflow_id, offer = await open_offer(network)
    assert completed_roles(network) == ["intake", "researcher", "verification"]
    assert workflow(network)["status"] == "awaiting_payment"
    text = json.dumps(offer)
    assert "4 halls re-checked" in text and "$1.00 by card (Stripe test mode)" in text
    assert "never your school" in text and "No real money moves" in text
    assert fake.created == []  # nothing is created before the student asks

    # The facts stay free while the match waits.
    await network.send_user(chat(click(offer, "show_housing_facts")))
    _, facts, _ = last_card(network, "detail")
    assert facts["title"].startswith("Housing at Purdue")

    await network.send_user(chat(click(offer, "unlock_housing")))
    await network.send_user(chat(click(offer, "unlock_housing")))  # a double click
    assert len(fake.created) == 1
    (request,) = sent(network, RequestPayment)
    assert request.reference == workflow_id and request.recipient == wallet(network)
    assert [f.payment_method for f in request.accepted_funds] == ["stripe"]
    stripe = request.metadata["stripe"]
    assert stripe["publishable_key"] == PUBLISHABLE and stripe["ui_mode"] == "embedded"
    session_id = stripe["checkout_session_id"]
    assert last_card(network)[1]["root"]["title"] == "Finish the payment"

    fake.paid.add(session_id)
    await network.send_user(stripe_commit(session_id, workflow_id))
    assert [m.transaction_id for m in sent(network, CompletePayment)] == [session_id]
    assert completed_roles(network) == ["intake", "researcher", "verification", "planner"]
    _, carousel, _ = last_card(network, "carousel")
    assert [item["title"] for item in carousel["items"]][:2] == ["Earhart", "Cary Quadrangle"]
    state = workflow(network)
    assert state["status"] == "answered"
    assert state["data"]["payment"]["status"] == "paid"
    events = [e["event_type"] for e in network.store.events[workflow_id]]
    assert "payment_requested" in events and "payment_completed" in events

    # A replayed commit cannot unlock or charge anything again.
    await network.send_user(stripe_commit(session_id, workflow_id))
    assert len(sent(network, CompletePayment)) == 1
    stored = json.dumps([network.store.workflows, network.store.events], default=str)
    assert SECRET not in stored and f"{session_id}_secret" not in stored
    assert EMAIL_USER not in stored
    assert all(SECRET not in text for text in network.texts())


async def test_an_unpaid_or_foreign_checkout_never_unlocks(network, monkeypatch) -> None:
    fake = enable_payments(network, monkeypatch, STRIPE)
    workflow_id, offer = await open_offer(network)
    await network.send_user(chat(click(offer, "unlock_housing")))
    session_id = sent(network, RequestPayment)[-1].metadata["stripe"]["checkout_session_id"]

    await network.send_user(stripe_commit(session_id, workflow_id))  # not paid yet
    (cancel,) = sent(network, CancelPayment)
    assert "not marked that checkout as paid" in str(cancel.reason)
    _, notice, _ = last_with(network, "unlock_housing")
    assert notice["root"]["title"] == "Payment not confirmed"
    assert workflow(network)["status"] == "awaiting_payment"
    assert "planner" not in completed_roles(network)

    # Try again opens a fresh checkout; a checkout made for another request is refused.
    await network.send_user(chat(click(notice, "unlock_housing")))
    assert len(fake.created) == 2
    fresh = sent(network, RequestPayment)[-1].metadata["stripe"]["checkout_session_id"]
    fake.paid.update({session_id, fresh})
    await network.send_user(stripe_commit(session_id, workflow_id))
    assert "not created for this request" in str(sent(network, CancelPayment)[-1].reason)
    await network.send_user(chat(click(notice, "unlock_housing")))
    fresh = sent(network, RequestPayment)[-1].metadata["stripe"]["checkout_session_id"]
    fake.paid.add(fresh)
    await network.send_user(stripe_commit(fresh, workflow_id))
    assert len(sent(network, CompletePayment)) == 1
    assert workflow(network)["status"] == "answered"


async def test_testnet_fet_is_checked_on_chain_and_used_once(network, monkeypatch) -> None:
    enable_payments(network, monkeypatch, FET)
    reads: list[str] = []

    def reader(tx_hash: str) -> LedgerTx:
        reads.append(tx_hash)
        return tx(Transfer(BUYER, wallet(network), "atestfet", PRICE))

    monkeypatch.setattr(payments, "TX_READER", reader)
    workflow_id, offer = await open_offer(network)
    await network.send_user(chat(click(offer, "unlock_housing")))
    (request,) = sent(network, RequestPayment)
    assert [(f.payment_method, f.amount) for f in request.accepted_funds] == [("fet_direct", "1")]
    assert request.metadata["provider_agent_wallet"] == wallet(network)
    # The buyer's claimed amount is ignored: the chain decides.
    await network.send_user(fet_commit(workflow_id, claimed="1000"))
    assert reads == [TX]
    assert len(sent(network, CompletePayment)) == 1
    assert workflow(network)["data"]["payment"]["amount"] == "1 testnet FET"

    # The same transaction cannot pay for a second search.
    second, again = await open_offer(network)
    assert second != workflow_id
    await network.send_user(chat(click(again, "unlock_housing")))
    await network.send_user(fet_commit(second))
    assert "already used" in str(sent(network, CancelPayment)[-1].reason)
    assert workflow(network)["status"] == "awaiting_payment"


async def test_an_underpaid_fet_transfer_is_cancelled(network, monkeypatch) -> None:
    enable_payments(network, monkeypatch, FET)
    monkeypatch.setattr(
        payments,
        "TX_READER",
        lambda tx_hash: tx(Transfer(BUYER, wallet(network), "atestfet", PRICE // 10)),
    )
    workflow_id, offer = await open_offer(network)
    await network.send_user(chat(click(offer, "unlock_housing")))
    await network.send_user(fet_commit(workflow_id))
    assert "less than" in str(sent(network, CancelPayment)[-1].reason)
    assert sent(network, CompletePayment) == []
    assert "planner" not in completed_roles(network)


async def test_a_commit_from_someone_else_is_cancelled(network, monkeypatch) -> None:
    fake = enable_payments(network, monkeypatch, STRIPE)
    workflow_id, offer = await open_offer(network)
    await network.send_user(chat(click(offer, "unlock_housing")))
    session_id = sent(network, RequestPayment)[-1].metadata["stripe"]["checkout_session_id"]
    fake.paid.add(session_id)
    stranger = "agent1qstranger000000000000000000000000000000000000000000000000000"
    orchestrator = network.address("orchestrator")
    network.queue.append((stranger, orchestrator, stripe_commit(session_id, workflow_id), SESSION))
    await network.run()
    assert workflow(network)["status"] == "awaiting_payment"
    assert sent(network, CompletePayment) == []


async def test_declining_the_payment_shows_the_free_facts(network, monkeypatch) -> None:
    enable_payments(network, monkeypatch, STRIPE)
    _, offer = await open_offer(network)
    await network.send_user(chat(click(offer, "unlock_housing")))
    await network.send_user(RejectPayment(reason="Not now"))
    _, facts, narration = last_card(network, "detail")
    assert facts["title"].startswith("Housing at Purdue") and "No payment was taken" in narration
    assert workflow(network)["status"] == "awaiting_payment"
    # The offer can be taken up later.
    await network.send_user(chat(click(offer, "unlock_housing")))
    assert len(sent(network, RequestPayment)) == 2


async def test_a_failed_checkout_charges_nothing_and_can_be_retried(network, monkeypatch) -> None:
    fake = enable_payments(network, monkeypatch, STRIPE)
    fake.fail_create = True
    _, offer = await open_offer(network)
    await network.send_user(chat(click(offer, "unlock_housing")))
    _, notice, narration = last_with(network, "unlock_housing")
    assert notice["root"]["title"] == "Checkout not started" and "Nothing was charged" in narration
    assert sent(network, RequestPayment) == []
    fake.fail_create = False
    await network.send_user(chat(click(notice, "unlock_housing")))
    assert len(sent(network, RequestPayment)) == 1


async def test_no_halls_means_no_offer_and_no_charge(network, monkeypatch) -> None:
    fake = enable_payments(network, monkeypatch, STRIPE)
    await submit(network, housing_form("mit.edu"))
    _, card, _ = last_card(network, "detail")
    rows = {row["label"]: row["value"] for row in card["summary_rows"]}
    assert rows["Halls"].startswith("Couldn't read individual halls")
    assert completed_roles(network) == ["intake", "researcher", "verification", "planner"]
    assert fake.created == [] and sent(network, RequestPayment) == []


async def test_prose_can_never_ask_for_a_payment(network, monkeypatch) -> None:
    fake = enable_payments(network, monkeypatch, STRIPE)
    await open_offer(network)
    for text in ("unlock housing", "unlock_housing", "pay for my hall match"):
        await network.send_user(chat(text))
    assert fake.created == [] and sent(network, RequestPayment) == []
    assert workflow(network)["status"] == "awaiting_payment"


def test_fet_prices_convert_exactly() -> None:
    assert PaymentConfig(price_fet="0.000001").price_atto == 10**12
    assert Decimal(PaymentConfig(price_fet="12.5").price_atto) == Decimal("12.5") * 10**18
