#!/usr/bin/env python3
"""Mock Stripe demo buyer for the full hold-then-pay checkout.

It chats with the Orchestrator like a person clicking cards: it submits the example demo-bundle
form, picks the first bundle, holds everything, chooses Mock Stripe, and answers RequestPayment
with a stripe_mock CommitPayment. It never calls Stripe and never moves any money.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    MetadataContent,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    RejectPayment,
    RequestPayment,
    payment_protocol_spec,
)

from resourcepilot.cards import example_bundle_selection

load_dotenv()
ORCHESTRATOR = os.environ["ORCHESTRATOR_ADDRESS"]
buyer = Agent(
    name="ResourcePilot Mock Stripe Demo Buyer",
    seed=os.getenv("DEMO_BUYER_SEED") or secrets.token_urlsafe(48),
    port=int(os.getenv("DEMO_BUYER_PORT", "8099")),
)
chat = Protocol(spec=chat_protocol_spec)
payment = Protocol(spec=payment_protocol_spec, role="buyer")


def message(body: str | dict[str, Any]) -> ChatMessage:
    text = body if isinstance(body, str) else json.dumps(body)
    return ChatMessage(
        timestamp=datetime.now(UTC), msg_id=uuid4(), content=[TextContent(type="text", text=text)]
    )


def next_selection(kind: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Click what a person would click next on each card."""
    if kind == "carousel" and payload.get("items"):
        return dict(payload["items"][0]["primary_cta"]["selection"])
    if kind == "detail" and payload.get("ctas"):
        first = dict(payload["ctas"][0]["selection"])
        return first if first.get("action") == "place_holds" else None
    if kind == "review":
        return dict(payload["approve_cta"]["selection"])
    if kind == "custom" and payload.get("root", {}).get("title") == "Pay ResourcePilot escrow":
        for child in payload["root"]["children"]:
            selection = child.get("action", {}).get("selection", {})
            if selection.get("payment_method") == "stripe_mock":
                return dict(selection)
    return None


@buyer.on_event("startup")
async def start(ctx: Context) -> None:
    ctx.logger.warning("STRIPE_MOCK demo buyer: NO Stripe API call and NO money moves")
    await ctx.send(ORCHESTRATOR, message(example_bundle_selection()))


@chat.on_message(model=ChatMessage)
async def on_chat(ctx: Context, sender: str, incoming: ChatMessage) -> None:
    await ctx.send(
        sender,
        ChatAcknowledgement(timestamp=datetime.now(UTC), acknowledged_msg_id=incoming.msg_id),
    )
    for content in incoming.content:
        if isinstance(content, TextContent):
            ctx.logger.info("Orchestrator: %s", content.text)
        if not isinstance(content, MetadataContent):
            continue
        metadata = content.metadata or {}
        try:
            payload = json.loads(metadata.get("card_payload", "{}"))
        except json.JSONDecodeError:
            continue
        kind = str(metadata.get("card_kind", ""))
        if kind == "detail" and str(payload.get("title", "")).startswith("Booked with"):
            for row in payload["summary_rows"]:
                ctx.logger.warning("Receipt  %s: %s", row["label"], row["value"])
            continue
        selection = next_selection(kind, payload)
        if selection:
            await ctx.send(sender, message(selection))


@chat.on_message(model=ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, incoming: ChatAcknowledgement) -> None:
    return None


@payment.on_message(model=RequestPayment)
async def on_request(ctx: Context, sender: str, request: RequestPayment) -> None:
    option = next(
        (funds for funds in request.accepted_funds if funds.payment_method == "stripe_mock"), None
    )
    if option is None:
        await ctx.send(sender, RejectPayment(reason="This demo buyer only supports stripe_mock"))
        return
    transaction_id = f"STRIPE_MOCK:{uuid4()}"
    await ctx.send(
        sender,
        CommitPayment(
            funds=option,
            recipient=request.recipient,
            transaction_id=transaction_id,
            reference=request.reference,
            description="Mock Stripe checkout commitment (no money moved)",
            metadata={
                "stripe_mock_buyer": str(buyer.address),
                "stripe_mock_payment_intent": transaction_id,
                "moves_funds": "false",
            },
        ),
    )


@payment.on_message(model=CompletePayment)
async def on_complete(ctx: Context, sender: str, completed: CompletePayment) -> None:
    ctx.logger.warning("Mock Stripe payment accepted into escrow; no money moved")


@payment.on_message(model=CancelPayment)
async def on_cancel(ctx: Context, sender: str, cancelled: CancelPayment) -> None:
    ctx.logger.error("Mock Stripe payment cancelled: %s", cancelled.reason)


buyer.include(chat)
buyer.include(payment)

if __name__ == "__main__":
    buyer.run()
