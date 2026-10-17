"""Runtime for one ResourcePilot platform agent, selected by AGENT_ROLE.

Every role is its own process, seed, address, and profile. The Orchestrator delegates with
``ctx.send``; specialists reply with correlated TaskResults. Discovery fans out to independent
seller agents over the ResourceHold protocol.
"""

from __future__ import annotations

import asyncio
import os
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

from resourcepilot import cards
from resourcepilot.clock import later, parse, utc_now
from resourcepilot.config import (
    SPECIALIST_CHAIN,
    agent_addresses,
    env_bool,
    env_int,
    get_role,
)
from resourcepilot.intake import SAVED_PREFERENCES, parse_request_text, upcoming_days
from resourcepilot.models import AuditEvent, HealthResponse, TaskRequest, TaskResult
from resourcepilot.pricing import usd
from resourcepilot.protocols import (
    BookingConfirmed,
    HoldConfirmed,
    HoldRejected,
    Quote,
    QuoteRequest,
    resource_hold_spec,
    seller_protocol_digest,
)
from resourcepilot.registry import listings_for
from resourcepilot.store import store
from resourcepilot.workflow import (
    accept_result,
    begin_workflow,
    execute_specialist,
    is_timed_out,
    task_result,
)

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]
ROLE = get_role()
ADDRESSES = agent_addresses()
PORT = int(os.getenv("AGENT_PORT", str(ROLE.configured_port())))
QUOTE_WINDOW_SECONDS = env_int("QUOTE_WINDOW_SECONDS", 12, minimum=3)

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
    await _send_card(
        ctx,
        sender,
        narration,
        cards.bundle_carousel(str(state["workflow_id"]), list(data["bundles"]), rejected),
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
            cards.bundle_detail(workflow_id, state["data"]["brief"], bundle, checkout=False),
        )
        return
    await ctx.send(sender, cards.text_message("That action is not valid for this request."))


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


# Buyer-role handlers that only the Checkout Coordinator acts on.
@hold_proto.on_message(model=HoldConfirmed)
async def handle_hold_confirmed(ctx: Context, sender: str, message: HoldConfirmed) -> None:
    return None


@hold_proto.on_message(model=HoldRejected)
async def handle_hold_rejected(ctx: Context, sender: str, message: HoldRejected) -> None:
    return None


@hold_proto.on_message(model=BookingConfirmed)
async def handle_booking_confirmed(ctx: Context, sender: str, message: BookingConfirmed) -> None:
    return None


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


if __name__ == "__main__":
    agent.run()
