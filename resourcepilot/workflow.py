"""Workflow sequencing shared by the Orchestrator and the specialist agents.

The Orchestrator owns workflow state. Each specialist receives a correlated TaskRequest, does
one job, and returns a TaskResult that echoes the correlation fields. A late or duplicate result
can never advance a newer stage. Each campus service has its own chain of specialists
(``SERVICE_CHAINS``). The Campus Researcher and Campus Rooms read the public web, so they run
asynchronously in the agent runtime rather than through ``HANDLERS``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from resourcepilot.clock import later, parse, utc_now
from resourcepilot.config import env_int, service_chain
from resourcepilot.intake import intake_handler
from resourcepilot.models import TaskRequest, TaskResult
from resourcepilot.planning import planner_handler
from resourcepilot.store import DurableStore
from resourcepilot.vetting import verification_handler

Handler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def task_deadline() -> str:
    return later(env_int("TASK_TIMEOUT_SECONDS", 45, minimum=10))


def make_task(
    *,
    workflow_id: str,
    requester: str,
    session_id: str,
    target_role: str,
    payload: dict[str, Any],
) -> TaskRequest:
    return TaskRequest(
        workflow_id=workflow_id,
        request_id=str(uuid4()),
        requester=requester,
        session_id=session_id,
        target_role=target_role,
        payload=payload,
        deadline_at=task_deadline(),
    )


def task_result(task: TaskRequest, role: str, *, ok: bool, **fields: Any) -> TaskResult:
    return TaskResult(
        workflow_id=task.workflow_id,
        request_id=task.request_id,
        requester=task.requester,
        session_id=task.session_id,
        source_role=role,
        ok=ok,
        **fields,
    )


HANDLERS: dict[str, Handler] = {
    "intake": lambda payload, context: intake_handler(payload),
    "verification": verification_handler,
    "planner": planner_handler,
}


def execute_specialist(
    role: str, task: TaskRequest, context: dict[str, Any] | None = None
) -> TaskResult:
    if task.target_role != role:
        return task_result(task, role, ok=False, error=f"Task targeted {task.target_role}")
    if utc_now() > parse(task.deadline_at):
        return task_result(task, role, ok=False, error="Task timed out; the Orchestrator retries")
    try:
        output = HANDLERS[role](dict(task.payload), context or {})
    except (KeyError, TypeError, ValueError) as exc:
        return task_result(task, role, ok=False, error=str(exc))
    return task_result(task, role, ok=True, payload=output)


async def begin_workflow(
    store: DurableStore,
    *,
    requester: str,
    session_id: str,
    request: dict[str, Any],
) -> TaskRequest:
    workflow_id = str(uuid4())
    service = str(request.get("service") or "rooms")
    first = service_chain(service)[0]
    task = make_task(
        workflow_id=workflow_id,
        requester=requester,
        session_id=session_id,
        target_role=first,
        payload=request,
    )
    await store.create_workflow(
        workflow_id,
        {
            "workflow_id": workflow_id,
            "requester": requester,
            "session_id": session_id,
            "service": service,
            "stage": first,
            "status": "running",
            "expected_request_id": task.request_id,
            "deadline_at": task.deadline_at,
            "data": request,
        },
    )
    return task


async def accept_result(
    store: DurableStore, result: TaskResult
) -> tuple[TaskRequest | None, dict[str, Any]]:
    """Advance the planning pipeline by one correlated, first-seen specialist result."""
    if not await store.claim("task_result", result.request_id):
        state = await store.get_workflow(result.workflow_id)
        if state is None:
            raise KeyError(result.workflow_id)
        return None, state
    state = await store.get_workflow(result.workflow_id)
    if state is None:
        raise KeyError(result.workflow_id)
    if state["expected_request_id"] != result.request_id or state["stage"] != result.source_role:
        raise ValueError("Result correlation or source role did not match the active task")
    if not result.ok:
        return None, await store.patch_workflow(
            result.workflow_id,
            {"status": "failed", "error": result.error or "Specialist failed"},
        )
    service = str(state.get("service") or "rooms")
    if result.source_role == "researcher" and result.payload.get("needs_link"):
        # No booking system was found: pause until the student pastes the link.
        return None, await store.patch_workflow(
            result.workflow_id,
            {"status": "awaiting_link", "stage": "link", "data": result.payload},
        )
    chain = service_chain(service, result.payload)
    index = chain.index(result.source_role)
    if index == len(chain) - 1:
        # Housing and wellness without rooms are answers; a room search ends at the Planner with
        # options to re-check. The emergency preview waits for the student to press "Call now".
        if service == "emergency":
            done = {"status": "awaiting_call", "stage": "review"}
        elif service == "housing" or chain[-1] == "verification":
            done = {"status": "answered", "stage": "complete"}
        else:
            done = {"status": "awaiting_selection", "stage": "selection"}
        return None, await store.patch_workflow(
            result.workflow_id, {**done, "data": result.payload}
        )
    return await advance_to(
        store,
        workflow_id=result.workflow_id,
        requester=result.requester,
        session_id=result.session_id,
        next_role=chain[index + 1],
        payload=result.payload,
    )


async def advance_to(
    store: DurableStore,
    *,
    workflow_id: str,
    requester: str,
    session_id: str,
    next_role: str,
    payload: dict[str, Any],
) -> tuple[TaskRequest, dict[str, Any]]:
    """Hand the workflow to ``next_role`` with a fresh, correlated task."""
    task = make_task(
        workflow_id=workflow_id,
        requester=requester,
        session_id=session_id,
        target_role=next_role,
        payload=payload,
    )
    state = await store.patch_workflow(
        workflow_id,
        {
            "stage": next_role,
            "status": "running",
            "expected_request_id": task.request_id,
            "deadline_at": task.deadline_at,
            "data": payload,
        },
    )
    return task, state


# Steps outside a service's chain, and the one role allowed to answer each.
SIDE_STAGES: dict[str, str] = {
    "recheck": "campus_rooms",
    "call": "emergency",
    "call_status": "emergency",
}


async def accept_side_result(store: DurableStore, result: TaskResult) -> dict[str, Any] | None:
    """Accept the answer to an out-of-chain step once, and only for the active step.

    That is a live re-check from Campus Rooms, or a demo call or call-status read from the
    Emergency Dispatcher. Correlation is checked before the claim, so a forged or misrouted
    result can never use up the claim that the genuine answer needs.
    """
    state = await store.get_workflow(result.workflow_id)
    if (
        state is None
        or SIDE_STAGES.get(str(state["stage"])) != result.source_role
        or state["expected_request_id"] != result.request_id
    ):
        return None
    if not await store.claim("task_result", result.request_id):
        return None
    return state


async def accept_recheck(store: DurableStore, result: TaskResult) -> dict[str, Any] | None:
    """Accept Campus Rooms' answer to a live re-check once, and only for the active re-check."""
    state = await store.get_workflow(result.workflow_id)
    if state is None or state["stage"] != "recheck":
        return None
    return await accept_side_result(store, result)


def is_timed_out(state: dict[str, Any], *, now: datetime | None = None) -> bool:
    if state.get("status") != "running":
        return False
    value = state.get("deadline_at")
    return bool(value and (now or utc_now()) > parse(str(value)))
