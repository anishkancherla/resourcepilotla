"""Durable shared state: workflows, one-time claims, the audit timeline, the live-data cache, and
each student's saved school.

Postgres is the shared durable path every agent process uses. ``MemoryStore`` keeps the same
contract in one process and is for tests and explicit local development only.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from copy import deepcopy
from datetime import datetime
from typing import Any, Protocol

import asyncpg


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


class DurableStore(Protocol):
    production_ready: bool

    async def setup(self) -> None: ...

    async def close(self) -> None: ...

    async def claim(self, scope: str, key: str) -> bool: ...

    # Workflows
    async def create_workflow(self, workflow_id: str, state: dict[str, Any]) -> None: ...

    async def get_workflow(self, workflow_id: str) -> dict[str, Any] | None: ...

    async def find_workflow(self, requester: str, session_id: str) -> dict[str, Any] | None: ...

    async def patch_workflow(self, workflow_id: str, patch: dict[str, Any]) -> dict[str, Any]: ...

    async def list_overdue_workflows(
        self, now: str, status: str = "running"
    ) -> list[dict[str, Any]]: ...

    async def recent_bookings(self, requester: str, limit: int) -> list[dict[str, Any]]: ...

    # The student's school, set once and reused by every service: a domain, never an email
    async def get_profile(self, requester: str) -> dict[str, Any] | None: ...

    async def save_profile(self, requester: str, profile: dict[str, Any]) -> None: ...

    # Append-only audit timeline
    async def append_event(
        self, workflow_id: str, event_id: str, event_type: str, details: dict[str, Any]
    ) -> bool: ...

    async def list_events(self, workflow_id: str) -> list[dict[str, Any]]: ...

    # Live-provider response cache (Campus Researcher and Campus Rooms)
    async def cache_get(self, key: str) -> Any | None: ...

    async def cache_put(self, key: str, value: Any, ttl_seconds: int) -> None: ...


class MemoryStore:
    """Single-process, non-production fallback used by tests and explicit local runs."""

    production_ready = False

    def __init__(self) -> None:
        self.workflows: dict[str, dict[str, Any]] = {}
        self.claims: set[tuple[str, str]] = set()
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.cache: dict[str, tuple[float, Any]] = {}
        self.profiles: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def claim(self, scope: str, key: str) -> bool:
        async with self._lock:
            marker = (scope, key)
            if marker in self.claims:
                return False
            self.claims.add(marker)
            return True

    async def create_workflow(self, workflow_id: str, state: dict[str, Any]) -> None:
        async with self._lock:
            if workflow_id in self.workflows:
                raise ValueError(f"workflow already exists: {workflow_id}")
            self.workflows[workflow_id] = deepcopy(state)

    async def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        value = self.workflows.get(workflow_id)
        return deepcopy(value) if value else None

    async def find_workflow(self, requester: str, session_id: str) -> dict[str, Any] | None:
        values = [
            value
            for value in self.workflows.values()
            if value.get("requester") == requester and value.get("session_id") == session_id
        ]
        return deepcopy(values[-1]) if values else None

    async def patch_workflow(self, workflow_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            state = self.workflows[workflow_id]
            state.update(deepcopy(patch))
            return deepcopy(state)

    async def list_overdue_workflows(
        self, now: str, status: str = "running"
    ) -> list[dict[str, Any]]:
        return [
            deepcopy(state)
            for state in self.workflows.values()
            if state.get("status") == status
            and state.get("deadline_at")
            and _parse(str(state["deadline_at"])) < _parse(now)
        ]

    async def recent_bookings(self, requester: str, limit: int) -> list[dict[str, Any]]:
        """This requester's booked workflow states, newest first."""
        values = [
            deepcopy(state)
            for state in self.workflows.values()
            if state.get("requester") == requester and state.get("status") == "booked"
        ]
        return list(reversed(values))[:limit]

    async def get_profile(self, requester: str) -> dict[str, Any] | None:
        value = self.profiles.get(requester)
        return deepcopy(value) if value else None

    async def save_profile(self, requester: str, profile: dict[str, Any]) -> None:
        self.profiles[requester] = deepcopy(profile)

    async def append_event(
        self, workflow_id: str, event_id: str, event_type: str, details: dict[str, Any]
    ) -> bool:
        async with self._lock:
            events = self.events.setdefault(workflow_id, [])
            if any(event["event_id"] == event_id for event in events):
                return False
            events.append(
                {"event_id": event_id, "event_type": event_type, "details": deepcopy(details)}
            )
            return True

    async def list_events(self, workflow_id: str) -> list[dict[str, Any]]:
        return deepcopy(self.events.get(workflow_id, []))

    async def cache_get(self, key: str) -> Any | None:
        entry = self.cache.get(key)
        if entry is None or entry[0] <= time.time():
            return None
        return deepcopy(entry[1])

    async def cache_put(self, key: str, value: Any, ttl_seconds: int) -> None:
        self.cache[key] = (time.time() + ttl_seconds, deepcopy(value))


# Tables from earlier versions that are no longer used are left untouched in existing volumes.
SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
  scope TEXT NOT NULL, key TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (scope, key)
);
CREATE TABLE IF NOT EXISTS workflows (
  workflow_id TEXT PRIMARY KEY, state JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS workflow_events (
  event_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, event_type TEXT NOT NULL,
  details JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS api_cache (
  key TEXT PRIMARY KEY, payload JSONB NOT NULL, expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS student_profiles (
  requester TEXT PRIMARY KEY, profile JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class PostgresStore:
    production_ready = True

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    def _pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("Postgres store is not initialized")
        return self.pool

    async def setup(self) -> None:
        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=5)
        async with self._pool().acquire() as connection:
            await connection.execute("SELECT pg_advisory_lock(749273042)")
            try:
                await connection.execute(SCHEMA)
            finally:
                await connection.execute("SELECT pg_advisory_unlock(749273042)")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def claim(self, scope: str, key: str) -> bool:
        result = await self._pool().execute(
            "INSERT INTO idempotency_keys(scope,key) VALUES($1,$2) ON CONFLICT DO NOTHING",
            scope,
            key,
        )
        return result.endswith("1")

    async def create_workflow(self, workflow_id: str, state: dict[str, Any]) -> None:
        await self._pool().execute(
            "INSERT INTO workflows(workflow_id,state) VALUES($1,$2::jsonb)",
            workflow_id,
            json.dumps(state),
        )

    async def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        value = await self._pool().fetchval(
            "SELECT state FROM workflows WHERE workflow_id=$1", workflow_id
        )
        return json.loads(value) if value else None

    async def find_workflow(self, requester: str, session_id: str) -> dict[str, Any] | None:
        value = await self._pool().fetchval(
            """SELECT state FROM workflows
               WHERE state->>'requester'=$1 AND state->>'session_id'=$2
               ORDER BY updated_at DESC LIMIT 1""",
            requester,
            session_id,
        )
        return json.loads(value) if value else None

    async def patch_workflow(self, workflow_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        value = await self._pool().fetchval(
            """UPDATE workflows SET state=state || $2::jsonb, updated_at=now()
               WHERE workflow_id=$1 RETURNING state""",
            workflow_id,
            json.dumps(patch),
        )
        if value is None:
            raise KeyError(workflow_id)
        return dict(json.loads(value))

    async def list_overdue_workflows(
        self, now: str, status: str = "running"
    ) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """SELECT state FROM workflows
               WHERE state->>'status'=$2
                 AND (state->>'deadline_at')::timestamptz < $1::timestamptz""",
            _parse(now),
            status,
        )
        return [json.loads(row["state"]) for row in rows]

    async def recent_bookings(self, requester: str, limit: int) -> list[dict[str, Any]]:
        """This requester's booked workflow states, newest first."""
        rows = await self._pool().fetch(
            """SELECT state FROM workflows
               WHERE state->>'requester'=$1 AND state->>'status'='booked'
               ORDER BY updated_at DESC LIMIT $2""",
            requester,
            limit,
        )
        return [json.loads(row["state"]) for row in rows]

    async def get_profile(self, requester: str) -> dict[str, Any] | None:
        value = await self._pool().fetchval(
            "SELECT profile FROM student_profiles WHERE requester=$1", requester
        )
        return json.loads(value) if value else None

    async def save_profile(self, requester: str, profile: dict[str, Any]) -> None:
        await self._pool().execute(
            """INSERT INTO student_profiles(requester,profile) VALUES($1,$2::jsonb)
               ON CONFLICT(requester) DO UPDATE
               SET profile=EXCLUDED.profile, updated_at=now()""",
            requester,
            json.dumps(profile),
        )

    async def append_event(
        self, workflow_id: str, event_id: str, event_type: str, details: dict[str, Any]
    ) -> bool:
        result = await self._pool().execute(
            """INSERT INTO workflow_events(event_id,workflow_id,event_type,details)
               VALUES($1,$2,$3,$4::jsonb) ON CONFLICT DO NOTHING""",
            event_id,
            workflow_id,
            event_type,
            json.dumps(details),
        )
        return result.endswith("1")

    async def list_events(self, workflow_id: str) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """SELECT event_id,event_type,details FROM workflow_events
               WHERE workflow_id=$1 ORDER BY created_at""",
            workflow_id,
        )
        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "details": json.loads(row["details"]),
            }
            for row in rows
        ]

    async def cache_get(self, key: str) -> Any | None:
        value = await self._pool().fetchval(
            "SELECT payload FROM api_cache WHERE key=$1 AND expires_at > now()", key
        )
        return json.loads(value) if isinstance(value, str) else value

    async def cache_put(self, key: str, value: Any, ttl_seconds: int) -> None:
        await self._pool().execute(
            """INSERT INTO api_cache(key,payload,expires_at)
               VALUES($1,$2::jsonb,now() + make_interval(secs => $3))
               ON CONFLICT(key) DO UPDATE
               SET payload=EXCLUDED.payload, expires_at=EXCLUDED.expires_at""",
            key,
            json.dumps(value),
            float(ttl_seconds),
        )


def build_store() -> DurableStore:
    """Postgres when ``DATABASE_URL`` is set; otherwise the in-process development store."""
    database_url = os.getenv("DATABASE_URL", "").strip()
    return PostgresStore(database_url) if database_url else MemoryStore()


store: DurableStore = build_store()
