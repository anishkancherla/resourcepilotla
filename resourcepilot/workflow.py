"""Workflow sequencing shared by the Orchestrator and the specialist agents.

The Orchestrator owns workflow state. Each specialist receives a correlated TaskRequest, does
one job, and returns a TaskResult that echoes the correlation fields. A late or duplicate result
can never advance a newer stage.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from resourcepilot.clock import later, parse, utc_now
from resourcepilot.config import SPECIALIST_CHAIN, env_int
from resourcepilot.intake import intake_handler
from resourcepilot.logistics import analysis_handler
from resourcepilot.models import TaskRequest, TaskResult
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
    "analysis": analysis_handler,
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
    task = make_task(
        workflow_id=workflow_id,
        requester=requester,
        session_id=session_id,
        target_role="intake",
        payload=request,
    )
    await store.create_workflow(
        workflow_id,
        {
            "workflow_id": workflow_id,
            "requester": requester,
            "session_id": session_id,
            "stage": "intake",
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
    index = SPECIALIST_CHAIN.index(result.source_role)
    if index == len(SPECIALIST_CHAIN) - 1:
        return None, await store.patch_workflow(
            result.workflow_id,
            {"status": "awaiting_selection", "stage": "selection", "data": result.payload},
        )
    next_role = SPECIALIST_CHAIN[index + 1]
    task = make_task(
        workflow_id=result.workflow_id,
        requester=result.requester,
        session_id=result.session_id,
        target_role=next_role,
        payload=result.payload,
    )
    state = await store.patch_workflow(
        result.workflow_id,
        {
            "stage": next_role,
            "status": "running",
            "expected_request_id": task.request_id,
            "deadline_at": task.deadline_at,
            "data": result.payload,
        },
    )
    return task, state


def is_timed_out(state: dict[str, Any], *, now: datetime | None = None) -> bool:
    if state.get("status") != "running":
        return False
    value = state.get("deadline_at")
    return bool(value and (now or utc_now()) > parse(str(value)))
