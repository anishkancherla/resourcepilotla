from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from resourcepilot.models import PaymentRecord
from resourcepilot.store import DurableStore

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def iso(hours: float = 0, minutes: float = 0) -> str:
    return (NOW + timedelta(hours=hours, minutes=minutes)).isoformat()


def hold(hold_id: str, *, seller: str, item: str, quantity: int = 1, **overrides: str) -> dict:
    return {
        "hold_id": hold_id,
        "seller_id": seller,
        "item_id": item,
        "workflow_id": "workflow-1",
        "quantity": quantity,
        "window_start": overrides.get("start", iso(3)),
        "window_end": overrides.get("end", iso(6)),
        "expires_at": overrides.get("expires", iso(minutes=10)),
        "subtotal_cents": 900,
    }


async def test_workflow_patch_find_and_overdue(any_store: DurableStore, unique: str) -> None:
    workflow_id = f"wf-{unique}"
    await any_store.create_workflow(
        workflow_id,
        {
            "workflow_id": workflow_id,
            "requester": f"agent1q{unique}",
            "session_id": "s1",
            "status": "running",
            "deadline_at": iso(minutes=-1),
        },
    )
    state = await any_store.patch_workflow(workflow_id, {"stage": "discovery"})
    assert state["stage"] == "discovery"
    assert (await any_store.find_workflow(f"agent1q{unique}", "s1"))["stage"] == "discovery"
    overdue = await any_store.list_overdue_workflows(iso())
    assert workflow_id in {item["workflow_id"] for item in overdue}
    await any_store.patch_workflow(workflow_id, {"status": "booking_failed"})
    assert workflow_id not in {
        w["workflow_id"] for w in await any_store.list_overdue_workflows(iso())
    }
    refunds_due = await any_store.list_overdue_workflows(iso(), status="booking_failed")
    assert workflow_id in {item["workflow_id"] for item in refunds_due}
    await any_store.patch_workflow(workflow_id, {"status": "booked"})
    overdue = await any_store.list_overdue_workflows(iso())
    assert workflow_id not in {item["workflow_id"] for item in overdue}
    booked = await any_store.recent_bookings(f"agent1q{unique}", 5)
    assert [item["workflow_id"] for item in booked] == [workflow_id]


async def test_claims_are_one_time(any_store: DurableStore, unique: str) -> None:
    assert await any_store.claim("chat", unique)
    assert not await any_store.claim("chat", unique)


async def test_events_preferences_and_seller_records(any_store: DurableStore, unique: str) -> None:
    assert await any_store.append_event(f"wf-{unique}", f"e-{unique}", "started", {"a": 1})
    assert not await any_store.append_event(f"wf-{unique}", f"e-{unique}", "started", {"a": 1})
    assert [event["event_type"] for event in await any_store.list_events(f"wf-{unique}")] == [
        "started"
    ]
    await any_store.save_preference(unique, {"anchor": "library"})
    await any_store.save_preference(unique, {"priority": "closest"})
    assert await any_store.get_preference(unique) == {"anchor": "library", "priority": "closest"}

    seller = f"seller-{unique}"
    await any_store.record_seller_outcome(seller, "holds_confirmed")
    await any_store.record_seller_outcome(seller, "holds_failed")
    await any_store.record_seller_outcome(seller, "holds_failed")
    records = await any_store.seller_records([seller, f"new-{unique}"])
    assert records[seller]["holds_failed"] == 2
    assert records[seller]["holds_confirmed"] == 1
    assert records[f"new-{unique}"]["holds_confirmed"] == 0
    with pytest.raises(ValueError, match="unknown seller counter"):
        await any_store.record_seller_outcome(seller, "secret_bonus")


async def _payment(any_store: DurableStore, reference: str) -> None:
    await any_store.create_payment(
        PaymentRecord(
            reference=reference,
            workflow_id="wf",
            requester="agent1qbuyer",
            amount="34.29",
            currency="USD",
            recipient="acct_mock_resourcepilot",
            network="stripe-mock-no-network",
            status="requested",
        )
    )


async def test_payment_lifecycle_and_replay(any_store: DurableStore, unique: str) -> None:
    reference, other = f"ref-{unique}", f"ref2-{unique}"
    await _payment(any_store, reference)
    await _payment(any_store, other)
    tx = f"STRIPE_MOCK:{unique}"
    assert await any_store.verify_payment(reference, tx, "agent1qbuyer") == "verified"
    assert await any_store.verify_payment(reference, tx, "agent1qbuyer") == "replay"
    assert await any_store.verify_payment(other, tx, "agent1qbuyer") == "transaction_replay"
    assert await any_store.reserve_payment(reference)
    assert not await any_store.reserve_payment(reference)
    assert await any_store.release_payment(reference, "seller ghosted")
    assert await any_store.reserve_payment(reference)
    assert await any_store.deliver_payment(reference)
    assert not await any_store.refund_payment(reference, "too late")
    assert (await any_store.get_payment(reference))["status"] == "delivered"


async def test_released_entitlement_can_be_refunded_once(
    any_store: DurableStore, unique: str
) -> None:
    reference = f"ref-{unique}"
    await _payment(any_store, reference)
    await any_store.verify_payment(reference, f"tx-{unique}", "agent1qbuyer")
    await any_store.reserve_payment(reference)
    await any_store.release_payment(reference, "booking failed")
    assert await any_store.refund_payment(reference, "buyer chose refund")
    assert not await any_store.refund_payment(reference, "duplicate")
    assert not await any_store.reserve_payment(reference)


async def test_escrow_entries_are_idempotent(any_store: DurableStore, unique: str) -> None:
    entry = {
        "entry_id": f"payout-{unique}",
        "reference": f"ref-{unique}",
        "kind": "payout",
        "party": "gear_share",
        "amount": "9.00",
        "currency": "USD",
        "status": "scheduled",
        "note": "",
    }
    assert await any_store.add_escrow_entry(entry)
    assert not await any_store.add_escrow_entry(entry)
    await any_store.update_escrow_entry(entry["entry_id"], "sent", "STRIPE_MOCK_PAYOUT:1")
    [stored] = await any_store.list_escrow_entries(f"ref-{unique}")
    assert stored["status"] == "sent"
    assert stored["transaction_id"] == "STRIPE_MOCK_PAYOUT:1"


async def test_rounds_accept_each_reply_once_and_close_once(
    any_store: DurableStore, unique: str
) -> None:
    round_id = f"round-{unique}"
    await any_store.create_round(round_id, "discovery", iso(minutes=-1), {"expected": ["a", "b"]})
    state = await any_store.record_reply(round_id, "a", {"ok": True})
    assert state and state["replies"] == {"a": {"ok": True}}
    assert await any_store.record_reply(round_id, "a", {"ok": False}) is None
    expired = await any_store.list_expired_rounds("discovery", iso())
    assert round_id in {item["round_id"] for item in expired}
    assert await any_store.close_round(round_id)
    assert not await any_store.close_round(round_id)
    assert await any_store.record_reply(round_id, "b", {"ok": True}) is None
    assert round_id not in {
        item["round_id"] for item in await any_store.list_expired_rounds("discovery", iso())
    }


async def test_holds_respect_capacity_overlap_and_expiry(
    any_store: DurableStore, unique: str
) -> None:
    seller, item = f"gear-{unique}", "dslr"
    assert (
        await any_store.place_hold(hold(f"h1-{unique}", seller=seller, item=item), 1, iso())
        == "held"
    )
    assert (
        await any_store.place_hold(hold(f"h1-{unique}", seller=seller, item=item), 1, iso())
        == "duplicate"
    )
    assert (
        await any_store.place_hold(hold(f"h2-{unique}", seller=seller, item=item), 1, iso())
        == "unavailable"
    )
    later = hold(f"h3-{unique}", seller=seller, item=item, start=iso(6), end=iso(8))
    assert await any_store.place_hold(later, 1, iso()) == "held"
    # After the first hold's TTL, its capacity frees up.
    assert (
        await any_store.place_hold(
            hold(f"h4-{unique}", seller=seller, item=item, expires=iso(minutes=20)),
            1,
            iso(minutes=11),
        )
        == "held"
    )
    assert await any_store.used_quantity(seller, item, iso(3), iso(6), iso(minutes=11)) == 1


async def test_commit_and_release_holds(any_store: DurableStore, unique: str) -> None:
    seller = f"rooms-{unique}"
    await any_store.place_hold(hold(f"c1-{unique}", seller=seller, item="podcast"), 1, iso())
    assert await any_store.commit_hold(f"c1-{unique}", "ref", "RP-1", iso(minutes=1)) == "committed"
    assert await any_store.commit_hold(f"c1-{unique}", "ref", "RP-1", iso(minutes=1)) == "duplicate"
    await any_store.place_hold(
        hold(f"c2-{unique}", seller=seller, item="study", expires=iso(minutes=1)), 1, iso()
    )
    assert await any_store.commit_hold(f"c2-{unique}", "ref", "RP-2", iso(minutes=2)) == "expired"
    assert await any_store.commit_hold(f"missing-{unique}", "ref", "RP-3", iso()) == "missing"
    assert await any_store.release_hold(f"c1-{unique}", "buyer refund")
    assert not await any_store.release_hold(f"c1-{unique}", "again")
    assert (await any_store.get_hold(f"c1-{unique}"))["status"] == "released"


async def test_fault_mode_round_trip(any_store: DurableStore, unique: str) -> None:
    assert await any_store.get_fault_mode(unique) is None
    await any_store.set_fault_mode(unique, "ghost_holds")
    await any_store.set_fault_mode(unique, "none")
    assert await any_store.get_fault_mode(unique) == "none"


async def test_api_cache_round_trips_and_expires(any_store: DurableStore, unique: str) -> None:
    key = f"test:{unique}"
    assert await any_store.cache_get(key) is None
    value = {"purdue.edu": {"unitid": 243780, "name": "Purdue"}, "rows": [1, 2]}
    await any_store.cache_put(key, value, 60)
    assert await any_store.cache_get(key) == value
    await any_store.cache_put(key, {"replaced": True}, 60)
    assert await any_store.cache_get(key) == {"replaced": True}
    await any_store.cache_put(key, value, 0)
    assert await any_store.cache_get(key) is None
