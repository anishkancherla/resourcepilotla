"""Pure helpers for the Checkout Coordinator's all-or-nothing hold and commit rounds.

A round asks every seller in a bundle to do one thing (hold, or commit a hold). It succeeds only
if every seller confirms. One rejection ends the round early; silence ends it at the deadline.
Either way every hold in the round is released, so a partial bundle is never left behind.
"""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from resourcepilot.registry import LISTING_BY_ID

SUCCESS_STATUS = {"hold": "held", "commit": "booked"}


def hold_id_for(request_id: str, index: int) -> str:
    return "hold_" + uuid5(NAMESPACE_URL, f"{request_id}:{index}").hex[:24]


def hold_plan(request_id: str, bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """One hold per bundle line, addressed only to the seller's registered agent address."""
    plan: dict[str, dict[str, Any]] = {}
    for index, line in enumerate(bundle["lines"]):
        listing = LISTING_BY_ID.get(str(line["seller_id"]))
        if listing is None or not listing.address:
            raise ValueError(f"No registered agent address for {line['seller_name']}")
        hold_id = hold_id_for(request_id, index)
        plan[hold_id] = {
            "hold_id": hold_id,
            "seller_id": listing.seller_id,
            "seller_name": listing.display_name,
            "address": listing.address,
            "item_id": str(line["item_id"]),
            "quantity": int(line["quantity"]),
            "subtotal_cents": int(line["subtotal_cents"]),
        }
    return plan


def round_complete(state: dict[str, Any]) -> bool:
    replies = state["replies"].values()
    return any(reply["status"] == "rejected" for reply in replies) or len(state["replies"]) >= len(
        state["holds"]
    )


def round_outcome(state: dict[str, Any], *, timed_out: bool) -> dict[str, list[dict[str, Any]]]:
    """Split a closed round into confirmed, failed, and cancelled holds.

    Sellers that never answered count as failed only when the deadline passed. If another seller
    already declined, their holds were cancelled before they had a fair chance to answer.
    """
    success = SUCCESS_STATUS[state["phase"]]
    outcome: dict[str, list[dict[str, Any]]] = {"confirmed": [], "failed": [], "cancelled": []}
    for hold_id, hold in state["holds"].items():
        reply = state["replies"].get(hold_id)
        if reply and reply["status"] == success:
            details = {key: value for key, value in reply.items() if key != "status"}
            outcome["confirmed"].append({**hold, **details})
        elif reply:
            outcome["failed"].append({**hold, "reason": str(reply.get("reason", "Declined"))})
        elif timed_out:
            outcome["failed"].append({**hold, "reason": "Did not answer in time"})
        else:
            outcome["cancelled"].append(
                {**hold, "reason": "Cancelled after another seller declined"}
            )
    return outcome


def failure_message(phase: str, failed: list[dict[str, Any]]) -> str:
    names = ", ".join(dict.fromkeys(item["seller_name"] for item in failed))
    reasons = "; ".join(dict.fromkeys(item["reason"] for item in failed))
    action = "hold your items" if phase == "hold" else "confirm the booking"
    return f"{names} could not {action} ({reasons})"
