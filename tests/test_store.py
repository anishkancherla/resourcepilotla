"""The durable store contract, run against MemoryStore and (with TEST_DATABASE_URL) Postgres."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from resourcepilot import store as store_module
from resourcepilot.store import DurableStore, MemoryStore, PostgresStore, build_store

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
CONTRACT = {
    "setup",
    "close",
    "claim",
    "create_workflow",
    "get_workflow",
    "find_workflow",
    "patch_workflow",
    "list_overdue_workflows",
    "recent_bookings",
    "get_profile",
    "save_profile",
    "append_event",
    "list_events",
    "cache_get",
    "cache_put",
}


def iso(hours: float = 0, minutes: float = 0) -> str:
    return (NOW + timedelta(hours=hours, minutes=minutes)).isoformat()


def workflow(workflow_id: str, requester: str, **state: str) -> dict[str, str]:
    return {
        "workflow_id": workflow_id,
        "requester": requester,
        "session_id": "s1",
        "status": "running",
        "deadline_at": iso(minutes=5),
        **state,
    }


def public_methods(cls: type) -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def test_every_store_implements_exactly_the_contract() -> None:
    assert public_methods(DurableStore) == CONTRACT
    assert public_methods(MemoryStore) == CONTRACT
    assert public_methods(PostgresStore) == CONTRACT
    assert MemoryStore.production_ready is False
    assert PostgresStore.production_ready is True


def test_postgres_creates_only_the_tables_still_used() -> None:
    tables = {
        line.split()[5]
        for line in store_module.SCHEMA.splitlines()
        if line.startswith("CREATE TABLE IF NOT EXISTS")
    }
    assert tables == {
        "idempotency_keys",
        "workflows",
        "workflow_events",
        "api_cache",
        "student_profiles",
    }


def test_build_store_uses_postgres_only_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert isinstance(build_store(), MemoryStore)
    monkeypatch.setenv("DATABASE_URL", "   ")
    assert isinstance(build_store(), MemoryStore)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user@127.0.0.1:1/never-opened")
    built = build_store()
    assert isinstance(built, PostgresStore)
    assert built.pool is None  # nothing connects before setup()


async def test_claims_are_one_time(any_store: DurableStore, unique: str) -> None:
    assert await any_store.claim("chat", unique)
    assert not await any_store.claim("chat", unique)
    assert await any_store.claim("result", unique)


async def test_workflow_create_get_and_patch(any_store: DurableStore, unique: str) -> None:
    workflow_id = f"wf-{unique}"
    assert await any_store.get_workflow(workflow_id) is None
    await any_store.create_workflow(workflow_id, workflow(workflow_id, f"agent1q{unique}"))
    with pytest.raises((ValueError, asyncpg.UniqueViolationError)):
        await any_store.create_workflow(workflow_id, workflow(workflow_id, "agent1qother"))
    state = await any_store.patch_workflow(
        workflow_id, {"stage": "researcher", "data": {"brief": {"service": "rooms"}}}
    )
    assert state["stage"] == "researcher"
    assert state["requester"] == f"agent1q{unique}"
    stored = await any_store.get_workflow(workflow_id)
    assert stored == state
    # A patch replaces top-level keys; it never merges into nested ones.
    state = await any_store.patch_workflow(workflow_id, {"data": {"offers": []}})
    assert state["data"] == {"offers": []}
    with pytest.raises(KeyError):
        await any_store.patch_workflow(f"missing-{unique}", {"stage": "planner"})


async def test_find_workflow_returns_the_latest_for_the_session(
    any_store: DurableStore, unique: str
) -> None:
    requester = f"agent1q{unique}"
    assert await any_store.find_workflow(requester, "s1") is None
    await any_store.create_workflow(f"wf1-{unique}", workflow(f"wf1-{unique}", requester))
    await any_store.create_workflow(f"wf2-{unique}", workflow(f"wf2-{unique}", requester))
    await any_store.create_workflow(
        f"wf3-{unique}", workflow(f"wf3-{unique}", requester, session_id="s2")
    )
    found = await any_store.find_workflow(requester, "s1")
    assert found and found["workflow_id"] == f"wf2-{unique}"
    assert await any_store.find_workflow(f"agent1qother{unique}", "s1") is None


async def test_overdue_workflows_filter_by_status_and_deadline(
    any_store: DurableStore, unique: str
) -> None:
    late, early = f"late-{unique}", f"early-{unique}"
    requester = f"agent1q{unique}"
    await any_store.create_workflow(late, workflow(late, requester, deadline_at=iso(minutes=-1)))
    await any_store.create_workflow(early, workflow(early, requester))

    def ids(states: list[dict]) -> set[str]:
        return {state["workflow_id"] for state in states} & {late, early}

    assert ids(await any_store.list_overdue_workflows(iso())) == {late}
    assert ids(await any_store.list_overdue_workflows(iso(minutes=10))) == {late, early}
    await any_store.patch_workflow(late, {"status": "awaiting_link"})
    assert ids(await any_store.list_overdue_workflows(iso())) == set()
    assert ids(await any_store.list_overdue_workflows(iso(), status="awaiting_link")) == {late}
    await any_store.patch_workflow(late, {"status": "booked"})
    assert ids(await any_store.list_overdue_workflows(iso(), status="awaiting_link")) == set()


async def test_recent_bookings_are_the_requesters_booked_workflows_newest_first(
    any_store: DurableStore, unique: str
) -> None:
    requester = f"agent1q{unique}"
    first, second, running = f"a-{unique}", f"b-{unique}", f"c-{unique}"
    other = f"d-{unique}"
    for workflow_id in (first, second, running):
        await any_store.create_workflow(workflow_id, workflow(workflow_id, requester))
    await any_store.create_workflow(other, workflow(other, f"agent1qother{unique}"))
    assert await any_store.recent_bookings(requester, 5) == []

    option = {"option_id": "opt_1", "label": "Earliest opening", "lines": []}
    await any_store.patch_workflow(first, {"status": "booked", "option": option})
    await any_store.patch_workflow(second, {"status": "booked", "option": option})
    await any_store.patch_workflow(other, {"status": "booked"})
    await any_store.patch_workflow(running, {"stage": "planner"})

    booked = await any_store.recent_bookings(requester, 5)
    assert [state["workflow_id"] for state in booked] == [second, first]
    assert booked[0]["option"] == option
    assert [state["workflow_id"] for state in await any_store.recent_bookings(requester, 1)] == [
        second
    ]


async def test_profiles_keep_one_school_per_requester(any_store: DurableStore, unique: str) -> None:
    requester = f"agent-{unique}"
    assert await any_store.get_profile(requester) is None
    await any_store.save_profile(requester, {"school_domain": "purdue.edu", "school_name": ""})
    await any_store.save_profile(
        requester, {"school_domain": "mit.edu", "school_name": "Massachusetts Institute"}
    )
    assert await any_store.get_profile(requester) == {
        "school_domain": "mit.edu",
        "school_name": "Massachusetts Institute",
    }
    assert await any_store.get_profile(f"other-{unique}") is None


async def test_events_are_append_only_and_ordered(any_store: DurableStore, unique: str) -> None:
    workflow_id = f"wf-{unique}"
    assert await any_store.list_events(workflow_id) == []
    assert await any_store.append_event(workflow_id, f"e1-{unique}", "started", {"a": 1})
    assert not await any_store.append_event(workflow_id, f"e1-{unique}", "retried", {"a": 2})
    assert await any_store.append_event(workflow_id, f"e2-{unique}", "booked", {"b": [1, 2]})
    assert await any_store.list_events(workflow_id) == [
        {"event_id": f"e1-{unique}", "event_type": "started", "details": {"a": 1}},
        {"event_id": f"e2-{unique}", "event_type": "booked", "details": {"b": [1, 2]}},
    ]
    assert await any_store.list_events(f"other-{unique}") == []


async def test_api_cache_round_trips_and_expires(any_store: DurableStore, unique: str) -> None:
    key = f"test:{unique}"
    assert await any_store.cache_get(key) is None
    value = {"purdue.edu": {"unitid": 243780, "name": "Purdue"}, "rows": [1, 2]}
    await any_store.cache_put(key, value, 60)
    assert await any_store.cache_get(key) == value
    await any_store.cache_put(key, {"replaced": True}, 60)
    assert await any_store.cache_get(key) == {"replaced": True}
    await any_store.cache_put(f"{key}:list", [{"item_id": "booking-page"}], 60)
    assert await any_store.cache_get(f"{key}:list") == [{"item_id": "booking-page"}]
    await any_store.cache_put(key, value, 0)
    assert await any_store.cache_get(key) is None


async def test_memory_store_hands_out_copies(unique: str) -> None:
    memory = MemoryStore()
    state = workflow(f"wf-{unique}", f"agent1q{unique}")
    await memory.create_workflow(state["workflow_id"], state)
    state["status"] = "booked"
    fetched = await memory.get_workflow(state["workflow_id"])
    assert fetched and fetched["status"] == "running"
    fetched["status"] = "booked"
    assert (await memory.get_workflow(state["workflow_id"]) or {})["status"] == "running"
    cached = {"rows": [1]}
    await memory.cache_put("k", cached, 60)
    cached["rows"].append(2)
    assert await memory.cache_get("k") == {"rows": [1]}
