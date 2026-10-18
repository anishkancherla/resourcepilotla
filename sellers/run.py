"""Runtime for one independent seller agent, selected by SELLER_ID.

Each seller is its own uAgent with its own seed, address, wallet, and profile. It speaks the
ResourceHold seller role and the Agent Chat Protocol. It never takes payment: the buyer pays
ResourcePilot's escrow, and the seller is paid out after every booking commits.

Demo sellers answer from a catalog. Campus Rooms (``live`` in the registry) answers from the
school's public LibCal pages instead, and every one of its offers is a free booking handoff.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

from resourcepilot import cards
from resourcepilot.clock import utc_now
from resourcepilot.config import env_bool, env_int, get_seller
from resourcepilot.http import Http, build_http
from resourcepilot.models import HealthResponse
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
)
from resourcepilot.registry import CATEGORIES, LISTING_BY_ID
from resourcepilot.store import store
from sellers.inventory import build_offers, commit_hold, place_hold, resolve_fault_mode
from sellers.live_rooms import commit_room, hold_room, quote_rooms

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = get_seller()
LISTING = LISTING_BY_ID[IDENTITY.key]
PORT = int(os.getenv("AGENT_PORT", str(IDENTITY.configured_port())))
HOLD_TTL_SECONDS = env_int("HOLD_TTL_SECONDS", 600, minimum=60)
ORCHESTRATOR_ADDRESS = os.getenv("ORCHESTRATOR_ADDRESS", "").strip()

agent = Agent(
    name=IDENTITY.name,
    seed=IDENTITY.seed(),
    port=PORT,
    mailbox=env_bool("ENABLE_MAILBOX", True),
    agentverse=os.getenv("AGENTVERSE_URL", "https://agentverse.ai"),
    publish_agent_details=True,
    readme_path=str(ROOT / IDENTITY.readme_dir / "README.md"),
)

chat_proto = Protocol(spec=chat_protocol_spec)
hold_proto = Protocol(spec=resource_hold_spec, role="seller")
READY = False
HTTP: Http | None = None


def _http() -> Http:
    global HTTP
    if HTTP is None:
        HTTP = build_http(store, follow_redirects=False)
    return HTTP


def _now() -> str:
    return utc_now().isoformat()


@agent.on_event("startup")
async def startup(ctx: Context) -> None:
    global READY
    await store.setup()
    if not store.production_ready:
        ctx.logger.warning("DATABASE_URL is unset: using a NON-PRODUCTION process-local store.")
    READY = True
    ctx.logger.info("%s ready at %s on port %s", IDENTITY.name, agent.address, PORT)


@agent.on_event("shutdown")
async def shutdown(ctx: Context) -> None:
    global READY
    READY = False
    await store.close()


@agent.on_rest_get("/health", HealthResponse)
async def health(ctx: Context) -> HealthResponse:
    return HealthResponse(
        status="ok" if READY else "starting", role=IDENTITY.key, address=str(agent.address)
    )


@hold_proto.on_message(model=QuoteRequest)
async def handle_quote_request(ctx: Context, sender: str, request: QuoteRequest) -> None:
    if await resolve_fault_mode(store, LISTING.seller_id) == "ghost_quotes":
        ctx.logger.warning("Fault mode ghost_quotes: ignoring quote request %s", request.round_id)
        return
    if LISTING.live:
        offers = await quote_rooms(_http(), LISTING, request)
    else:
        offers = await build_offers(store, LISTING, request, _now())
    ctx.logger.info(
        "Quote for workflow %s: %d offers across %s",
        request.workflow_id,
        len(offers),
        ", ".join(request.categories),
    )
    await ctx.send(
        sender,
        Quote(
            round_id=request.round_id,
            seller_id=LISTING.seller_id,
            hold_ttl_seconds=HOLD_TTL_SECONDS,
            offers=offers,
        ),
    )


@hold_proto.on_message(model=HoldRequest)
async def handle_hold_request(ctx: Context, sender: str, request: HoldRequest) -> None:
    mode = await resolve_fault_mode(store, LISTING.seller_id)
    if mode == "ghost_holds":
        ctx.logger.warning("Fault mode ghost_holds: ignoring hold %s", request.hold_id)
        return
    if mode == "reject_holds":
        await ctx.send(
            sender,
            HoldRejected(
                round_id=request.round_id,
                hold_id=request.hold_id,
                seller_id=LISTING.seller_id,
                reason="Seller is temporarily unavailable",
            ),
        )
        return
    if LISTING.live:
        reply = await hold_room(
            _http(), store, LISTING, request, ttl_seconds=HOLD_TTL_SECONDS, now=_now()
        )
    else:
        reply = await place_hold(store, LISTING, request, ttl_seconds=HOLD_TTL_SECONDS, now=_now())
    ctx.logger.info(
        "Hold %s for workflow %s: %s",
        request.hold_id,
        request.workflow_id,
        "confirmed" if isinstance(reply, HoldConfirmed) else f"rejected ({reply.reason})",
    )
    await ctx.send(sender, reply)


@hold_proto.on_message(model=CommitHold)
async def handle_commit(ctx: Context, sender: str, request: CommitHold) -> None:
    if await resolve_fault_mode(store, LISTING.seller_id) == "ghost_commits":
        ctx.logger.warning("Fault mode ghost_commits: ignoring commit %s", request.hold_id)
        return
    if LISTING.live:
        reply = await commit_room(store, LISTING, request, now=_now())
    else:
        reply = await commit_hold(store, LISTING, request, now=_now())
    ctx.logger.info(
        "Commit %s: %s",
        request.hold_id,
        "booked" if isinstance(reply, BookingConfirmed) else f"rejected ({reply.reason})",
    )
    await ctx.send(sender, reply)


@hold_proto.on_message(model=ReleaseHold)
async def handle_release(ctx: Context, sender: str, request: ReleaseHold) -> None:
    hold = await store.get_hold(request.hold_id)
    if hold and hold["seller_id"] == LISTING.seller_id:
        if await store.release_hold(request.hold_id, request.reason[:200]):
            ctx.logger.info("Released %s: %s", request.hold_id, request.reason[:200])


def _chat_text(message: ChatMessage) -> str:
    return " ".join(
        item.text for item in (message.content or []) if isinstance(item, TextContent)
    ).strip()


@chat_proto.on_message(model=ChatMessage)
async def handle_chat(ctx: Context, sender: str, message: ChatMessage) -> None:
    await ctx.send(
        sender,
        ChatAcknowledgement(timestamp=datetime.now(UTC), acknowledged_msg_id=message.msg_id),
    )
    if not await store.claim(f"chat:{IDENTITY.key}", str(message.msg_id)):
        return
    action = str(cards.parse_selection(_chat_text(message)).get("action", ""))
    demo_controls = env_bool("SELLER_DEMO_CONTROLS", False)
    if demo_controls and action in {"simulate_outage", "restore_service"}:
        mode = "ghost_holds" if action == "simulate_outage" else "none"
        await store.set_fault_mode(LISTING.seller_id, mode)
    mode = await resolve_fault_mode(store, LISTING.seller_id)
    healthy = mode == "none"
    badges = [
        ("Online" if healthy else "Simulated outage", "success" if healthy else "warning"),
        ("Verified listing" if LISTING.identity_verified else "Unverified", "info"),
    ]
    sells = ", ".join(CATEGORIES[category] for category in LISTING.categories)
    if LISTING.live:
        narration = (
            f"I am {LISTING.display_name}. I read real open study rooms from a school's public "
            "LibCal pages and hand you the school's booking link. Rooms are always free."
        )
        summary = "Live seller agent. Quotes real open library rooms as free booking handoffs."
        facts = [
            "Any school the Campus Researcher can match",
            "ResourcePilot never reserves the room; you book with your own sign-in",
        ]
        badges[1] = ("Live \u00b7 LibCal", "info")
    else:
        narration = (
            f"I am {LISTING.display_name}, a demo seller. I sell: {sells}. "
            "Book me through the ResourcePilot Orchestrator."
        )
        summary = f"Demo seller agent. Sells {sells}."
        facts = [
            f"{LISTING.location_label} \u00b7 open {LISTING.open_hour}:00-{LISTING.close_hour}:00",
            LISTING.verification_basis,
        ]
    await ctx.send(
        sender,
        cards.card_message(
            narration,
            *cards.status_card(
                LISTING.display_name,
                summary,
                ORCHESTRATOR_ADDRESS,
                badges=badges,
                facts=facts,
                extra_buttons=cards.demo_control_buttons(mode) if demo_controls else None,
            ),
        ),
    )


@chat_proto.on_message(model=ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, message: ChatAcknowledgement) -> None:
    return None


agent.include(chat_proto, publish_manifest=True)
agent.include(hold_proto, publish_manifest=True)


if __name__ == "__main__":
    agent.run()
