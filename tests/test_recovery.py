"""Failure recovery through the real agent handlers: reroute, retry, refund, and timeouts."""

from __future__ import annotations

from network import SESSION, click, last_with
from test_checkout import (
    chat,
    hold_first_bundle,
    last_card,
    mock_commit,
    payment_requests,
    request_payment,
    terminal_flags,
    workflow,
)

from resourcepilot.cards import example_bundle_selection
from resourcepilot.clock import later
from resourcepilot.models import TaskResult


def sellers(bundle: dict) -> set[str]:
    return {line["seller_id"] for line in bundle["lines"]}


def event_types(network) -> list[str]:
    return [event["event_type"] for events in network.store.events.values() for event in events]


async def paid_but_commit_failed(network):
    await network.store.set_fault_mode("studio_rooms", "ghost_commits")
    await hold_first_bundle(network)
    request = await request_payment(network, "stripe_mock")
    await network.send_user(mock_commit(request))
    await network.advance(6)
    assert workflow(network)["status"] == "booking_failed"
    return request


async def test_silent_seller_is_rerouted_around_and_the_new_bundle_books(network) -> None:
    await network.store.set_fault_mode("westwood_print", "ghost_holds")
    await hold_first_bundle(network)
    assert "westwood_print" in sellers(workflow(network)["bundle"])
    await network.advance(6)

    _, carousel, narration = last_card(network, "carousel")
    assert "Westwood Print Lab could not hold your items" in " ".join(network.texts())
    assert "Rerouted around: Westwood Print Lab" in narration
    state = workflow(network)
    assert state["data"]["bundles"]
    assert all("westwood_print" not in sellers(bundle) for bundle in state["data"]["bundles"])
    assert "reroute_requested" in event_types(network)

    await network.send_user(chat(carousel["items"][0]["primary_cta"]["selection"]))
    _, detail, _ = last_card(network, "detail")
    await network.send_user(chat(detail["ctas"][0]["selection"]))
    request = await request_payment(network, "stripe_mock")
    await network.send_user(mock_commit(request))
    state = workflow(network)
    assert state["status"] == "booked"
    assert "gayley_copy" in sellers(state["bundle"])
    westwood = [h for h in network.store.holds.values() if h["seller_id"] == "westwood_print"]
    assert all(hold["status"] != "committed" for hold in westwood)
    assert network.store.stats["westwood_print"]["holds_failed"] == 1


async def test_retry_after_a_failed_commit_reuses_the_escrowed_payment(network) -> None:
    request = await paid_but_commit_failed(network)
    _, recovery, narration = last_with(network, "retry_booking")
    assert recovery["root"]["title"] == "Booking did not complete"
    assert "safe in escrow" in narration
    assert network.store.payments[request.reference]["status"] == "released"

    await network.store.set_fault_mode("studio_rooms", "none")
    await network.send_user(chat(click(recovery, "retry_booking")))
    assert workflow(network)["status"] == "booked"
    assert len(payment_requests(network)) == 1  # the buyer never pays twice
    assert network.store.payments[request.reference]["status"] == "delivered"
    payouts = [entry for entry in network.store.escrow.values() if entry["kind"] == "payout"]
    assert len(payouts) == 3
    assert "booking_retried" in event_types(network)


async def test_buyer_can_take_a_refund_after_a_failed_commit(network) -> None:
    request = await paid_but_commit_failed(network)
    _, recovery, _ = last_with(network, "refund")
    await network.send_user(chat(click(recovery, "refund")))

    _, refund, narration = last_card(network, "detail")
    assert refund["title"] == "Refunded"
    assert terminal_flags(network)[-1] == "true"
    assert "No seller was paid" in narration
    assert network.store.payments[request.reference]["status"] == "refunded"
    entries = list(network.store.escrow.values())
    assert [entry["kind"] for entry in entries] == ["refund"]
    assert entries[0]["transaction_id"].startswith("STRIPE_MOCK_REFUND:")

    await network.send_user(chat(click(recovery, "refund")))
    assert "not available" in network.texts()[-1]
    assert len(network.store.escrow) == 1


async def test_failed_booking_is_refunded_automatically_after_the_retry_window(network) -> None:
    request = await paid_but_commit_failed(network)
    await network.advance(601)
    assert workflow(network)["status"] == "refunded"
    assert network.store.payments[request.reference]["status"] == "refunded"
    assert "refunded automatically" in last_card(network, "detail")[2]


async def test_a_lost_specialist_task_is_retried_once(network) -> None:
    network.set_offline("analysis")
    await network.send_user(chat(example_bundle_selection()))
    assert workflow(network)["stage"] == "analysis"
    network.set_offline("analysis", False)
    await network.advance(31)
    assert last_card(network)[0] == "carousel"
    assert workflow(network)["retried_stages"] == ["analysis"]
    assert "task_retried" in event_types(network)


async def test_a_specialist_that_stays_down_fails_cleanly(network) -> None:
    network.set_offline("analysis")
    await network.send_user(chat(example_bundle_selection()))
    await network.advance(31)
    await network.advance(31)
    state = workflow(network)
    assert state["status"] == "failed"
    assert "did not answer in time, even after a retry" in last_card(network)[2]
    assert network.store.payments == {}


async def test_checkout_coordinator_outage_during_booking_keeps_the_payment(network) -> None:
    await hold_first_bundle(network)
    request = await request_payment(network, "stripe_mock")
    network.set_offline("fulfillment")
    await network.send_user(mock_commit(request))
    await network.advance(31)
    assert workflow(network)["status"] == "booking_failed"
    assert network.store.payments[request.reference]["status"] == "released"
    assert network.store.escrow == {}
    assert last_card(network)[1]["root"]["title"] == "Booking did not complete"


async def test_late_success_after_the_workflow_moved_on_is_released(network) -> None:
    await network.send_user(chat(example_bundle_selection()))
    state = workflow(network)
    orphan = {
        "hold_id": "orphan-hold",
        "seller_id": "gear_share",
        "item_id": "tripod",
        "workflow_id": state["workflow_id"],
        "quantity": 1,
        "window_start": later(3600),
        "window_end": later(7200),
        "expires_at": later(600),
        "status": "held",
        "subtotal_cents": 50,
    }
    assert await network.store.place_hold(orphan, 5, later(0)) == "held"
    stale = TaskResult(
        workflow_id=state["workflow_id"],
        request_id="stale-request",
        requester=state["requester"],
        session_id=SESSION,
        source_role="fulfillment",
        ok=True,
        payload={
            "phase": "hold",
            "holds": [{"hold_id": "orphan-hold", "address": network.address("gear_share")}],
        },
    )
    network.queue.append(
        (network.address("fulfillment"), network.address("orchestrator"), stale, SESSION)
    )
    await network.run()
    assert network.store.holds["orphan-hold"]["status"] == "released"
    assert workflow(network)["status"] == "awaiting_selection"
