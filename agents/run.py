"""Runtime for one ResourcePilot agent, selected by AGENT_ROLE.

Eight agents, each its own process, seed, address, and profile. The Orchestrator owns the ASI:One
chat and the workflow, and delegates every step with ``ctx.send``; specialists reply with
correlated TaskResults. Study rooms: Intake, then the Campus Researcher (finds the school's room
system), Campus Rooms (reads open rooms live), Verification, and the Planner. Before handing over a
booking link, Campus Rooms re-checks that room live; a room that was just taken sends the Planner
back to the other open rooms. Health and wellness runs the same chain for wellness rooms, plus
the Researcher's events feed, which Verification re-fetches; a school with no room system skips
Campus Rooms and the Planner. Housing: Intake, the Campus Researcher, then Verification, which
re-opens every official link and hall page, then the Planner's hall match. When payments are set
up, the match is the one paid step: the Orchestrator asks for Stripe test-mode or testnet FET
payment over the Agent Payment Protocol and checks it itself first; the facts stay free.
Emergency (demo): the Emergency Dispatcher checks the form, the student reviews the call, and
only "Call now" makes it phone the one configured demo contact, never 911; the call is never
retried. ResourcePilot never reserves a room and never pays for or
applies for housing.
"""

from __future__ import annotations

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
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    RejectPayment,
    payment_protocol_spec,
)

from resourcepilot import cards, payments
from resourcepilot.clock import parse, utc_now
from resourcepilot.config import (
    ROLE_BY_KEY,
    SERVICES,
    SPECIALISTS,
    agent_addresses,
    env_bool,
    get_role,
    service_chain,
)
from resourcepilot.emergency import MAX_DETAILS, MAX_LOCATION, MAX_SCHOOL, clean_text
from resourcepilot.emergency import emergency_handler as emergency_task
from resourcepilot.housing import recheck_halls
from resourcepilot.http import Http, ProviderError, build_http
from resourcepilot.intake import scrub_fields
from resourcepilot.models import AuditEvent, HealthResponse, TaskRequest, TaskResult
from resourcepilot.research import recheck_events, recheck_link, research
from resourcepilot.rooms import campus_rooms_handler
from resourcepilot.store import store
from resourcepilot.workflow import (
    SIDE_STAGES,
    accept_result,
    accept_side_result,
    advance_to,
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
# Only the Orchestrator sells anything: the optional hall match. A live Stripe key stops it here.
payment_proto = Protocol(spec=payment_protocol_spec, role="seller")
PAYMENTS = payments.load_config() if ROLE.key == "orchestrator" else payments.PaymentConfig()
READY = False
HTTP: Http | None = None
# Network specialists: they read the public web, so they run outside the synchronous HANDLERS.
LIVE_ROLES = frozenset({"researcher", "campus_rooms", "emergency"})


def _http() -> Http:
    """Network access for the Campus Researcher, Campus Rooms, Verification, and the Dispatcher."""
    global HTTP
    if HTTP is None:
        HTTP = build_http(store)
    return HTTP


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


async def _notice(
    ctx: Context,
    target: str,
    title: str,
    body: str,
    *,
    badge: str | None = None,
    variant: str = "info",
    buttons: list[tuple[str, dict[str, Any]]] | None = None,
) -> None:
    """Every Orchestrator reply is a card; a short notice replaces plain text."""
    await _send_card(
        ctx,
        target,
        body,
        cards.notice_card(title, body, badge=badge, variant=variant, buttons=buttons),
    )


def _status_button(workflow_id: str) -> tuple[str, dict[str, Any]]:
    return ("Show status", {"action": "show_status", "workflow_id": workflow_id})


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
            f"I am the {ROLE.name}. {ROLE.responsibility}. Chat with the Orchestrator to start.",
            cards.status_card(
                ROLE.name,
                ROLE.responsibility,
                ADDRESSES["orchestrator"],
                image=cards.agent_art(ROLE.key),
            ),
        )
        return
    await _orchestrator_chat(ctx, sender, text, cards.parse_selection(text))


FORM_PROMPTS = {
    "rooms": "Enter your school email and when you need a room. Only the domain is kept.",
    "wellness": "Enter your school email and when you'd like a quiet room. Only the domain is "
    "kept. In crisis? Call or text 988.",
    "housing": "Enter your school email and what you need in a hall. Only the domain is kept.",
    "emergency": "Describe the drill. It calls your team's demo contact, never 911. "
    "In a real emergency, call 911.",
}
# Free-text drill fields are trimmed and stripped of emails before the request is stored.
EMERGENCY_TEXT = {"location": MAX_LOCATION, "details": MAX_DETAILS, "school": MAX_SCHOOL}
FIELD_LIMIT = 200


def _form_fields(selection: dict[str, Any]) -> dict[str, Any]:
    """Bounded scalar fields from a form submission, with any email reduced to its domain.

    A picture-tile pick (``choice_grid``) may arrive as a one-item list; it is unwrapped.
    """
    fields: dict[str, Any] = {}
    for key, raw in selection.items():
        value = raw[0] if isinstance(raw, list) and len(raw) == 1 else raw
        if key in {"action", "selection", "service"} or not isinstance(
            value, str | int | float | bool
        ):
            continue
        fields[str(key)[:40]] = str(value)[:FIELD_LIMIT] if isinstance(value, str) else value
    return scrub_fields(fields)


async def _start_workflow(ctx: Context, sender: str, service: str, fields: dict[str, Any]) -> None:
    if service == "emergency":
        fields = {
            key: clean_text(value, EMERGENCY_TEXT[key]) if key in EMERGENCY_TEXT else value
            for key, value in fields.items()
        }
    request = {"service": service, "fields": fields}
    task = await begin_workflow(store, requester=sender, session_id=_session(ctx), request=request)
    state = await store.get_workflow(task.workflow_id)
    assert state is not None
    await _audit(ctx, state, "workflow_started", stage=task.target_role, service=service)
    await _dispatch(ctx, task)
    chain = service_chain(service)
    names = ", ".join(ROLE_BY_KEY[role].name.removeprefix("ResourcePilot ") for role in chain)
    await _send_card(
        ctx,
        sender,
        f"Got it. {names} {'is' if len(chain) == 1 else 'are'} on it.",
        cards.progress_card(task.workflow_id, task.target_role, chain),
    )


async def _orchestrator_chat(
    ctx: Context, sender: str, text: str, selection: dict[str, Any]
) -> None:
    action = str(selection.get("action", "")).strip()
    service = str(selection.get("service", "")).strip()
    stale = action in {"open_service", "submit_form"} and service not in SERVICES
    if not action or stale or action in {"start_over", "open_form", "open_orchestrator"}:
        narration = "Pick a campus service."
        if text and not selection:
            narration = "ResourcePilot works from buttons and forms. Pick a campus service."
        elif stale:
            narration = "That service is not available. Pick a campus service."
        await _send_card(ctx, sender, narration, cards.welcome_card())
        return
    if action == "open_service" and service in SERVICES:
        await _send_card(ctx, sender, FORM_PROMPTS[service], cards.service_form(service))
        return
    if action == "submit_form" and service in SERVICES:
        await _start_workflow(ctx, sender, service, _form_fields(selection))
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
        await _notice(
            ctx,
            sender,
            "Request not found",
            "I could not find that request. Pick a service to start again.",
            badge="Not found",
            variant="warning",
        )
        return
    await _workflow_action(ctx, sender, action, selection, state)


def _option_index(state: dict[str, Any], option_id: str) -> int:
    """The option's place in the rooms carousel, so its detail shows the same picture."""
    options = state.get("data", {}).get("options", [])
    return next(
        (index for index, option in enumerate(options) if option.get("option_id") == option_id),
        0,
    )


def _option(state: dict[str, Any], option_id: str) -> dict[str, Any] | None:
    return next(
        (
            dict(option)
            for option in state.get("data", {}).get("options", [])
            if option.get("option_id") == option_id
        ),
        None,
    )


def _school_name(state: dict[str, Any]) -> str:
    return str(state.get("data", {}).get("school", {}).get("school_name") or "your school")


def _wellness(state: dict[str, Any]) -> bool:
    return state.get("service") == "wellness"


async def _show_rooms(ctx: Context, sender: str, state: dict[str, Any], *, note: str = "") -> None:
    prefix = f"{note} " if note else ""
    if _wellness(state):
        await _show_wellness(ctx, sender, state, prefix=prefix)
        return
    options = list(state["data"].get("options", []))
    school = _school_name(state)
    count = len(options)
    await _send_card(
        ctx,
        sender,
        f"{prefix}{count} open room{'s' if count != 1 else ''} at {school}, earliest first. "
        "They are free; you book on your school's site.",
        cards.rooms_carousel(str(state["workflow_id"]), options, school),
    )


async def _send_link(ctx: Context, sender: str, state: dict[str, Any], narration: str) -> None:
    receipt = cards.room_link_receipt(
        dict(state["option"]), service=str(state.get("service") or "rooms")
    )
    await _send_card(ctx, sender, narration, receipt, terminal=True)


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
                cards.error_card(
                    "A specialist timed out. Start over to try again.",
                    service=str(state.get("service") or ""),
                ),
            )
        elif state["status"] == "awaiting_link":
            await _ask_for_link(ctx, sender, state)
        elif state["status"] == "answered":
            await _show_answer(ctx, sender, state)
        elif state["status"] == "awaiting_selection":
            await _show_rooms(ctx, sender, state)
        elif state["status"] == "booked":
            await _send_link(ctx, sender, state, "Here is your booking link again.")
        elif state["status"] == "awaiting_call":
            await _show_call_review(ctx, sender, state)
        elif state["status"] == "awaiting_payment":
            await _show_match_offer(ctx, sender, state)
        elif state["status"] == "called":
            await _show_call(ctx, sender, state, "Here is your demo call.")
        elif state["status"] == "failed":
            error = str(state.get("error") or "Something went wrong")
            await _send_card(
                ctx,
                sender,
                error,
                cards.error_card(error, service=str(state.get("service") or "")),
            )
        else:
            await _send_card(
                ctx,
                sender,
                f"Current status: {state['status'].replace('_', ' ')}.",
                cards.progress_card(workflow_id, str(state["stage"]), _chain(state)),
            )
        return
    if action == "submit_link":
        await _submit_link(ctx, sender, state, str(selection.get("booking_link") or ""))
        return
    if action == "place_call":
        await _place_call(ctx, sender, state)
        return
    if action == "call_status":
        await _check_call(ctx, sender, state)
        return
    if action == "unlock_housing":
        await _request_match_payment(ctx, sender, state)
        return
    housing_done = state.get("service") == "housing" and state["status"] == "answered"
    if action == "view_housing" and housing_done:
        hall = _hall(state, str(selection.get("option_id", "")))
        if hall:
            await _send_card(
                ctx,
                sender,
                f"{hall['name']}, as {_school_name(state)}'s own page describes it. "
                "You apply on the school's site.",
                cards.housing_detail(workflow_id, hall, state["data"]),
            )
            return
    facts_ready = housing_done or state["status"] == "awaiting_payment"
    if action == "show_housing_facts" and state.get("service") == "housing" and facts_ready:
        await _show_housing_facts(ctx, sender, state)
        return
    if action == "show_all_housing" and housing_done:
        await _show_housing(ctx, sender, state, show_all=True)
        return
    if action == "show_results" and state["status"] == "awaiting_selection":
        await _show_rooms(ctx, sender, state)
        return
    option = _option(state, str(selection.get("option_id", "")))
    if action == "view_room" and option:
        await _send_card(
            ctx,
            sender,
            f"{option['lines'][0]['name']}: free, re-checked live before you book.",
            cards.room_detail(
                workflow_id,
                option,
                wellness=_wellness(state),
                index=_option_index(state, str(option["option_id"])),
            ),
        )
        return
    if action == "recheck_room" and option:
        await _start_recheck(ctx, sender, state, option)
        return
    await _notice(
        ctx,
        sender,
        "Not available",
        "That action is not valid for this request right now.",
        badge="Not available",
        variant="warning",
        buttons=[_status_button(workflow_id), ("Start over", {"action": "start_over"})],
    )


def _chain(state: dict[str, Any]) -> tuple[str, ...]:
    return service_chain(str(state.get("service") or "rooms"), state.get("data") or {})


def _hall(state: dict[str, Any], option_id: str) -> dict[str, Any] | None:
    halls = state.get("data", {}).get("all_halls") or []
    return next((dict(hall) for hall in halls if hall.get("option_id") == option_id), None)


async def _show_answer(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    """A finished lookup with nothing to re-check: housing, or wellness without a room system."""
    if _wellness(state):
        await _show_wellness(ctx, sender, state)
        return
    await _show_housing(ctx, sender, state)


async def _show_housing(
    ctx: Context, sender: str, state: dict[str, Any], *, show_all: bool = False
) -> None:
    """The halls carousel; a no-match card; or, when no hall could be read, the facts card."""
    data = state["data"]
    workflow_id = str(state["workflow_id"])
    if not data.get("all_halls"):
        await _show_housing_facts(ctx, sender, state)
        return
    if not data.get("halls") and not show_all:
        await _send_card(
            ctx,
            sender,
            f"No hall at {_school_name(state)} fits all of your criteria.",
            cards.no_housing_match_card(workflow_id, data),
        )
        return
    shown = len(data["all_halls"] if show_all else data["halls"])
    await _send_card(
        ctx,
        sender,
        f"{shown} on-campus housing option{'s' if shown != 1 else ''} at {_school_name(state)}, "
        "read from the school's own pages"
        + (", including ones your criteria rule out." if show_all else ", matches first."),
        cards.housing_carousel(workflow_id, data, show_all=show_all),
    )


async def _show_housing_facts(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    await _send_card(
        ctx,
        sender,
        f"Housing at {_school_name(state)}: official facts and links. "
        "You apply on the school's site.",
        cards.housing_card(state["data"]),
    )


async def _show_wellness(
    ctx: Context, sender: str, state: dict[str, Any], *, prefix: str = ""
) -> None:
    data = state["data"]
    rooms = len(data.get("options") or [])
    events = len((data.get("wellness") or {}).get("events") or [])
    await _send_card(
        ctx,
        sender,
        f"{prefix}Wellness at {_school_name(state)}: {rooms} open room{'s' if rooms != 1 else ''} "
        f"and {events} event{'s' if events != 1 else ''} this week. In crisis? Call or text 988.",
        cards.wellness_card(str(state["workflow_id"]), data),
    )


async def _ask_for_link(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    data = state["data"]
    school = _school_name(state)
    narration = (
        f"{data['link_error']}. Paste the booking link again."
        if data.get("link_error")
        else f"I could not find {school}'s room-booking system. Paste the booking link and I "
        "will check it."
    )
    await _send_card(
        ctx, sender, narration, cards.room_link_card(str(state["workflow_id"]), school)
    )


async def _submit_link(ctx: Context, sender: str, state: dict[str, Any], link: str) -> None:
    """Send the pasted link back to the Researcher, which verifies it like a found one."""
    if state["status"] != "awaiting_link":
        return
    if not link.strip():
        await _ask_for_link(ctx, sender, state)
        return
    if not await store.claim("submit_link", f"{state['workflow_id']}:{link.strip()[:300]}"):
        return
    data = dict(state["data"])
    data.pop("needs_link", None)
    data.pop("link_error", None)
    data["brief"] = {**data["brief"], "booking_link": link.strip()[:300]}
    task, state = await advance_to(
        store,
        workflow_id=str(state["workflow_id"]),
        requester=sender,
        session_id=str(state["session_id"]),
        next_role="researcher",
        payload=data,
    )
    await _dispatch(ctx, task)
    await _send_card(
        ctx,
        sender,
        "Checking that link on your school's site.",
        cards.progress_card(task.workflow_id, "researcher", _chain(state)),
    )


# ---------------------------------------------------------------------------
# The live re-check: Campus Rooms confirms the room is still open, then the link is handed over.
# ---------------------------------------------------------------------------


async def _start_recheck(
    ctx: Context, sender: str, state: dict[str, Any], option: dict[str, Any]
) -> None:
    """Claim one re-check per click; a double click never sends two."""
    workflow_id = str(state["workflow_id"])
    attempt = int(state.get("recheck_attempt", 0))
    if state["status"] != "awaiting_selection" or not await store.claim(
        "recheck", f"{workflow_id}:{attempt}"
    ):
        await _notice(
            ctx,
            sender,
            "Already in progress",
            "That room is already being re-checked.",
            buttons=[_status_button(workflow_id)],
        )
        return
    task = make_task(
        workflow_id=workflow_id,
        requester=sender,
        session_id=str(state["session_id"]),
        target_role="campus_rooms",
        payload={
            "phase": "recheck",
            "campus": dict(state["data"].get("campus") or {}),
            "line": dict(option["lines"][0]),
        },
    )
    state = await store.patch_workflow(
        workflow_id,
        {
            "status": "running",
            "stage": "recheck",
            "option": option,
            "recheck_attempt": attempt + 1,
            "expected_request_id": task.request_id,
            "deadline_at": task.deadline_at,
        },
    )
    await _audit(ctx, state, "recheck_requested", item_id=option["lines"][0]["item_id"])
    await _dispatch(ctx, task)
    await _send_card(
        ctx,
        sender,
        "Re-checking that room live on your school's site.",
        cards.progress_card(workflow_id, "recheck"),
    )


async def _reroute(ctx: Context, state: dict[str, Any], item_id: str, reason: str) -> None:
    """Ask the Planner again without the room that was just taken."""
    workflow_id, requester = str(state["workflow_id"]), str(state["requester"])
    payload = {
        **state["data"],
        "exclude_items": sorted({*state["data"].get("exclude_items", []), item_id}),
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
            "error": reason,
            "expected_request_id": task.request_id,
            "deadline_at": task.deadline_at,
        },
    )
    await _audit(ctx, state, "reroute_requested", exclude_items=payload["exclude_items"])
    await _dispatch(ctx, task)
    await _send_card(
        ctx,
        requester,
        f"{reason}. Showing the other open rooms.",
        cards.progress_card(workflow_id, "planner"),
    )


async def _recheck_result(ctx: Context, state: dict[str, Any], result: TaskResult) -> None:
    option = dict(state["option"])
    item_id = str(option["lines"][0]["item_id"])
    if not result.ok or not result.payload.get("open"):
        reason = str(
            result.payload.get("reason") or result.error or "That room is no longer open"
        ).rstrip(".")
        await _audit(ctx, state, "recheck_failed", item_id=item_id, reason=reason)
        await _reroute(ctx, state, item_id, reason)
        return
    state = await store.patch_workflow(
        str(state["workflow_id"]),
        {"status": "booked", "stage": "complete", "booked_at": _now()},
    )
    await _audit(ctx, state, "booked", item_id=item_id)
    await _send_link(
        ctx,
        str(state["requester"]),
        state,
        "Still open. Here is your booking link: open it and book with your own sign-in. "
        "ResourcePilot did not reserve the room.",
    )


# ---------------------------------------------------------------------------
# Emergency (demo): review, then exactly one call to the configured demo contact, never retried.
# ---------------------------------------------------------------------------


async def _show_call_review(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    preview = dict(state["data"].get("preview") or {})
    await _send_card(
        ctx,
        sender,
        f"Review the demo call. It will phone your demo contact ({preview.get('to', '')}), "
        "never 911. Press Call now to place it.",
        cards.emergency_review_card(str(state["workflow_id"]), preview),
    )


async def _show_call(ctx: Context, sender: str, state: dict[str, Any], narration: str) -> None:
    data = state["data"]
    await _send_card(
        ctx,
        sender,
        narration,
        cards.emergency_call_card(
            str(state["workflow_id"]), dict(data.get("call") or {}), data.get("call_status")
        ),
    )


async def _side_task(
    ctx: Context, state: dict[str, Any], stage: str, payload: dict[str, Any], **patch: Any
) -> dict[str, Any]:
    """Send the Emergency Dispatcher one out-of-chain step and wait for its answer."""
    task = make_task(
        workflow_id=str(state["workflow_id"]),
        requester=str(state["requester"]),
        session_id=str(state["session_id"]),
        target_role="emergency",
        payload=payload,
    )
    state = await store.patch_workflow(
        str(state["workflow_id"]),
        {
            "status": "running",
            "stage": stage,
            "expected_request_id": task.request_id,
            "deadline_at": task.deadline_at,
            **patch,
        },
    )
    await _dispatch(ctx, task)
    return state


async def _place_call(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    """Claim the one call this workflow may place; a second press never dials again."""
    workflow_id = str(state["workflow_id"])
    if state["status"] != "awaiting_call" or not await store.claim("place_call", workflow_id):
        await _notice(
            ctx,
            sender,
            "Already placed",
            "This demo call was already placed. Start a new demo to call again.",
            buttons=[_status_button(workflow_id), ("Menu", {"action": "start_over"})],
        )
        return
    data = state["data"]
    state = await _side_task(
        ctx,
        state,
        "call",
        {"phase": "call", "brief": dict(data["brief"]), "workflow_id": workflow_id},
    )
    await _audit(ctx, state, "demo_call_requested")
    await _send_card(
        ctx,
        sender,
        "Placing the demo call to your demo contact. It never calls 911.",
        cards.progress_card(workflow_id, "call"),
    )


async def _check_call(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    workflow_id = str(state["workflow_id"])
    if state["status"] != "called":
        await _notice(
            ctx,
            sender,
            "Not available",
            "There is no placed call to check for this request right now.",
            buttons=[_status_button(workflow_id), ("Menu", {"action": "start_over"})],
        )
        return
    await _side_task(
        ctx, state, "call_status", {"phase": "status", "call": dict(state["data"]["call"])}
    )
    await _send_card(
        ctx, sender, "Checking the demo call.", cards.progress_card(workflow_id, "call_status")
    )


async def _call_result(ctx: Context, state: dict[str, Any], result: TaskResult) -> None:
    workflow_id, requester = str(state["workflow_id"]), str(state["requester"])
    if state["stage"] == "call":
        if not result.ok:
            error = f"{result.error or 'The demo call could not be placed'}. It was not retried"
            state = await store.patch_workflow(workflow_id, {"status": "failed", "error": error})
            await _audit(ctx, state, "demo_call_failed")
            await _send_card(
                ctx, requester, f"{error}.", cards.error_card(error, service="emergency")
            )
            return
        data = {**state["data"], "call": dict(result.payload.get("call") or {})}
        state = await store.patch_workflow(
            workflow_id, {"status": "called", "stage": "complete", "data": data}
        )
        await _audit(ctx, state, "demo_call_placed")
        await _show_call(
            ctx, requester, state, "Calling your demo contact now. Put that phone on speaker."
        )
        return
    note = "Here is the demo call."
    data = dict(state["data"])
    if result.ok:
        data["call_status"] = dict(result.payload.get("call_status") or {})
    else:
        note = f"{result.error or 'The call could not be checked'}. Try again shortly."
    state = await store.patch_workflow(
        workflow_id, {"status": "called", "stage": "complete", "data": data}
    )
    await _show_call(ctx, requester, state, note)


# ---------------------------------------------------------------------------
# The paid hall match: one payment request per offer, checked by the Orchestrator itself.
# ---------------------------------------------------------------------------


def _wallet() -> str:
    """The Orchestrator's own Fetch wallet, derived from its seed: the only FET recipient."""
    return str(agent.wallet.address())


async def _show_match_offer(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    prices = PAYMENTS.price_labels()
    if dict(state["data"].get("payment") or {}).get("status") == "requested":
        await _send_card(
            ctx,
            sender,
            "Your payment request is open. Pay in ASI:One's payment panel, or take the free facts.",
            cards.housing_payment_card(str(state["workflow_id"]), prices),
        )
        return
    halls = payments.halls_to_match(state["data"])
    await _send_card(
        ctx,
        sender,
        f"{halls} hall{'s' if halls != 1 else ''} at {_school_name(state)} re-checked. The facts "
        f"are free; the hall-by-hall match is {' or '.join(prices)}. Test mode only.",
        cards.housing_unlock_card(str(state["workflow_id"]), state["data"], prices),
    )


async def _request_match_payment(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    """Claim one payment request per offer; a double click never opens two checkouts."""
    workflow_id = str(state["workflow_id"])
    attempt = int(state.get("payment_attempt", 0))
    if (
        state.get("service") != "housing"
        or state["status"] != "awaiting_payment"
        or not PAYMENTS.enabled
    ):
        await _notice(
            ctx,
            sender,
            "Not available",
            "There is no hall match waiting for payment on this request.",
            buttons=[_status_button(workflow_id), ("Menu", {"action": "start_over"})],
        )
        return
    if not await store.claim("payment_request", f"{workflow_id}:{attempt}"):
        await _show_match_offer(ctx, sender, state)
        return
    checkout: dict[str, str] | None = None
    if PAYMENTS.stripe_enabled:
        try:
            checkout = await payments.create_checkout(
                _http(), PAYMENTS, workflow_id=workflow_id, school=_school_name(state)
            )
        except ProviderError as exc:
            if not PAYMENTS.accept_fet:
                await store.patch_workflow(workflow_id, {"payment_attempt": attempt + 1})
                await _notice(
                    ctx,
                    sender,
                    "Checkout not started",
                    f"{exc}. Nothing was charged.",
                    badge="Try again",
                    variant="warning",
                    buttons=[
                        ("Try again", {"action": "unlock_housing", "workflow_id": workflow_id}),
                        (
                            "Free facts only",
                            {"action": "show_housing_facts", "workflow_id": workflow_id},
                        ),
                    ],
                )
                return
    request = payments.payment_request(
        PAYMENTS,
        workflow_id=workflow_id,
        wallet=_wallet(),
        school=_school_name(state),
        checkout=checkout,
    )
    data = {
        **state["data"],
        "payment": {
            "status": "requested",
            "attempt": attempt,
            "checkout_session_id": checkout["checkout_session_id"] if checkout else "",
            "methods": [funds.payment_method for funds in request.accepted_funds],
            "requested_at": _now(),
        },
    }
    state = await store.patch_workflow(workflow_id, {"data": data})
    await _audit(ctx, state, "payment_requested", methods=data["payment"]["methods"])
    await ctx.send(sender, request)
    await _send_card(
        ctx,
        sender,
        "Payment requested. Pay in ASI:One's payment panel; ResourcePilot checks the payment "
        "itself before the Planner starts.",
        cards.housing_payment_card(workflow_id, PAYMENTS.price_labels()),
    )


async def _payment_workflow(ctx: Context, sender: str, reference: str | None) -> dict | None:
    """The sender's workflow that is waiting for this payment, by reference or by session."""
    state = (
        await store.get_workflow(reference)
        if reference
        else await store.find_workflow(sender, _session(ctx))
    )
    if (
        state is None
        or state.get("requester") != sender
        or state.get("service") != "housing"
        or state.get("status") != "awaiting_payment"
        or dict(state["data"].get("payment") or {}).get("status") != "requested"
    ):
        return None
    return state


async def _check_commit(state: dict[str, Any], message: CommitPayment) -> str | None:
    """Check the payment with Stripe or the testnet itself. Returns the problem, or None."""
    method = message.funds.payment_method
    pending = dict(state["data"]["payment"])
    if method not in pending.get("methods", []):
        return "That payment method was not offered for this request"
    if method == "stripe":
        if message.transaction_id != pending.get("checkout_session_id"):
            return "That checkout was not created for this request"
        return await payments.verify_stripe(
            _http(),
            PAYMENTS,
            session_id=message.transaction_id,
            workflow_id=str(state["workflow_id"]),
        )
    if message.funds.currency.upper() != "FET":
        return "Only testnet FET is accepted"
    return await payments.verify_fet(
        PAYMENTS,
        tx_hash=message.transaction_id,
        buyer=payments.buyer_wallet(message.metadata),
        wallet=_wallet(),
    )


async def _decline_payment(
    ctx: Context, sender: str, state: dict[str, Any], transaction_id: str, reason: str
) -> None:
    """Cancel this commit and offer a fresh payment request; nothing unlocks."""
    workflow_id = str(state["workflow_id"])
    data = {**state["data"], "payment": {"status": "declined", "reason": reason}}
    attempt = int(state.get("payment_attempt", 0)) + 1
    state = await store.patch_workflow(workflow_id, {"data": data, "payment_attempt": attempt})
    await _audit(ctx, state, "payment_declined", reason=reason)
    await ctx.send(sender, CancelPayment(transaction_id=transaction_id, reason=reason))
    await _notice(
        ctx,
        sender,
        "Payment not confirmed",
        f"{reason}. The hall match is still locked, and the facts are free.",
        badge="Not confirmed",
        variant="warning",
        buttons=[
            ("Try again", {"action": "unlock_housing", "workflow_id": workflow_id}),
            ("Free facts only", {"action": "show_housing_facts", "workflow_id": workflow_id}),
        ],
    )


@payment_proto.on_message(model=CommitPayment)
async def handle_commit_payment(ctx: Context, sender: str, message: CommitPayment) -> None:
    if ROLE.key != "orchestrator":
        return
    transaction_id = str(message.transaction_id or "")[:300]
    state = await _payment_workflow(ctx, sender, message.reference)
    if state is None:
        await ctx.send(
            sender,
            CancelPayment(
                transaction_id=transaction_id,
                reason="No hall match is waiting for this payment",
            ),
        )
        return
    try:
        problem = await _check_commit(state, message)
    except ProviderError as exc:
        problem = f"{exc}; ResourcePilot could not confirm the payment"
    if problem is None and not await store.claim(
        "payment_tx", f"{message.funds.payment_method}:{transaction_id.lower()}"
    ):
        problem = "That payment was already used"
    if problem is not None:
        await _decline_payment(ctx, sender, state, transaction_id, problem)
        return
    workflow_id = str(state["workflow_id"])
    if not await store.claim("payment_unlock", workflow_id):
        return
    await ctx.send(sender, CompletePayment(transaction_id=transaction_id))
    method = message.funds.payment_method
    receipt = {
        "status": "paid",
        "method": method,
        "amount": f"{PAYMENTS.price_fet} testnet FET"
        if method == "fet_direct"
        else f"${PAYMENTS.price_cents / 100:.2f} (Stripe test mode)",
        "transaction": transaction_id[:12],
        "paid_at": _now(),
    }
    data = {**state["data"], "payment": receipt}
    task, state = await advance_to(
        store,
        workflow_id=workflow_id,
        requester=sender,
        session_id=str(state["session_id"]),
        next_role="planner",
        payload=data,
    )
    await _audit(ctx, state, "payment_completed", method=method)
    await _dispatch(ctx, task)
    await _send_card(
        ctx,
        sender,
        "Payment confirmed. The Planner is matching your halls now.",
        cards.progress_card(workflow_id, "planner", _chain(state)),
    )


@payment_proto.on_message(model=RejectPayment)
async def handle_reject_payment(ctx: Context, sender: str, message: RejectPayment) -> None:
    if ROLE.key != "orchestrator":
        return
    state = await _payment_workflow(ctx, sender, None)
    if state is None:
        return
    workflow_id = str(state["workflow_id"])
    data = {**state["data"], "payment": {"status": "declined", "reason": "Declined"}}
    attempt = int(state.get("payment_attempt", 0)) + 1
    state = await store.patch_workflow(workflow_id, {"data": data, "payment_attempt": attempt})
    await _audit(ctx, state, "payment_rejected")
    await _send_card(
        ctx,
        sender,
        "No payment was taken. Here are the free housing facts and official links.",
        cards.housing_card(state["data"]),
    )


# ---------------------------------------------------------------------------
# Specialists
# ---------------------------------------------------------------------------


@task_proto.on_message(model=TaskRequest)
async def handle_task(ctx: Context, sender: str, task: TaskRequest) -> None:
    if sender != ADDRESSES["orchestrator"]:
        ctx.logger.warning("Ignoring task from non-orchestrator %s", sender)
        return
    if ROLE.key in LIVE_ROLES:
        await ctx.send(sender, await _live_task(task))
        return
    service = task.payload.get("brief", {}).get("service")
    if ROLE.key == "verification" and service == "housing":
        await ctx.send(sender, await _verify_housing(task))
        return
    if ROLE.key == "verification" and service == "wellness":
        await ctx.send(sender, await _verify_wellness(task))
        return
    if ROLE.key not in SPECIALISTS:
        return
    await ctx.send(sender, execute_specialist(ROLE.key, task))


async def _live_task(task: TaskRequest) -> TaskResult:
    """Campus Researcher or Campus Rooms: bounded reads of the school's public pages."""
    if task.target_role != ROLE.key or utc_now() > parse(task.deadline_at):
        return task_result(task, ROLE.key, ok=False, error="Task timed out; retrying")
    try:
        if ROLE.key == "researcher":
            payload = await research(dict(task.payload), _http(), now=utc_now())
        elif ROLE.key == "emergency":
            payload = await emergency_task(_emergency_payload(task), _http(), store)
        else:
            payload = await campus_rooms_handler(dict(task.payload), _http())
    except (ProviderError, KeyError, ValueError) as exc:
        return task_result(task, ROLE.key, ok=False, error=str(exc))
    return task_result(task, ROLE.key, ok=True, payload=payload)


def _emergency_payload(task: TaskRequest) -> dict[str, Any]:
    """The first task carries the submitted form; later steps name their own phase."""
    payload = dict(task.payload)
    if payload.get("phase"):
        return payload
    return {"phase": "preview", "brief": dict(payload.get("fields") or {})}


async def _verify_housing(task: TaskRequest) -> TaskResult:
    """Verification's housing job: independently re-open every official link and hall page."""
    payload = dict(task.payload)
    housing = dict(payload.get("housing") or {})
    domain = str(payload.get("school", {}).get("domain") or "")
    dropped = []
    for key in ("office_url", "portal_url"):
        url = str(housing.get(key) or "")
        if url and not await recheck_link(_http(), url, domain):
            dropped.append(key.removesuffix("_url"))
            housing[key] = ""
    housing["unverified"] = dropped
    halls, gone = await recheck_halls(_http(), list(housing.get("options") or []), domain)
    housing["options"], housing["halls_dropped"] = halls, gone
    return task_result(task, "verification", ok=True, payload={**payload, "housing": housing})


async def _verify_wellness(task: TaskRequest) -> TaskResult:
    """Vet Campus Rooms' wellness offers, if any, then re-fetch the events feed independently."""
    vetted = execute_specialist("verification", task)
    if not vetted.ok:
        return vetted
    payload = dict(vetted.payload)
    school = {str(k): str(v) for k, v in dict(payload.get("school") or {}).items()}
    wellness = dict(payload.get("wellness") or {})
    if school.get("domain"):
        wellness = await recheck_events(_http(), wellness, school)
    return task_result(task, "verification", ok=True, payload={**payload, "wellness": wellness})


@task_proto.on_message(model=TaskResult)
async def handle_task_result(ctx: Context, sender: str, result: TaskResult) -> None:
    if ROLE.key != "orchestrator" or sender != ADDRESSES.get(result.source_role):
        return
    current = await store.get_workflow(result.workflow_id)
    if current is not None and current.get("stage") in SIDE_STAGES:
        side_state = await accept_side_result(store, result)
        if side_state is None:
            return
        if side_state["stage"] == "recheck":
            await _recheck_result(ctx, side_state, result)
        else:
            await _call_result(ctx, side_state, result)
        return
    try:
        next_task, state = await accept_result(store, result, paid_match=PAYMENTS.enabled)
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
    service = str(state.get("service") or "rooms")
    if next_task:
        await _dispatch(ctx, next_task)
        if result.source_role == "researcher":
            school = str(result.payload.get("school", {}).get("school_name") or "your school")
            found = result.payload.get("rooms", {})
            detail = (
                f"Found {school}'s {found['system_label']} room system."
                if found.get("status") == "found"
                else f"Matched {school}."
            )
            await _send_card(
                ctx,
                result.requester,
                f"{detail} {ROLE_BY_KEY[next_task.target_role].name} is next.",
                cards.progress_card(next_task.workflow_id, next_task.target_role, _chain(state)),
            )
    elif state["status"] == "awaiting_link":
        await _ask_for_link(ctx, result.requester, state)
    elif state["status"] == "answered":
        await _show_answer(ctx, result.requester, state)
    elif state["status"] == "awaiting_selection":
        await _show_rooms(ctx, result.requester, state)
    elif state["status"] == "awaiting_call":
        await _show_call_review(ctx, result.requester, state)
    elif state["status"] == "awaiting_payment":
        await _show_match_offer(ctx, result.requester, state)
    elif state["status"] == "failed":
        error = str(state.get("error", "Something went wrong"))
        if error.startswith("Missing"):
            await _send_card(
                ctx,
                result.requester,
                f"I need a bit more detail. {error}.",
                cards.service_form(service),
            )
        else:
            await _send_card(ctx, result.requester, error, cards.error_card(error, service=service))


# ---------------------------------------------------------------------------
# Timeouts: retry a silent specialist once, then fail with a card.
# ---------------------------------------------------------------------------


async def _recover_overdue(ctx: Context, state: dict[str, Any]) -> None:
    workflow_id, stage = str(state["workflow_id"]), str(state["stage"])
    if not await store.claim("timeout", f"{workflow_id}:{state['expected_request_id']}"):
        return
    requester = str(state["requester"])
    if stage == "recheck":
        # The room was never handed over; go back to the list so the student can try again.
        state = await store.patch_workflow(
            workflow_id, {"status": "awaiting_selection", "stage": "selection"}
        )
        await _audit(ctx, state, "recheck_timeout")
        await _show_rooms(
            ctx,
            requester,
            state,
            note=f"The {ROLE_BY_KEY['campus_rooms'].name} did not answer in time. "
            "Pick a room to try again.",
        )
        return
    if stage == "call":
        # Never retried: a lost answer may mean the phone is already ringing.
        error = (
            "The Emergency Dispatcher did not confirm the demo call in time. Calls are never "
            "retried; start a new demo to try again"
        )
        state = await store.patch_workflow(workflow_id, {"status": "failed", "error": error})
        await _audit(ctx, state, "demo_call_timeout")
        await _send_card(ctx, requester, f"{error}.", cards.error_card(error, service="emergency"))
        return
    if stage == "call_status":
        state = await store.patch_workflow(workflow_id, {"status": "called", "stage": "complete"})
        await _show_call(ctx, requester, state, "The call check timed out. Try again shortly.")
        return
    if stage not in SPECIALISTS:
        return
    name = ROLE_BY_KEY[stage].name
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
    await _send_card(
        ctx,
        requester,
        f"{error}.",
        cards.error_card(error, service=str(state.get("service") or "")),
    )


@agent.on_interval(period=2.0)
async def sweep(ctx: Context) -> None:
    if not READY or ROLE.key != "orchestrator":
        return
    for overdue in await store.list_overdue_workflows(_now()):
        await _recover_overdue(ctx, overdue)


# ---------------------------------------------------------------------------
# Memory and Audit
# ---------------------------------------------------------------------------


@task_proto.on_message(model=AuditEvent)
async def handle_audit(ctx: Context, sender: str, event: AuditEvent) -> None:
    if ROLE.key != "memory_audit" or sender not in ADDRESSES.values():
        return
    await store.append_event(
        event.workflow_id, event.event_id, event.event_type, dict(event.details)
    )


@chat_proto.on_message(model=ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, message: ChatAcknowledgement) -> None:
    return None


agent.include(chat_proto, publish_manifest=True)
agent.include(task_proto, publish_manifest=True)
if ROLE.key == "orchestrator":
    agent.include(payment_proto, publish_manifest=True)


if __name__ == "__main__":
    agent.run()
