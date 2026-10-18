"""Runtime for one ResourcePilot agent, selected by AGENT_ROLE.

Seven agents, each its own process, seed, address, and profile. The Orchestrator owns the ASI:One
chat and the workflow, and delegates every step with ``ctx.send``; specialists reply with
correlated TaskResults. Study rooms: Intake, then the Campus Researcher (finds the school's room
system), Campus Rooms (reads open rooms live), Verification, and the Planner. Before handing over a
booking link, Campus Rooms re-checks that room live; a room that was just taken sends the Planner
back to the other open rooms. Health and wellness runs the same chain for wellness rooms, plus
the Researcher's events feed, which Verification re-fetches; a school with no room system skips
Campus Rooms and the Planner. Housing: Intake, the Campus Researcher, then Verification, which
re-opens every official link. ResourcePilot never reserves a room and takes no payments.
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

from resourcepilot import cards
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
from resourcepilot.http import Http, ProviderError, build_http
from resourcepilot.intake import scrub_fields
from resourcepilot.models import AuditEvent, HealthResponse, TaskRequest, TaskResult
from resourcepilot.research import recheck_events, recheck_link, research
from resourcepilot.rooms import campus_rooms_handler
from resourcepilot.store import store
from resourcepilot.workflow import (
    accept_recheck,
    accept_result,
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
READY = False
HTTP: Http | None = None
# Network specialists: they read the public web, so they run outside the synchronous HANDLERS.
LIVE_ROLES = frozenset({"researcher", "campus_rooms"})


def _http() -> Http:
    """Live public sources, used only by the Campus Researcher, Campus Rooms, and Verification."""
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
            cards.status_card(ROLE.name, ROLE.responsibility, ADDRESSES["orchestrator"]),
        )
        return
    await _orchestrator_chat(ctx, sender, text, cards.parse_selection(text))


FORM_PROMPTS = {
    "rooms": "Enter your school email and when you need a room. Only the domain is kept.",
    "wellness": "Enter your school email and when you'd like a quiet room. Only the domain is "
    "kept. In crisis? Call or text 988.",
    "housing": "Enter your school email. Only the domain is kept.",
}
FIELD_LIMIT = 200


def _form_fields(selection: dict[str, Any]) -> dict[str, Any]:
    """Bounded scalar fields from a form submission, with any email reduced to its domain."""
    fields = {
        str(key)[:40]: str(value)[:FIELD_LIMIT] if isinstance(value, str) else value
        for key, value in selection.items()
        if key not in {"action", "selection", "service"}
        and isinstance(value, str | int | float | bool)
    }
    return scrub_fields(fields)


async def _start_workflow(ctx: Context, sender: str, service: str, fields: dict[str, Any]) -> None:
    request = {"service": service, "fields": fields}
    task = await begin_workflow(store, requester=sender, session_id=_session(ctx), request=request)
    state = await store.get_workflow(task.workflow_id)
    assert state is not None
    await _audit(ctx, state, "workflow_started", stage="intake", service=service)
    await _dispatch(ctx, task)
    chain = service_chain(service)
    names = ", ".join(ROLE_BY_KEY[role].name.removeprefix("ResourcePilot ") for role in chain)
    await _send_card(
        ctx,
        sender,
        f"Got it. {names} are on it.",
        cards.progress_card(task.workflow_id, "intake", chain),
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
    if action == "show_results" and state["status"] == "awaiting_selection":
        await _show_rooms(ctx, sender, state)
        return
    option = _option(state, str(selection.get("option_id", "")))
    if action == "view_room" and option:
        await _send_card(
            ctx,
            sender,
            f"{option['lines'][0]['name']}: free, re-checked live before you book.",
            cards.room_detail(workflow_id, option, wellness=_wellness(state)),
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


async def _show_answer(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    """A finished lookup with nothing to pick: housing, or wellness without a room system."""
    if _wellness(state):
        await _show_wellness(ctx, sender, state)
        return
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
        else:
            payload = await campus_rooms_handler(dict(task.payload), _http())
    except (ProviderError, KeyError, ValueError) as exc:
        return task_result(task, ROLE.key, ok=False, error=str(exc))
    return task_result(task, ROLE.key, ok=True, payload=payload)


async def _verify_housing(task: TaskRequest) -> TaskResult:
    """Verification's housing job: independently re-open every official link."""
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
    if current is not None and current.get("stage") == "recheck":
        recheck_state = await accept_recheck(store, result)
        if recheck_state is not None:
            await _recheck_result(ctx, recheck_state, result)
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


if __name__ == "__main__":
    agent.run()
