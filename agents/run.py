"""Runtime for one ResourcePilot platform agent, selected by AGENT_ROLE.

Every role is its own process, seed, address, and profile. The Orchestrator delegates with
``ctx.send``; specialists reply with correlated TaskResults. Discovery fans out to independent
seller agents over the ResourceHold protocol. Checkout is hold-then-pay: the Checkout
Coordinator holds every line with every seller, the buyer pays the Orchestrator's escrow over
the Agent Payment Protocol, and only then are the holds committed and the sellers paid out.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    Funds,
    RejectPayment,
    RequestPayment,
    payment_protocol_spec,
)

from resourcepilot import cards
from resourcepilot.checkout import failure_message, hold_plan, round_complete, round_outcome
from resourcepilot.clock import later, parse, utc_now
from resourcepilot.config import (
    ROLE_BY_KEY,
    SPECIALIST_CHAIN,
    agent_addresses,
    env_bool,
    env_int,
    get_role,
)
from resourcepilot.escrow import CosmpyPayoutGateway, refund_buyer, settle_booking
from resourcepilot.intake import SAVED_PREFERENCES, parse_request_text, upcoming_days
from resourcepilot.models import (
    AuditEvent,
    HealthResponse,
    PaymentRecord,
    TaskRequest,
    TaskResult,
)
from resourcepilot.payment import (
    STRIPE_MOCK,
    CosmpyLedgerGateway,
    PaymentConfig,
    fet_for_usd_cents,
    verify_commit,
)
from resourcepilot.pricing import usd
from resourcepilot.protocols import (
    BookingConfirmed,
    CommitHold,
    HoldConfirmed,
    HoldRejected,
    HoldRequest,
    Quote,
    QuoteRequest,
    ReleaseHold,
    resource_hold_spec,
    seller_protocol_digest,
)
from resourcepilot.registry import LISTING_BY_ID, listings_for
from resourcepilot.store import store
from resourcepilot.workflow import (
    accept_result,
    begin_workflow,
    execute_specialist,
    is_timed_out,
    make_task,
    task_result,
)

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]
ROLE = get_role()
ADDRESSES = agent_addresses()
PORT = int(os.getenv("AGENT_PORT", str(ROLE.configured_port())))
QUOTE_WINDOW_SECONDS = env_int("QUOTE_WINDOW_SECONDS", 12, minimum=3)
HOLD_WINDOW_SECONDS = env_int("HOLD_WINDOW_SECONDS", 15, minimum=3)
PAYMENT_DEADLINE_SECONDS = env_int("PAYMENT_DEADLINE_SECONDS", 600, minimum=60)

agent = Agent(
    name=ROLE.name,
    seed=ROLE.seed(),
    port=PORT,
    mailbox=env_bool("ENABLE_MAILBOX", True),
    agentverse=os.getenv("AGENTVERSE_URL", "https://agentverse.ai"),
    publish_agent_details=True,
    readme_path=str(ROOT / ROLE.readme_dir / "README.md"),
)

chat_proto = Protocol(spec=chat_protocol_spec)
task_proto = Protocol(name="ResourcePilotTasks", version="1.0.0")
hold_proto = Protocol(spec=resource_hold_spec, role="buyer")
payment_proto = Protocol(spec=payment_protocol_spec, role="seller")
# The Orchestrator's own wallet is the escrow account buyers pay into.
ESCROW_WALLET = str(agent.wallet.address())
ledger = CosmpyLedgerGateway()
payouts = CosmpyPayoutGateway(agent.wallet)
READY = False


def _session(ctx: Context) -> str:
    return str(getattr(ctx, "session", "") or "no-session")


def _now() -> str:
    return utc_now().isoformat()


def _chat_text(message: ChatMessage) -> str:
    return " ".join(
        item.text for item in (message.content or []) if isinstance(item, TextContent)
    ).strip()


async def _send_card(
    ctx: Context,
    target: str,
    narration: str,
    card: tuple[str, dict[str, Any]],
    *,
    terminal: bool = False,
) -> None:
    kind, payload = card
    await ctx.send(target, cards.card_message(narration, kind, payload, terminal=terminal))


async def _audit(ctx: Context, state: dict[str, Any], event_type: str, **details: Any) -> None:
    await ctx.send(
        ADDRESSES["memory_audit"],
        AuditEvent(
            workflow_id=str(state["workflow_id"]),
            requester=str(state["requester"]),
            session_id=str(state["session_id"]),
            event_id=str(uuid4()),
            event_type=event_type,
            details=details,
        ),
    )


async def _seller_outcome(ctx: Context, workflow_id: str, seller_id: str, counter: str) -> None:
    await ctx.send(
        ADDRESSES["memory_audit"],
        AuditEvent(
            workflow_id=workflow_id,
            requester=str(agent.address),
            session_id="seller-outcome",
            event_id=str(uuid4()),
            event_type="seller_outcome",
            details={"seller_id": seller_id, "counter": counter},
        ),
    )


async def _dispatch(ctx: Context, task: TaskRequest) -> None:
    await ctx.send(ADDRESSES[task.target_role], task)


@agent.on_event("startup")
async def startup(ctx: Context) -> None:
    global READY
    await store.setup()
    if not store.production_ready:
        ctx.logger.warning(
            "DATABASE_URL is unset: using a NON-PRODUCTION process-local store. "
            "The multi-container demo requires Postgres."
        )
    READY = True
    ctx.logger.info("%s ready at %s on port %s", ROLE.name, agent.address, PORT)


@agent.on_event("shutdown")
async def shutdown(ctx: Context) -> None:
    global READY
    READY = False
    await store.close()


@agent.on_rest_get("/health", HealthResponse)
async def health(ctx: Context) -> HealthResponse:
    return HealthResponse(
        status="ok" if READY else "starting", role=ROLE.key, address=str(agent.address)
    )


# ---------------------------------------------------------------------------
# Chat: the Orchestrator runs the workflow; every other role shows its status card.
# ---------------------------------------------------------------------------


@chat_proto.on_message(model=ChatMessage)
async def handle_chat(ctx: Context, sender: str, message: ChatMessage) -> None:
    await ctx.send(
        sender,
        ChatAcknowledgement(timestamp=datetime.now(UTC), acknowledged_msg_id=message.msg_id),
    )
    if not await store.claim(f"chat:{ROLE.key}", str(message.msg_id)):
        return
    text = _chat_text(message)
    if ROLE.key != "orchestrator":
        await _send_card(
            ctx,
            sender,
            f"I am the {ROLE.name}. {ROLE.responsibility}. Chat with the Orchestrator to book.",
            cards.status_card(ROLE.name, ROLE.responsibility, ADDRESSES["orchestrator"]),
        )
        return
    await _orchestrator_chat(ctx, sender, text, cards.parse_selection(text))


async def _start_workflow(ctx: Context, sender: str, request: dict[str, Any]) -> None:
    request["saved_preferences"] = await store.get_preference(sender)
    task = await begin_workflow(store, requester=sender, session_id=_session(ctx), request=request)
    state = await store.get_workflow(task.workflow_id)
    assert state is not None
    await _audit(ctx, state, "workflow_started", stage="intake")
    await _dispatch(ctx, task)
    await _send_card(
        ctx,
        sender,
        "Got it. Intake, Discovery, Verification, Analysis, and Planner are on it.",
        cards.progress_card(task.workflow_id, "intake"),
    )


async def _orchestrator_chat(
    ctx: Context, sender: str, text: str, selection: dict[str, Any]
) -> None:
    action = str(selection.get("action", "")).strip()
    if not action and text and parse_request_text(text).get("items"):
        # A plain sentence that names resources is a request, not small talk.
        action, selection = "submit_text", {"text": text}
    if not action or action == "start_over":
        await _send_card(ctx, sender, "What do you need to book?", cards.welcome_card())
        return
    if action == "open_form":
        await _send_card(ctx, sender, "Fill in what you need.", cards.request_form(upcoming_days()))
        return
    if action == "submit_text":
        await _start_workflow(ctx, sender, {"text": str(selection.get("text") or text)[:500]})
        return
    if action == "submit_form":
        fields = {key: value for key, value in selection.items() if key != "action"}
        await _start_workflow(ctx, sender, {"fields": fields})
        return
    if action == "show_bookings":
        await _send_card(
            ctx,
            sender,
            "Here are your recent bookings.",
            cards.bookings_card(await store.recent_bookings(sender, 5)),
        )
        return

    workflow_id = str(selection.get("workflow_id", "")).strip()
    state = (
        await store.get_workflow(workflow_id)
        if workflow_id
        else await store.find_workflow(sender, _session(ctx))
    )
    if state is None or state.get("requester") != sender:
        await ctx.send(sender, cards.text_message("I could not find that request. Start over?"))
        return
    await _workflow_action(ctx, sender, action, selection, state)


def _bundle(state: dict[str, Any], bundle_id: str) -> dict[str, Any] | None:
    return next(
        (
            dict(bundle)
            for bundle in state.get("data", {}).get("bundles", [])
            if bundle.get("bundle_id") == bundle_id
        ),
        None,
    )


async def _show_bundles(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    data = state["data"]
    rejected = len(data.get("rejections", []))
    names = sorted(
        {r["seller_name"] for r in data.get("rejections", []) if r["stage"] == "identity"}
    )
    narration = f"I vetted the quotes and built {len(data['bundles'])} bundles within budget."
    if names:
        narration += f" Excluded for identity: {', '.join(names)}."
    if data.get("uncovered"):
        narration += f" No trusted seller had: {', '.join(data['uncovered'])}."
    if data.get("exclude_sellers"):
        skipped = [
            LISTING_BY_ID[seller].display_name if seller in LISTING_BY_ID else seller
            for seller in data["exclude_sellers"]
        ]
        narration += f" Rerouted around: {', '.join(skipped)}."
    await _send_card(
        ctx,
        sender,
        narration,
        cards.bundle_carousel(str(state["workflow_id"]), list(data["bundles"]), rejected),
    )


def _holds_expired(state: dict[str, Any]) -> bool:
    expires_at = state.get("holds_expire_at")
    return bool(expires_at) and utc_now() >= parse(str(expires_at))


async def _show_review(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    await _send_card(
        ctx,
        sender,
        "Every seller is holding your items. Review the total, then choose how to pay.",
        cards.holds_review_card(
            str(state["workflow_id"]),
            state["data"]["brief"],
            state["bundle"],
            str(state["holds_expire_at"]),
        ),
    )


async def _show_payment_options(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    total = int(state["bundle"]["total_cents"])
    try:
        fet_amount: str | None = str(fet_for_usd_cents(total))
    except ValueError:
        fet_amount = None
    await _send_card(
        ctx,
        sender,
        f"Pay {usd(total)} into ResourcePilot escrow. "
        "Sellers are paid after every booking commits.",
        cards.payment_options_card(str(state["workflow_id"]), total, fet_amount),
    )


async def _send_receipt(ctx: Context, sender: str, state: dict[str, Any], narration: str) -> None:
    payment = await store.get_payment(str(state["payment_reference"]))
    receipt = state["receipt"]
    await _send_card(
        ctx,
        sender,
        narration,
        cards.receipt_card(
            state["data"]["brief"],
            state["bundle"],
            list(receipt["bookings"]),
            payment or {"currency": ""},
            list(receipt["ledger"]),
        ),
        terminal=True,
    )


async def _workflow_action(
    ctx: Context, sender: str, action: str, selection: dict[str, Any], state: dict[str, Any]
) -> None:
    workflow_id = str(state["workflow_id"])
    if action == "show_status":
        if is_timed_out(state):
            await _send_card(
                ctx,
                sender,
                "A specialist did not answer in time. Your request is saved.",
                cards.error_card("A specialist timed out. Start over to try again."),
            )
        elif state["status"] == "awaiting_selection":
            await _show_bundles(ctx, sender, state)
        elif state["status"] == "awaiting_payment":
            await _show_review(ctx, sender, state)
        elif state["status"] == "payment_requested":
            await _show_payment_options(ctx, sender, state)
        elif state["status"] == "booked":
            await _send_receipt(ctx, sender, state, "Here is your booking receipt.")
        elif state["status"] == "booking_failed":
            await _show_recovery(ctx, sender, state)
        elif state["status"] == "refunded":
            await _send_refund(ctx, sender, state, "This request was refunded.")
        else:
            await _send_card(
                ctx,
                sender,
                f"Current status: {state['status'].replace('_', ' ')}.",
                cards.progress_card(workflow_id, str(state["stage"])),
            )
        return
    if action == "show_vetting":
        await _send_card(
            ctx,
            sender,
            "These offers were filtered out before you saw any bundle.",
            cards.vetting_card(workflow_id, list(state.get("data", {}).get("rejections", []))),
        )
        return
    bundle = _bundle(state, str(selection.get("bundle_id", "")))
    if action == "view_bundle" and bundle:
        await _send_card(
            ctx,
            sender,
            f"{bundle['label']}: {usd(bundle['total_cents'])} from "
            f"{bundle['seller_count']} vetted sellers.",
            cards.bundle_detail(
                workflow_id,
                state["data"]["brief"],
                bundle,
                checkout=state["status"] == "awaiting_selection",
            ),
        )
        return
    if action == "place_holds" and bundle:
        await _place_holds(ctx, sender, state, bundle)
        return
    if action == "choose_payment" and state["status"] in {"awaiting_payment", "payment_requested"}:
        if _holds_expired(state):
            await _release_holds(ctx, sender, state, "The holds expired before payment")
            return
        await _show_payment_options(ctx, sender, state)
        return
    if action == "pay":
        await _request_payment(ctx, sender, state, str(selection.get("payment_method", "")))
        return
    if action == "release_holds":
        await _release_holds(ctx, sender, state, "Buyer released the holds")
        return
    if action == "retry_booking":
        await _retry_booking(ctx, sender, state)
        return
    if action == "refund":
        await _refund(ctx, sender, state, automatic=False)
        return
    await ctx.send(sender, cards.text_message("That action is not valid for this request."))


# ---------------------------------------------------------------------------
# Checkout: hold everything, take payment into escrow, then commit everything.
# ---------------------------------------------------------------------------


async def _start_hold_round(
    ctx: Context,
    sender: str,
    state: dict[str, Any],
    bundle: dict[str, Any],
    *,
    from_status: str,
    retrying: bool,
) -> dict[str, Any] | None:
    """Claim one hold attempt and dispatch it; a double click never places two sets of holds."""
    workflow_id = str(state["workflow_id"])
    attempt = int(state.get("hold_attempt", 0))
    if state["status"] != from_status or not await store.claim(
        "place_holds", f"{workflow_id}:{attempt}"
    ):
        await ctx.send(
            sender, cards.text_message("Holds are already in progress for this request.")
        )
        return None
    task = make_task(
        workflow_id=workflow_id,
        requester=sender,
        session_id=str(state["session_id"]),
        target_role="fulfillment",
        payload={"phase": "hold", "brief": state["data"]["brief"], "bundle": bundle},
    )
    state = await store.patch_workflow(
        workflow_id,
        {
            "status": "running",
            "stage": "holds",
            "bundle": bundle,
            "retrying": retrying,
            "hold_attempt": attempt + 1,
            "expected_request_id": task.request_id,
            "deadline_at": task.deadline_at,
        },
    )
    await _audit(ctx, state, "holds_requested", bundle_id=bundle["bundle_id"], retrying=retrying)
    await _dispatch(ctx, task)
    return state


async def _place_holds(
    ctx: Context, sender: str, state: dict[str, Any], bundle: dict[str, Any]
) -> None:
    if await _start_hold_round(
        ctx, sender, state, bundle, from_status="awaiting_selection", retrying=False
    ):
        await _send_card(
            ctx,
            sender,
            f"Asking {bundle['seller_count']} sellers to hold everything at once. "
            "No money moves yet.",
            cards.progress_card(str(state["workflow_id"]), "holds"),
        )


async def _retry_booking(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    payment = await store.get_payment(str(state.get("payment_reference") or ""))
    if state["status"] != "booking_failed" or payment is None or payment["status"] != "released":
        await ctx.send(sender, cards.text_message("A retry is not available for this request."))
        return
    if await _start_hold_round(
        ctx, sender, state, state["bundle"], from_status="booking_failed", retrying=True
    ):
        await _send_card(
            ctx,
            sender,
            "Asking every seller to hold again. Your payment stays in escrow; no second payment.",
            cards.progress_card(str(state["workflow_id"]), "holds"),
        )


async def _dispatch_commit(ctx: Context, state: dict[str, Any], reference: str) -> dict[str, Any]:
    task = make_task(
        workflow_id=str(state["workflow_id"]),
        requester=str(state["requester"]),
        session_id=str(state["session_id"]),
        target_role="fulfillment",
        payload={"phase": "commit", "holds": list(state["holds"]), "payment_reference": reference},
    )
    state = await store.patch_workflow(
        str(state["workflow_id"]),
        {
            "status": "running",
            "stage": "booking",
            "retrying": False,
            "expected_request_id": task.request_id,
            "deadline_at": task.deadline_at,
        },
    )
    try:
        await _dispatch(ctx, task)
    except Exception:
        await store.release_payment(reference, "dispatch to Checkout Coordinator failed")
        raise
    return state


async def _release_hold_list(
    ctx: Context, state: dict[str, Any], holds: Sequence[Any], reason: str
) -> None:
    if not holds:
        return
    task = make_task(
        workflow_id=str(state["workflow_id"]),
        requester=str(state["requester"]),
        session_id=str(state["session_id"]),
        target_role="fulfillment",
        payload={"phase": "release", "holds": list(holds), "reason": reason},
    )
    await _dispatch(ctx, task)


async def _release_holds(ctx: Context, sender: str, state: dict[str, Any], reason: str) -> None:
    if state["status"] not in {"awaiting_payment", "payment_requested"}:
        await ctx.send(sender, cards.text_message("There are no active holds to release."))
        return
    reference = state.get("payment_reference")
    if reference and not await store.cancel_payment(str(reference), reason):
        payment = await store.get_payment(str(reference))
        if payment and payment["status"] != "cancelled":
            await ctx.send(
                sender,
                cards.text_message(
                    "Your payment was already received, so the holds stay in place."
                ),
            )
            return
    await _release_hold_list(ctx, state, list(state.get("holds", [])), reason)
    state = await store.patch_workflow(
        str(state["workflow_id"]),
        {
            "status": "awaiting_selection",
            "stage": "selection",
            "holds": [],
            "holds_expire_at": None,
            "payment_reference": None,
        },
    )
    await _audit(ctx, state, "holds_released", reason=reason)
    await ctx.send(
        sender, cards.text_message(f"{reason}. Every hold was released; nothing was charged.")
    )
    await _show_bundles(ctx, sender, state)


async def _request_payment(ctx: Context, sender: str, state: dict[str, Any], method: str) -> None:
    if state["status"] not in {"awaiting_payment", "payment_requested"}:
        await ctx.send(sender, cards.text_message("Payment is not open for this request."))
        return
    if _holds_expired(state):
        await _release_holds(ctx, sender, state, "The holds expired before payment")
        return
    bundle = state["bundle"]
    try:
        config = PaymentConfig.for_total(method, int(bundle["total_cents"]), ESCROW_WALLET)
    except ValueError as exc:
        await ctx.send(sender, cards.text_message(f"Payment configuration error: {exc}"))
        return
    previous = state.get("payment_reference")
    if previous:
        await store.cancel_payment(str(previous), "Buyer switched payment method")
    reference = str(uuid4())
    await store.create_payment(
        PaymentRecord(
            reference=reference,
            workflow_id=str(state["workflow_id"]),
            requester=sender,
            amount=str(config.amount),
            currency=config.currency,
            recipient=config.recipient,
            network=config.network,
            status="requested",
        )
    )
    state = await store.patch_workflow(
        str(state["workflow_id"]),
        {"status": "payment_requested", "stage": "payment", "payment_reference": reference},
    )
    hold_seconds = int((parse(str(state["holds_expire_at"])) - utc_now()).total_seconds())
    mock = config.mode == STRIPE_MOCK
    await ctx.send(
        sender,
        RequestPayment(
            accepted_funds=[
                Funds(
                    amount=str(config.amount), currency=config.currency, payment_method=config.mode
                )
            ],
            recipient=config.recipient,
            deadline_seconds=max(60, min(PAYMENT_DEADLINE_SECONDS, hold_seconds)),
            reference=reference,
            description=(
                f"ResourcePilot escrow for {len(bundle['lines'])} bookings with "
                f"{bundle['seller_count']} sellers"
            ),
            metadata={
                "fet_network": config.network,
                "stripe_mock": "true" if mock else "false",
                "moves_funds": "false" if mock else "true",
                "escrow": "true",
                "warning": "Mock Stripe only: no Stripe API call and no funds moved"
                if mock
                else "",
            },
        ),
    )
    await _audit(ctx, state, "payment_requested", reference=reference, mode=config.mode)
    await ctx.send(
        sender,
        cards.text_message(
            "Payment requested. Mock Stripe is simulated and moves no money."
            if mock
            else f"Payment requested: {config.amount} test FET on stable-testnet."
        ),
    )


@payment_proto.on_message(model=CommitPayment)
async def handle_commit(ctx: Context, sender: str, commit: CommitPayment) -> None:
    if ROLE.key != "orchestrator":
        return
    reference = str(commit.reference or "")
    payment = await store.get_payment(reference)
    if payment is None:
        await ctx.send(
            sender,
            CancelPayment(transaction_id=commit.transaction_id, reason="unknown payment reference"),
        )
        return
    try:
        config = PaymentConfig.from_record(payment)
    except ValueError as exc:
        await ctx.send(sender, CancelPayment(transaction_id=commit.transaction_id, reason=str(exc)))
        return
    verified, reason = await verify_commit(
        store, commit, sender=sender, config=config, gateway=ledger
    )
    if not verified:
        await ctx.send(sender, CancelPayment(transaction_id=commit.transaction_id, reason=reason))
        return
    await ctx.send(sender, CompletePayment(transaction_id=commit.transaction_id))
    state = await store.get_workflow(str(payment["workflow_id"]))
    if state is None:
        return
    if state["status"] == "booked" and state.get("payment_reference") == reference:
        await _send_receipt(ctx, sender, state, "This payment already booked; same receipt.")
        return
    # Only the first verified commit for the active reference may reserve and book.
    if (
        state.get("payment_reference") != reference
        or state["status"] != "payment_requested"
        or not await store.reserve_payment(reference)
    ):
        await ctx.send(sender, cards.text_message("Payment already received for this request."))
        return
    state = await _dispatch_commit(ctx, state, reference)
    await _audit(ctx, state, "payment_verified", reference=reference, mode=config.mode)
    await _send_card(
        ctx,
        sender,
        "Payment verified and held in escrow. Committing every booking now.",
        cards.progress_card(str(state["workflow_id"]), "booking"),
    )


@payment_proto.on_message(model=RejectPayment)
async def handle_reject(ctx: Context, sender: str, message: RejectPayment) -> None:
    if ROLE.key != "orchestrator":
        return
    state = await store.find_workflow(sender, _session(ctx))
    if state and state["status"] == "payment_requested":
        await _release_holds(ctx, sender, state, "Payment was declined")
        return
    await ctx.send(sender, cards.text_message("Payment declined. Nothing was charged."))


async def _checkout_result(ctx: Context, result: TaskResult) -> None:
    if not await store.claim("task_result", result.request_id):
        return
    state = await store.get_workflow(result.workflow_id)
    if state is None:
        return
    if state.get("expected_request_id") != result.request_id or state.get("stage") not in {
        "holds",
        "booking",
    }:
        # A late success after the Orchestrator moved on must not leave holds or bookings behind.
        if result.ok:
            stale = list(result.payload.get("holds") or result.payload.get("bookings") or [])
            await _release_hold_list(ctx, state, stale, "Arrived after the workflow moved on")
        return
    if state["stage"] == "holds":
        await _holds_result(ctx, state, result)
    else:
        await _booking_result(ctx, state, result)


async def _holds_result(ctx: Context, state: dict[str, Any], result: TaskResult) -> None:
    workflow_id, requester = result.workflow_id, result.requester
    if result.ok:
        holds = {
            "holds": list(result.payload["holds"]),
            "holds_expire_at": str(result.payload["expires_at"]),
        }
        if state.get("retrying"):
            reference = str(state["payment_reference"])
            state = await store.patch_workflow(workflow_id, holds)
            if not await store.reserve_payment(reference):
                await _release_hold_list(ctx, state, holds["holds"], "Payment left escrow")
                await ctx.send(
                    requester,
                    cards.text_message("Your payment is no longer in escrow; holds released."),
                )
                return
            state = await _dispatch_commit(ctx, state, reference)
            await _audit(ctx, state, "booking_retried", reference=reference)
            await _send_card(
                ctx,
                requester,
                "Every seller is holding again. Using your escrowed payment; no second payment.",
                cards.progress_card(workflow_id, "booking"),
            )
            return
        state = await store.patch_workflow(
            workflow_id, {**holds, "status": "awaiting_payment", "stage": "review"}
        )
        await _audit(ctx, state, "holds_placed", holds=len(holds["holds"]))
        await _show_review(ctx, requester, state)
        return

    error = str(result.error or "A seller could not hold your items")
    failed = [str(seller) for seller in result.payload.get("failed_sellers", [])]
    await _audit(ctx, state, "holds_failed", failed=failed, retrying=bool(state.get("retrying")))
    if state.get("retrying"):
        await _booking_failed(ctx, state, error)
        return
    if not failed:
        state = await store.patch_workflow(
            workflow_id, {"status": "awaiting_selection", "stage": "selection", "error": error}
        )
        await ctx.send(requester, cards.text_message(f"{error}. Nothing was charged."))
        await _show_bundles(ctx, requester, state)
        return
    # Reroute: ask the Bundle Planner again without the sellers that failed.
    payload = {
        **state["data"],
        "exclude_sellers": sorted({*state["data"].get("exclude_sellers", []), *failed}),
    }
    task = make_task(
        workflow_id=workflow_id,
        requester=requester,
        session_id=str(state["session_id"]),
        target_role="planner",
        payload=payload,
    )
    state = await store.patch_workflow(
        workflow_id,
        {
            "status": "running",
            "stage": "planner",
            "data": payload,
            "error": error,
            "expected_request_id": task.request_id,
            "deadline_at": task.deadline_at,
        },
    )
    await _audit(ctx, state, "reroute_requested", exclude_sellers=payload["exclude_sellers"])
    await _dispatch(ctx, task)
    await _send_card(
        ctx,
        requester,
        f"{error}. Nothing was charged, and every other hold was released. "
        "The Bundle Planner is rerouting around them.",
        cards.progress_card(workflow_id, "planner"),
    )


async def _booking_result(ctx: Context, state: dict[str, Any], result: TaskResult) -> None:
    if not result.ok:
        await _booking_failed(ctx, state, str(result.error or "A seller could not confirm"))
        return
    reference = str(state["payment_reference"])
    payment = await store.get_payment(reference)
    assert payment is not None
    ledger_entries = await settle_booking(store, payouts, payment, state["bundle"])
    await store.deliver_payment(reference)
    bookings = [
        {key: value for key, value in booking.items() if key != "address"}
        for booking in result.payload["bookings"]
    ]
    state = await store.patch_workflow(
        result.workflow_id,
        {
            "status": "booked",
            "stage": "complete",
            "receipt": {"bookings": bookings, "ledger": ledger_entries},
        },
    )
    await _audit(ctx, state, "booked", reference=reference, sellers=state["bundle"]["seller_count"])
    await _send_receipt(
        ctx,
        result.requester,
        state,
        f"Booked with all {state['bundle']['seller_count']} sellers. "
        "The receipt shows each seller's payout from escrow.",
    )


# ---------------------------------------------------------------------------
# Recovery: retry, refund, and timeouts. A verified payment is never lost.
# ---------------------------------------------------------------------------


async def _show_recovery(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    error = str(state.get("error") or "A seller could not confirm")
    await _send_card(
        ctx,
        sender,
        f"{error}. Every booking was cancelled. Your payment is safe in escrow and no seller "
        "was paid.",
        cards.recovery_card(
            str(state["workflow_id"]),
            error,
            int(state["bundle"]["total_cents"]),
            str(state["deadline_at"]),
        ),
    )


async def _booking_failed(ctx: Context, state: dict[str, Any], error: str) -> None:
    """Keep the verified payment as a retryable escrow entitlement, refundable at any time."""
    await store.release_payment(str(state["payment_reference"]), error)
    state = await store.patch_workflow(
        str(state["workflow_id"]),
        {
            "status": "booking_failed",
            "stage": "recovery",
            "error": error,
            "retrying": False,
            "deadline_at": later(PAYMENT_DEADLINE_SECONDS),
        },
    )
    await _audit(ctx, state, "booking_failed", error=error)
    await _show_recovery(ctx, str(state["requester"]), state)


async def _send_refund(ctx: Context, sender: str, state: dict[str, Any], narration: str) -> None:
    payment = await store.get_payment(str(state["payment_reference"]))
    await _send_card(
        ctx,
        sender,
        narration,
        cards.refund_card(payment or {"currency": ""}, dict(state["refund"])),
        terminal=True,
    )


async def _refund(ctx: Context, sender: str, state: dict[str, Any], *, automatic: bool) -> None:
    reference = str(state.get("payment_reference") or "")
    if state["status"] != "booking_failed" or not await store.refund_payment(
        reference, "automatic refund" if automatic else "buyer requested a refund"
    ):
        if not automatic:
            await ctx.send(
                sender, cards.text_message("A refund is not available for this request.")
            )
        return
    payment = await store.get_payment(reference)
    assert payment is not None
    entry = await refund_buyer(store, payouts, payment)
    state = await store.patch_workflow(
        str(state["workflow_id"]), {"status": "refunded", "stage": "complete", "refund": entry}
    )
    await _audit(ctx, state, "refunded", reference=reference, automatic=automatic)
    await _send_refund(
        ctx,
        sender,
        state,
        "The retry window ended, so your payment was refunded automatically."
        if automatic
        else "Refunded. No seller was paid for this request.",
    )


STAGE_ROLES = {
    **{key: key for key in SPECIALIST_CHAIN},
    "holds": "fulfillment",
    "booking": "fulfillment",
}


async def _recover_overdue(ctx: Context, state: dict[str, Any]) -> None:
    workflow_id, stage = str(state["workflow_id"]), str(state["stage"])
    if not await store.claim("timeout", f"{workflow_id}:{state['expected_request_id']}"):
        return
    requester = str(state["requester"])
    name = ROLE_BY_KEY[STAGE_ROLES.get(stage, "orchestrator")].name
    if stage in SPECIALIST_CHAIN:
        retried = list(state.get("retried_stages", []))
        if stage not in retried:
            # Retry a lost or timed-out specialist task once, with a fresh request ID.
            task = make_task(
                workflow_id=workflow_id,
                requester=requester,
                session_id=str(state["session_id"]),
                target_role=stage,
                payload=dict(state["data"]),
            )
            state = await store.patch_workflow(
                workflow_id,
                {
                    "expected_request_id": task.request_id,
                    "deadline_at": task.deadline_at,
                    "retried_stages": [*retried, stage],
                },
            )
            await _audit(ctx, state, "task_retried", role=stage)
            await _dispatch(ctx, task)
            return
        error = f"The {name} did not answer in time, even after a retry"
        state = await store.patch_workflow(workflow_id, {"status": "failed", "error": error})
        await _audit(ctx, state, "specialist_timeout", role=stage)
        await _send_card(ctx, requester, f"{error}. Nothing was charged.", cards.error_card(error))
        return
    if stage == "booking" or state.get("retrying"):
        # Release what we know is held; a seller that never got the commit stays uncommitted.
        await _release_hold_list(ctx, state, list(state.get("holds", [])), "Booking timed out")
        await _booking_failed(ctx, state, f"The {name} did not confirm in time")
        return
    if stage == "holds":
        state = await store.patch_workflow(
            workflow_id, {"status": "awaiting_selection", "stage": "selection"}
        )
        await _audit(ctx, state, "holds_timeout")
        await ctx.send(
            requester,
            cards.text_message(
                f"The {name} did not answer. Nothing was charged; any holds expire on their own."
            ),
        )
        await _show_bundles(ctx, requester, state)


# ---------------------------------------------------------------------------
# Specialist pipeline
# ---------------------------------------------------------------------------


@task_proto.on_message(model=TaskRequest)
async def handle_task(ctx: Context, sender: str, task: TaskRequest) -> None:
    if sender != ADDRESSES["orchestrator"]:
        ctx.logger.warning("Ignoring task from non-orchestrator %s", sender)
        return
    if ROLE.key == "discovery":
        await _start_discovery(ctx, task)
        return
    if ROLE.key == "fulfillment":
        await _start_checkout_round(ctx, task)
        return
    if ROLE.key not in SPECIALIST_CHAIN:
        return
    context: dict[str, Any] = {}
    if ROLE.key == "verification":
        seller_ids = [str(q.get("seller_id", "")) for q in task.payload.get("quotes", [])]
        context["seller_records"] = await store.seller_records(seller_ids)
    await ctx.send(sender, execute_specialist(ROLE.key, task, context))


@task_proto.on_message(model=TaskResult)
async def handle_task_result(ctx: Context, sender: str, result: TaskResult) -> None:
    if ROLE.key != "orchestrator" or sender != ADDRESSES.get(result.source_role):
        return
    if result.source_role == "fulfillment":
        await _checkout_result(ctx, result)
        return
    try:
        next_task, state = await accept_result(store, result)
    except (KeyError, ValueError) as exc:
        ctx.logger.warning("Ignoring uncorrelated result: %s", exc)
        return
    await _audit(
        ctx,
        state,
        "specialist_completed" if result.ok else "specialist_failed",
        role=result.source_role,
        ok=result.ok,
    )
    if result.source_role == "intake" and result.ok:
        brief = dict(result.payload["brief"])
        await _audit(
            ctx, state, "preferences_seen", preferences={k: brief[k] for k in SAVED_PREFERENCES}
        )
    if next_task:
        await _dispatch(ctx, next_task)
    elif state["status"] == "awaiting_selection":
        await _show_bundles(ctx, result.requester, state)
    elif state["status"] == "failed":
        error = str(state.get("error", "Something went wrong"))
        if error.startswith("Missing"):
            await _send_card(
                ctx,
                result.requester,
                f"I need a bit more detail. {error}.",
                cards.request_form(upcoming_days(), note=error),
            )
        else:
            await _send_card(ctx, result.requester, error, cards.error_card(error))


# ---------------------------------------------------------------------------
# Discovery: fan out QuoteRequests to sellers, fan in their Quotes.
# ---------------------------------------------------------------------------


async def _almanac_sellers(ctx: Context, known: set[str]) -> list[str]:
    """Optionally look up agents that publish the ResourceHold seller protocol."""
    if not env_bool("ALMANAC_DISCOVERY", False):
        return []
    try:
        found = await asyncio.wait_for(
            asyncio.to_thread(ctx.get_agents_by_protocol, seller_protocol_digest(), 10),
            timeout=8,
        )
    except Exception as exc:
        ctx.logger.warning("Almanac discovery failed: %s", type(exc).__name__)
        return []
    return [address for address in found if address not in known][:5]


async def _start_discovery(ctx: Context, task: TaskRequest) -> None:
    if utc_now() > parse(task.deadline_at):
        await ctx.send(
            ADDRESSES["orchestrator"],
            task_result(task, "discovery", ok=False, error="Task timed out"),
        )
        return
    brief = dict(task.payload["brief"])
    listings = [listing for listing in listings_for(list(brief["items"])) if listing.address]
    expected = {listing.address: listing.seller_id for listing in listings}
    unlisted = await _almanac_sellers(ctx, set(expected))
    await store.create_round(
        task.request_id,
        "discovery",
        later(QUOTE_WINDOW_SECONDS),
        {"task": task.dict(), "expected": expected, "unlisted": unlisted},
    )
    request = QuoteRequest(
        round_id=task.request_id,
        workflow_id=task.workflow_id,
        categories=list(brief["items"]),
        quantities={key: int(value) for key, value in brief["items"].items()},
        window_start=str(brief["window_start"]),
        window_end=str(brief["window_end"]),
    )
    for address in [*expected, *unlisted]:
        await ctx.send(address, request)
    ctx.logger.info(
        "Workflow %s: asked %d listed and %d unlisted sellers for quotes",
        task.workflow_id,
        len(expected),
        len(unlisted),
    )


@hold_proto.on_message(model=Quote)
async def handle_quote(ctx: Context, sender: str, quote: Quote) -> None:
    if ROLE.key != "discovery":
        return
    current = await store.get_round(quote.round_id)
    if current is None or (sender not in current["expected"] and sender not in current["unlisted"]):
        return
    # Key replies by the authenticated sender, never by the seller_id the quote claims.
    state = await store.record_reply(quote.round_id, sender, {**quote.dict(), "sender": sender})
    if state and len(state["replies"]) >= len(state["expected"]) + len(state["unlisted"]):
        await _finish_discovery(ctx, state)


async def _finish_discovery(ctx: Context, state: dict[str, Any]) -> None:
    if not await store.close_round(str(state["round_id"])):
        return
    task = TaskRequest.parse_obj(state["task"])
    quotes = list(state["replies"].values())
    silent = [
        seller_id
        for address, seller_id in state["expected"].items()
        if address not in state["replies"]
    ]
    for quote in quotes:
        if quote["sender"] in state["expected"]:
            await _seller_outcome(
                ctx, task.workflow_id, state["expected"][quote["sender"]], "quotes_sent"
            )
    for seller_id in silent:
        await _seller_outcome(ctx, task.workflow_id, seller_id, "no_responses")
    if not quotes:
        result = task_result(
            task,
            "discovery",
            ok=False,
            error=f"No seller answered within {QUOTE_WINDOW_SECONDS} seconds. Try again shortly",
        )
    else:
        result = task_result(
            task,
            "discovery",
            ok=True,
            payload={**task.payload, "quotes": quotes, "no_response": silent},
        )
    await ctx.send(ADDRESSES["orchestrator"], result)


@agent.on_interval(period=2.0)
async def sweep(ctx: Context) -> None:
    if not READY:
        return
    if ROLE.key == "discovery":
        for expired in await store.list_expired_rounds("discovery", _now()):
            await _finish_discovery(ctx, expired)
    if ROLE.key == "fulfillment":
        for expired in await store.list_expired_rounds("fulfillment", _now()):
            await _finish_checkout_round(ctx, expired, timed_out=True)
    if ROLE.key == "orchestrator":
        now = _now()
        for overdue in await store.list_overdue_workflows(now):
            await _recover_overdue(ctx, overdue)
        for due in await store.list_overdue_workflows(now, status="booking_failed"):
            await _refund(ctx, str(due["requester"]), due, automatic=True)


# ---------------------------------------------------------------------------
# Checkout Coordinator: all-or-nothing hold and commit rounds across sellers.
# ---------------------------------------------------------------------------


async def _start_checkout_round(ctx: Context, task: TaskRequest) -> None:
    phase = str(task.payload.get("phase", ""))
    if phase == "release":
        reason = str(task.payload.get("reason", "Released by the buyer"))[:200]
        for hold in task.payload.get("holds", []):
            await ctx.send(
                str(hold["address"]), ReleaseHold(hold_id=hold["hold_id"], reason=reason)
            )
        return
    if utc_now() > parse(task.deadline_at):
        await ctx.send(
            ADDRESSES["orchestrator"],
            task_result(task, "fulfillment", ok=False, error="Task timed out", payload={}),
        )
        return
    reference = str(task.payload.get("payment_reference", ""))
    try:
        if phase == "hold":
            holds = hold_plan(task.request_id, dict(task.payload["bundle"]))
        elif phase == "commit":
            payment = await store.get_payment(reference)
            if payment is None or payment["status"] != "reserved":
                raise ValueError("No reserved, verified payment for this booking")
            holds = {str(hold["hold_id"]): dict(hold) for hold in task.payload["holds"]}
        else:
            raise ValueError(f"Unknown checkout phase: {phase}")
    except (KeyError, ValueError) as exc:
        await ctx.send(
            ADDRESSES["orchestrator"],
            task_result(task, "fulfillment", ok=False, error=str(exc), payload={"phase": phase}),
        )
        return
    await store.create_round(
        task.request_id,
        "fulfillment",
        later(HOLD_WINDOW_SECONDS),
        {"task": task.dict(), "phase": phase, "holds": holds},
    )
    brief = dict(task.payload.get("brief", {}))
    for hold in holds.values():
        message: HoldRequest | CommitHold
        if phase == "hold":
            message = HoldRequest(
                round_id=task.request_id,
                workflow_id=task.workflow_id,
                hold_id=hold["hold_id"],
                item_id=hold["item_id"],
                quantity=hold["quantity"],
                window_start=str(brief["window_start"]),
                window_end=str(brief["window_end"]),
            )
        else:
            message = CommitHold(
                round_id=task.request_id, hold_id=hold["hold_id"], payment_reference=reference
            )
        await ctx.send(str(hold["address"]), message)


async def _record_checkout_reply(
    ctx: Context, sender: str, round_id: str, hold_id: str, phase: str, reply: dict[str, Any]
) -> None:
    current = await store.get_round(round_id)
    if current is None or current["owner_role"] != "fulfillment" or current["phase"] != phase:
        return
    hold = current["holds"].get(hold_id)
    # Only the seller agent the hold was sent to may answer for it.
    if hold is None or hold["address"] != sender:
        return
    state = await store.record_reply(round_id, hold_id, reply)
    if state is None:
        latest = await store.get_round(round_id)
        if latest and latest["closed"] and hold_id not in latest["replies"]:
            # The round already failed; never leave a late success behind.
            await ctx.send(
                sender, ReleaseHold(hold_id=hold_id, reason="Arrived after the round closed")
            )
        return
    if round_complete(state):
        await _finish_checkout_round(ctx, state, timed_out=False)


@hold_proto.on_message(model=HoldConfirmed)
async def handle_hold_confirmed(ctx: Context, sender: str, message: HoldConfirmed) -> None:
    if ROLE.key != "fulfillment":
        return
    current = await store.get_round(message.round_id)
    hold = (current or {}).get("holds", {}).get(message.hold_id)
    if hold and int(message.subtotal_cents) != int(hold["subtotal_cents"]):
        reply = {"status": "rejected", "reason": "Confirmed a different price than it quoted"}
    else:
        reply = {"status": "held", "expires_at": message.expires_at}
    await _record_checkout_reply(ctx, sender, message.round_id, message.hold_id, "hold", reply)


@hold_proto.on_message(model=HoldRejected)
async def handle_hold_rejected(ctx: Context, sender: str, message: HoldRejected) -> None:
    if ROLE.key != "fulfillment":
        return
    current = await store.get_round(message.round_id)
    phase = str((current or {}).get("phase", ""))
    reply = {"status": "rejected", "reason": message.reason[:200]}
    await _record_checkout_reply(ctx, sender, message.round_id, message.hold_id, phase, reply)


@hold_proto.on_message(model=BookingConfirmed)
async def handle_booking_confirmed(ctx: Context, sender: str, message: BookingConfirmed) -> None:
    if ROLE.key != "fulfillment":
        return
    reply = {"status": "booked", "confirmation_code": message.confirmation_code[:40]}
    await _record_checkout_reply(ctx, sender, message.round_id, message.hold_id, "commit", reply)


async def _finish_checkout_round(ctx: Context, state: dict[str, Any], *, timed_out: bool) -> None:
    if not await store.close_round(str(state["round_id"])):
        return
    task = TaskRequest.parse_obj(state["task"])
    phase = str(state["phase"])
    outcome = round_outcome(state, timed_out=timed_out)
    ok_counter, failed_counter = (
        ("holds_confirmed", "holds_failed")
        if phase == "hold"
        else ("bookings_committed", "bookings_failed")
    )
    failed_ids = list(dict.fromkeys(item["seller_id"] for item in outcome["failed"]))
    confirmed_ids = {item["seller_id"] for item in outcome["confirmed"]} - set(failed_ids)
    for seller_id in sorted(confirmed_ids):
        await _seller_outcome(ctx, task.workflow_id, seller_id, ok_counter)
    for seller_id in failed_ids:
        await _seller_outcome(ctx, task.workflow_id, seller_id, failed_counter)

    if not outcome["failed"] and not outcome["cancelled"]:
        if phase == "hold":
            holds = outcome["confirmed"]
            payload = {
                "phase": phase,
                "holds": holds,
                "expires_at": min((str(hold["expires_at"]) for hold in holds), key=parse),
            }
        else:
            payload = {
                "phase": phase,
                "bookings": [
                    {
                        key: item[key]
                        for key in (
                            "hold_id",
                            "seller_id",
                            "item_id",
                            "confirmation_code",
                            "address",
                        )
                    }
                    for item in outcome["confirmed"]
                ],
            }
        await ctx.send(
            ADDRESSES["orchestrator"], task_result(task, "fulfillment", ok=True, payload=payload)
        )
        return

    # All or nothing: release every hold in the round, including any that may land late.
    reason = "Released because another seller in the bundle could not confirm"
    for hold in state["holds"].values():
        await ctx.send(str(hold["address"]), ReleaseHold(hold_id=hold["hold_id"], reason=reason))
    await ctx.send(
        ADDRESSES["orchestrator"],
        task_result(
            task,
            "fulfillment",
            ok=False,
            error=failure_message(phase, outcome["failed"]),
            payload={"phase": phase, "failed_sellers": failed_ids},
        ),
    )


# ---------------------------------------------------------------------------
# Memory and audit
# ---------------------------------------------------------------------------


@task_proto.on_message(model=AuditEvent)
async def handle_audit(ctx: Context, sender: str, event: AuditEvent) -> None:
    if ROLE.key != "memory_audit" or sender not in ADDRESSES.values():
        return
    if not await store.append_event(
        event.workflow_id, event.event_id, event.event_type, dict(event.details)
    ):
        return
    if event.event_type == "seller_outcome":
        await store.record_seller_outcome(
            str(event.details["seller_id"]), str(event.details["counter"])
        )
    preferences = event.details.get("preferences")
    if isinstance(preferences, dict):
        await store.save_preference(event.requester, preferences)


@chat_proto.on_message(model=ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, message: ChatAcknowledgement) -> None:
    return None


agent.include(chat_proto, publish_manifest=True)
agent.include(task_proto, publish_manifest=True)
if ROLE.key in {"discovery", "fulfillment"}:
    agent.include(hold_proto, publish_manifest=True)
if ROLE.key == "orchestrator":
    agent.include(payment_proto, publish_manifest=True)


if __name__ == "__main__":
    agent.run()
