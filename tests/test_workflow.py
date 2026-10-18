from __future__ import annotations

from datetime import timedelta

import pytest

from resourcepilot.cards import example_bundle_selection
from resourcepilot.clock import utc_now
from resourcepilot.models import TaskResult
from resourcepilot.store import MemoryStore
from resourcepilot.workflow import (
    accept_result,
    begin_workflow,
    execute_specialist,
    is_timed_out,
    make_task,
)

REQUEST = {"service": "bundle", "fields": example_bundle_selection()}


async def start(store: MemoryStore, request: dict | None = None):
    return await begin_workflow(
        store, requester="agent1qbuyer", session_id="session-1", request=request or REQUEST
    )


async def test_intake_result_advances_to_discovery() -> None:
    store = MemoryStore()
    task = await start(store)
    result = execute_specialist("intake", task)
    assert result.ok, result.error
    next_task, state = await accept_result(store, result)
    assert next_task is not None
    assert next_task.target_role == "discovery"
    assert next_task.workflow_id == task.workflow_id
    assert state["stage"] == "discovery"
    assert state["expected_request_id"] == next_task.request_id
    assert next_task.payload["brief"]["items"] == {"camera": 1, "podcast_room": 1, "posters": 20}


async def test_duplicate_result_is_ignored() -> None:
    store = MemoryStore()
    task = await start(store)
    result = execute_specialist("intake", task)
    next_task, state = await accept_result(store, result)
    duplicate_task, duplicate_state = await accept_result(store, result)
    assert next_task is not None
    assert duplicate_task is None
    assert duplicate_state == state


async def test_wrong_source_cannot_advance_workflow() -> None:
    store = MemoryStore()
    task = await start(store)
    forged = TaskResult(
        workflow_id=task.workflow_id,
        request_id=task.request_id,
        requester=task.requester,
        session_id=task.session_id,
        source_role="planner",
        ok=True,
        payload={},
    )
    with pytest.raises(ValueError, match="correlation"):
        await accept_result(store, forged)


async def test_specialist_failure_becomes_friendly_failed_state() -> None:
    store = MemoryStore()
    task = await start(store, {"service": "bundle", "fields": {"posters": "5"}})
    result = execute_specialist("intake", task)
    assert result.ok is False
    next_task, state = await accept_result(store, result)
    assert next_task is None
    assert state["status"] == "failed"
    assert "Missing" in state["error"]


def test_misrouted_and_expired_tasks_are_rejected() -> None:
    task = make_task(
        workflow_id="w", requester="u", session_id="s", target_role="intake", payload=REQUEST
    )
    assert "targeted" in (execute_specialist("planner", task).error or "")
    expired = task.copy(update={"deadline_at": (utc_now() - timedelta(seconds=1)).isoformat()})
    assert "timed out" in (execute_specialist("intake", expired).error or "")


def test_timeout_detection() -> None:
    past = (utc_now() - timedelta(seconds=1)).isoformat()
    assert is_timed_out({"status": "running", "deadline_at": past})
    assert not is_timed_out({"status": "awaiting_selection", "deadline_at": past})


def test_serialization_round_trip() -> None:
    result = TaskResult(
        workflow_id="w",
        request_id="r",
        requester="u",
        session_id="s",
        source_role="intake",
        ok=True,
        payload={"nested": [1, 2]},
    )
    assert TaskResult.parse_raw(result.json()) == result


def reply(task, payload: dict) -> TaskResult:
    return TaskResult(
        workflow_id=task.workflow_id,
        request_id=task.request_id,
        requester=task.requester,
        session_id=task.session_id,
        source_role=task.target_role,
        ok=True,
        payload=payload,
    )


async def test_housing_chain_ends_answered_after_verification() -> None:
    store = MemoryStore()
    task = await start(store, {"service": "housing", "fields": {"school_domain": "purdue.edu"}})
    intake = execute_specialist("intake", task)
    assert intake.ok, intake.error
    research, _ = await accept_result(store, intake)
    assert research is not None and research.target_role == "researcher"
    verify, _ = await accept_result(store, reply(research, {**research.payload, "housing": {}}))
    assert verify is not None and verify.target_role == "verification"
    done, state = await accept_result(store, reply(verify, verify.payload))
    assert done is None
    assert (state["status"], state["stage"]) == ("answered", "complete")


async def test_room_search_pauses_for_a_link_when_research_finds_no_system() -> None:
    store = MemoryStore()
    day = utc_now().date().isoformat()
    fields = {"school_domain": "mit.edu", "day": day, "window": "any", "duration": "60"}
    task = await start(store, {"service": "rooms", "fields": fields})
    intake = execute_specialist("intake", task)
    assert intake.ok, intake.error
    research, _ = await accept_result(store, intake)
    assert research is not None and research.target_role == "researcher"
    paused, state = await accept_result(
        store, reply(research, {**research.payload, "needs_link": True})
    )
    assert paused is None
    assert (state["status"], state["stage"]) == ("awaiting_link", "link")
