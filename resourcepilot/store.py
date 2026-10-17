from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from datetime import datetime
from typing import Any, Protocol

import asyncpg

from resourcepilot.models import PaymentRecord

SELLER_COUNTERS = (
    "quotes_sent",
    "no_responses",
    "holds_confirmed",
    "holds_failed",
    "bookings_committed",
    "bookings_failed",
)
PAID_STATES = {"verified", "reserved", "released", "delivered", "refunded"}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _overlaps(start: str, end: str, other_start: str, other_end: str) -> bool:
    return _parse(start) < _parse(other_end) and _parse(other_start) < _parse(end)


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

    async def list_overdue_workflows(self, now: str) -> list[dict[str, Any]]: ...

    async def recent_bookings(self, requester: str, limit: int) -> list[dict[str, Any]]: ...

    # Audit and memory
    async def append_event(
        self, workflow_id: str, event_id: str, event_type: str, details: dict[str, Any]
    ) -> bool: ...

    async def list_events(self, workflow_id: str) -> list[dict[str, Any]]: ...

    async def save_preference(self, requester: str, preferences: dict[str, Any]) -> None: ...

    async def get_preference(self, requester: str) -> dict[str, Any]: ...

    # Buyer payments into escrow
    async def create_payment(self, record: PaymentRecord) -> None: ...

    async def get_payment(self, reference: str) -> dict[str, Any] | None: ...

    async def verify_payment(
        self, reference: str, transaction_id: str, buyer_wallet: str
    ) -> str: ...

    async def reserve_payment(self, reference: str) -> bool: ...

    async def deliver_payment(self, reference: str) -> bool: ...

    async def release_payment(self, reference: str, reason: str) -> bool: ...

    async def cancel_payment(self, reference: str, reason: str) -> bool: ...

    async def refund_payment(self, reference: str, reason: str) -> bool: ...

    # Escrow ledger: payouts to sellers, refunds to buyers, platform fees
    async def add_escrow_entry(self, entry: dict[str, Any]) -> bool: ...

    async def update_escrow_entry(
        self, entry_id: str, status: str, transaction_id: str | None
    ) -> None: ...

    async def list_escrow_entries(self, reference: str) -> list[dict[str, Any]]: ...

    # Fan-out rounds owned by Discovery and Fulfillment
    async def create_round(
        self, round_id: str, owner_role: str, deadline_at: str, state: dict[str, Any]
    ) -> None: ...

    async def get_round(self, round_id: str) -> dict[str, Any] | None: ...

    async def record_reply(
        self, round_id: str, key: str, reply: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    async def close_round(self, round_id: str) -> bool: ...

    async def list_expired_rounds(self, owner_role: str, now: str) -> list[dict[str, Any]]: ...

    # Seller inventory holds (each seller only touches its own rows)
    async def place_hold(self, hold: dict[str, Any], capacity: int, now: str) -> str: ...

    async def get_hold(self, hold_id: str) -> dict[str, Any] | None: ...

    async def commit_hold(
        self, hold_id: str, payment_reference: str, confirmation_code: str, now: str
    ) -> str: ...

    async def release_hold(self, hold_id: str, reason: str) -> bool: ...

    async def used_quantity(
        self, seller_id: str, item_id: str, start: str, end: str, now: str
    ) -> int: ...

    async def get_fault_mode(self, seller_id: str) -> str | None: ...

    async def set_fault_mode(self, seller_id: str, mode: str) -> None: ...

    # Seller track records
    async def record_seller_outcome(self, seller_id: str, counter: str) -> None: ...

    async def seller_records(self, seller_ids: list[str]) -> dict[str, dict[str, int]]: ...


def _active(hold: dict[str, Any], now: str) -> bool:
    if hold["status"] == "committed":
        return True
    return hold["status"] == "held" and _parse(str(hold["expires_at"])) > _parse(now)


class MemoryStore:
    """Single-process, non-production fallback used by tests and explicit local runs."""

    production_ready = False

    def __init__(self) -> None:
        self.workflows: dict[str, dict[str, Any]] = {}
        self.claims: set[tuple[str, str]] = set()
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.preferences: dict[str, dict[str, Any]] = {}
        self.payments: dict[str, dict[str, Any]] = {}
        self.transactions: set[str] = set()
        self.escrow: dict[str, dict[str, Any]] = {}
        self.rounds: dict[str, dict[str, Any]] = {}
        self.holds: dict[str, dict[str, Any]] = {}
        self.fault_modes: dict[str, str] = {}
        self.stats: dict[str, dict[str, int]] = {}
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

    async def list_overdue_workflows(self, now: str) -> list[dict[str, Any]]:
        return [
            deepcopy(state)
            for state in self.workflows.values()
            if state.get("status") == "running"
            and state.get("deadline_at")
            and _parse(str(state["deadline_at"])) < _parse(now)
        ]

    async def recent_bookings(self, requester: str, limit: int) -> list[dict[str, Any]]:
        values = [
            deepcopy(state)
            for state in self.workflows.values()
            if state.get("requester") == requester and state.get("status") == "booked"
        ]
        return list(reversed(values))[:limit]

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

    async def save_preference(self, requester: str, preferences: dict[str, Any]) -> None:
        async with self._lock:
            self.preferences.setdefault(requester, {}).update(deepcopy(preferences))

    async def get_preference(self, requester: str) -> dict[str, Any]:
        return deepcopy(self.preferences.get(requester, {}))

    async def create_payment(self, record: PaymentRecord) -> None:
        async with self._lock:
            if record.reference in self.payments:
                return
            self.payments[record.reference] = record.model_dump()

    async def get_payment(self, reference: str) -> dict[str, Any] | None:
        value = self.payments.get(reference)
        return deepcopy(value) if value else None

    async def verify_payment(self, reference: str, transaction_id: str, buyer_wallet: str) -> str:
        async with self._lock:
            payment = self.payments.get(reference)
            if payment is None:
                return "missing"
            if payment.get("transaction_id") == transaction_id and payment["status"] in PAID_STATES:
                return "replay"
            if transaction_id in self.transactions:
                return "transaction_replay"
            if payment["status"] != "requested":
                return "invalid_state"
            self.transactions.add(transaction_id)
            payment.update(
                status="verified", transaction_id=transaction_id, buyer_wallet=buyer_wallet
            )
            return "verified"

    async def _transition(
        self, reference: str, sources: set[str], target: str, reason: str | None = None
    ) -> bool:
        async with self._lock:
            payment = self.payments.get(reference)
            if payment is None or payment["status"] not in sources:
                return False
            payment["status"] = target
            if reason is not None:
                payment["release_reason"] = reason
            return True

    async def reserve_payment(self, reference: str) -> bool:
        return await self._transition(reference, {"verified", "released"}, "reserved")

    async def deliver_payment(self, reference: str) -> bool:
        return await self._transition(reference, {"reserved"}, "delivered")

    async def release_payment(self, reference: str, reason: str) -> bool:
        return await self._transition(reference, {"reserved"}, "released", reason)

    async def cancel_payment(self, reference: str, reason: str) -> bool:
        return await self._transition(reference, {"requested"}, "cancelled", reason)

    async def refund_payment(self, reference: str, reason: str) -> bool:
        return await self._transition(reference, {"verified", "released"}, "refunded", reason)

    async def add_escrow_entry(self, entry: dict[str, Any]) -> bool:
        async with self._lock:
            if entry["entry_id"] in self.escrow:
                return False
            self.escrow[entry["entry_id"]] = deepcopy(entry)
            return True

    async def update_escrow_entry(
        self, entry_id: str, status: str, transaction_id: str | None
    ) -> None:
        async with self._lock:
            self.escrow[entry_id].update(status=status, transaction_id=transaction_id)

    async def list_escrow_entries(self, reference: str) -> list[dict[str, Any]]:
        return [deepcopy(item) for item in self.escrow.values() if item["reference"] == reference]

    async def create_round(
        self, round_id: str, owner_role: str, deadline_at: str, state: dict[str, Any]
    ) -> None:
        async with self._lock:
            if round_id in self.rounds:
                return
            self.rounds[round_id] = {
                "round_id": round_id,
                "owner_role": owner_role,
                "deadline_at": deadline_at,
                "closed": False,
                "replies": {},
                **deepcopy(state),
            }

    async def get_round(self, round_id: str) -> dict[str, Any] | None:
        value = self.rounds.get(round_id)
        return deepcopy(value) if value else None

    async def record_reply(
        self, round_id: str, key: str, reply: dict[str, Any]
    ) -> dict[str, Any] | None:
        async with self._lock:
            state = self.rounds.get(round_id)
            if state is None or state["closed"] or key in state["replies"]:
                return None
            state["replies"][key] = deepcopy(reply)
            return deepcopy(state)

    async def close_round(self, round_id: str) -> bool:
        async with self._lock:
            state = self.rounds.get(round_id)
            if state is None or state["closed"]:
                return False
            state["closed"] = True
            return True

    async def list_expired_rounds(self, owner_role: str, now: str) -> list[dict[str, Any]]:
        return [
            deepcopy(state)
            for state in self.rounds.values()
            if state["owner_role"] == owner_role
            and not state["closed"]
            and _parse(str(state["deadline_at"])) < _parse(now)
        ]

    async def place_hold(self, hold: dict[str, Any], capacity: int, now: str) -> str:
        async with self._lock:
            if hold["hold_id"] in self.holds:
                return "duplicate"
            used = sum(
                int(other["quantity"])
                for other in self.holds.values()
                if other["seller_id"] == hold["seller_id"]
                and other["item_id"] == hold["item_id"]
                and _active(other, now)
                and _overlaps(
                    hold["window_start"],
                    hold["window_end"],
                    other["window_start"],
                    other["window_end"],
                )
            )
            if used + int(hold["quantity"]) > capacity:
                return "unavailable"
            self.holds[hold["hold_id"]] = {**deepcopy(hold), "status": "held"}
            return "held"

    async def get_hold(self, hold_id: str) -> dict[str, Any] | None:
        value = self.holds.get(hold_id)
        return deepcopy(value) if value else None

    async def commit_hold(
        self, hold_id: str, payment_reference: str, confirmation_code: str, now: str
    ) -> str:
        async with self._lock:
            hold = self.holds.get(hold_id)
            if hold is None:
                return "missing"
            if hold["status"] == "committed":
                return "duplicate"
            if hold["status"] != "held":
                return hold["status"]
            if _parse(str(hold["expires_at"])) <= _parse(now):
                hold["status"] = "expired"
                return "expired"
            hold.update(
                status="committed",
                payment_reference=payment_reference,
                confirmation_code=confirmation_code,
            )
            return "committed"

    async def release_hold(self, hold_id: str, reason: str) -> bool:
        async with self._lock:
            hold = self.holds.get(hold_id)
            if hold is None or hold["status"] not in {"held", "committed"}:
                return False
            hold.update(status="released", release_reason=reason)
            return True

    async def used_quantity(
        self, seller_id: str, item_id: str, start: str, end: str, now: str
    ) -> int:
        return sum(
            int(hold["quantity"])
            for hold in self.holds.values()
            if hold["seller_id"] == seller_id
            and hold["item_id"] == item_id
            and _active(hold, now)
            and _overlaps(start, end, hold["window_start"], hold["window_end"])
        )

    async def get_fault_mode(self, seller_id: str) -> str | None:
        return self.fault_modes.get(seller_id)

    async def set_fault_mode(self, seller_id: str, mode: str) -> None:
        async with self._lock:
            self.fault_modes[seller_id] = mode

    async def record_seller_outcome(self, seller_id: str, counter: str) -> None:
        if counter not in SELLER_COUNTERS:
            raise ValueError(f"unknown seller counter: {counter}")
        async with self._lock:
            stats = self.stats.setdefault(seller_id, dict.fromkeys(SELLER_COUNTERS, 0))
            stats[counter] += 1

    async def seller_records(self, seller_ids: list[str]) -> dict[str, dict[str, int]]:
        return {
            seller_id: dict(self.stats.get(seller_id, dict.fromkeys(SELLER_COUNTERS, 0)))
            for seller_id in seller_ids
        }


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
CREATE TABLE IF NOT EXISTS user_preferences (
  requester TEXT PRIMARY KEY, preferences JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS payments (
  reference TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, requester TEXT NOT NULL,
  amount TEXT NOT NULL, currency TEXT NOT NULL, recipient TEXT NOT NULL, network TEXT NOT NULL,
  status TEXT NOT NULL, transaction_id TEXT UNIQUE, buyer_wallet TEXT,
  release_reason TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS escrow_entries (
  entry_id TEXT PRIMARY KEY, reference TEXT NOT NULL, kind TEXT NOT NULL, party TEXT NOT NULL,
  amount TEXT NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL, transaction_id TEXT,
  note TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS fanout_rounds (
  round_id TEXT PRIMARY KEY, owner_role TEXT NOT NULL, deadline_at TIMESTAMPTZ NOT NULL,
  closed BOOLEAN NOT NULL DEFAULT false, state JSONB NOT NULL,
  replies JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS seller_holds (
  hold_id TEXT PRIMARY KEY, seller_id TEXT NOT NULL, item_id TEXT NOT NULL,
  workflow_id TEXT NOT NULL, quantity INTEGER NOT NULL,
  window_start TIMESTAMPTZ NOT NULL, window_end TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL, subtotal_cents INTEGER NOT NULL,
  payment_reference TEXT, confirmation_code TEXT, release_reason TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS seller_holds_item ON seller_holds (seller_id, item_id);
CREATE TABLE IF NOT EXISTS seller_settings (
  seller_id TEXT PRIMARY KEY, fault_mode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seller_stats (
  seller_id TEXT PRIMARY KEY,
  quotes_sent INTEGER NOT NULL DEFAULT 0, no_responses INTEGER NOT NULL DEFAULT 0,
  holds_confirmed INTEGER NOT NULL DEFAULT 0, holds_failed INTEGER NOT NULL DEFAULT 0,
  bookings_committed INTEGER NOT NULL DEFAULT 0, bookings_failed INTEGER NOT NULL DEFAULT 0
);
"""

ACTIVE_HOLD_SQL = "(status='committed' OR (status='held' AND expires_at > $5::timestamptz))"


def _jsonable(row: asyncpg.Record) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in dict(row).items():
        result[key] = value.isoformat() if isinstance(value, datetime) else value
    return result


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

    async def list_overdue_workflows(self, now: str) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """SELECT state FROM workflows
               WHERE state->>'status'='running'
                 AND (state->>'deadline_at')::timestamptz < $1::timestamptz""",
            _parse(now),
        )
        return [json.loads(row["state"]) for row in rows]

    async def recent_bookings(self, requester: str, limit: int) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """SELECT state FROM workflows
               WHERE state->>'requester'=$1 AND state->>'status'='booked'
               ORDER BY updated_at DESC LIMIT $2""",
            requester,
            limit,
        )
        return [json.loads(row["state"]) for row in rows]

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

    async def save_preference(self, requester: str, preferences: dict[str, Any]) -> None:
        await self._pool().execute(
            """INSERT INTO user_preferences(requester,preferences) VALUES($1,$2::jsonb)
               ON CONFLICT(requester) DO UPDATE
               SET preferences=user_preferences.preferences || EXCLUDED.preferences,
                   updated_at=now()""",
            requester,
            json.dumps(preferences),
        )

    async def get_preference(self, requester: str) -> dict[str, Any]:
        value = await self._pool().fetchval(
            "SELECT preferences FROM user_preferences WHERE requester=$1", requester
        )
        return dict(json.loads(value)) if value else {}

    async def create_payment(self, record: PaymentRecord) -> None:
        await self._pool().execute(
            """INSERT INTO payments(
                 reference,workflow_id,requester,amount,currency,recipient,network,status
               )
               VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(reference) DO NOTHING""",
            record.reference,
            record.workflow_id,
            record.requester,
            record.amount,
            record.currency,
            record.recipient,
            record.network,
            record.status,
        )

    async def get_payment(self, reference: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow("SELECT * FROM payments WHERE reference=$1", reference)
        return _jsonable(row) if row else None

    async def verify_payment(self, reference: str, transaction_id: str, buyer_wallet: str) -> str:
        async with self._pool().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT * FROM payments WHERE reference=$1 FOR UPDATE", reference
            )
            if row is None:
                return "missing"
            if row["transaction_id"] == transaction_id and row["status"] in PAID_STATES:
                return "replay"
            other = await connection.fetchval(
                "SELECT reference FROM payments WHERE transaction_id=$1", transaction_id
            )
            if other:
                return "transaction_replay"
            if row["status"] != "requested":
                return "invalid_state"
            await connection.execute(
                """UPDATE payments
                   SET status='verified',transaction_id=$2,buyer_wallet=$3,updated_at=now()
                   WHERE reference=$1""",
                reference,
                transaction_id,
                buyer_wallet,
            )
            return "verified"

    async def _transition(
        self, reference: str, sources: tuple[str, ...], target: str, reason: str | None = None
    ) -> bool:
        result = await self._pool().execute(
            """UPDATE payments
               SET status=$3, release_reason=COALESCE($4, release_reason), updated_at=now()
               WHERE reference=$1 AND status=ANY($2)""",
            reference,
            list(sources),
            target,
            reason,
        )
        return result.endswith("1")

    async def reserve_payment(self, reference: str) -> bool:
        return await self._transition(reference, ("verified", "released"), "reserved")

    async def deliver_payment(self, reference: str) -> bool:
        return await self._transition(reference, ("reserved",), "delivered")

    async def release_payment(self, reference: str, reason: str) -> bool:
        return await self._transition(reference, ("reserved",), "released", reason)

    async def cancel_payment(self, reference: str, reason: str) -> bool:
        return await self._transition(reference, ("requested",), "cancelled", reason)

    async def refund_payment(self, reference: str, reason: str) -> bool:
        return await self._transition(reference, ("verified", "released"), "refunded", reason)

    async def add_escrow_entry(self, entry: dict[str, Any]) -> bool:
        result = await self._pool().execute(
            """INSERT INTO escrow_entries(
                 entry_id,reference,kind,party,amount,currency,status,transaction_id,note
               ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT DO NOTHING""",
            entry["entry_id"],
            entry["reference"],
            entry["kind"],
            entry["party"],
            str(entry["amount"]),
            entry["currency"],
            entry["status"],
            entry.get("transaction_id"),
            entry.get("note", ""),
        )
        return result.endswith("1")

    async def update_escrow_entry(
        self, entry_id: str, status: str, transaction_id: str | None
    ) -> None:
        await self._pool().execute(
            "UPDATE escrow_entries SET status=$2, transaction_id=$3 WHERE entry_id=$1",
            entry_id,
            status,
            transaction_id,
        )

    async def list_escrow_entries(self, reference: str) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            "SELECT * FROM escrow_entries WHERE reference=$1 ORDER BY created_at", reference
        )
        return [_jsonable(row) for row in rows]

    async def create_round(
        self, round_id: str, owner_role: str, deadline_at: str, state: dict[str, Any]
    ) -> None:
        await self._pool().execute(
            """INSERT INTO fanout_rounds(round_id,owner_role,deadline_at,state)
               VALUES($1,$2,$3::timestamptz,$4::jsonb) ON CONFLICT DO NOTHING""",
            round_id,
            owner_role,
            _parse(deadline_at),
            json.dumps(state),
        )

    @staticmethod
    def _round(row: asyncpg.Record) -> dict[str, Any]:
        return {
            **json.loads(row["state"]),
            "round_id": row["round_id"],
            "owner_role": row["owner_role"],
            "deadline_at": row["deadline_at"].isoformat(),
            "closed": row["closed"],
            "replies": json.loads(row["replies"]),
        }

    async def get_round(self, round_id: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow("SELECT * FROM fanout_rounds WHERE round_id=$1", round_id)
        return self._round(row) if row else None

    async def record_reply(
        self, round_id: str, key: str, reply: dict[str, Any]
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """UPDATE fanout_rounds SET replies = replies || jsonb_build_object($2::text, $3::jsonb)
               WHERE round_id=$1 AND NOT closed AND NOT (replies ? $2)
               RETURNING *""",
            round_id,
            key,
            json.dumps(reply),
        )
        return self._round(row) if row else None

    async def close_round(self, round_id: str) -> bool:
        result = await self._pool().execute(
            "UPDATE fanout_rounds SET closed=true WHERE round_id=$1 AND NOT closed", round_id
        )
        return result.endswith("1")

    async def list_expired_rounds(self, owner_role: str, now: str) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """SELECT * FROM fanout_rounds
               WHERE owner_role=$1 AND NOT closed AND deadline_at < $2::timestamptz""",
            owner_role,
            _parse(now),
        )
        return [self._round(row) for row in rows]

    async def place_hold(self, hold: dict[str, Any], capacity: int, now: str) -> str:
        async with self._pool().acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"{hold['seller_id']}:{hold['item_id']}",
            )
            if await connection.fetchval(
                "SELECT 1 FROM seller_holds WHERE hold_id=$1", hold["hold_id"]
            ):
                return "duplicate"
            used = await connection.fetchval(
                f"""SELECT COALESCE(SUM(quantity),0) FROM seller_holds
                    WHERE seller_id=$1 AND item_id=$2
                      AND window_start < $4::timestamptz AND $3::timestamptz < window_end
                      AND {ACTIVE_HOLD_SQL}""",
                hold["seller_id"],
                hold["item_id"],
                _parse(hold["window_start"]),
                _parse(hold["window_end"]),
                _parse(now),
            )
            if int(used) + int(hold["quantity"]) > capacity:
                return "unavailable"
            await connection.execute(
                """INSERT INTO seller_holds(
                     hold_id,seller_id,item_id,workflow_id,quantity,window_start,window_end,
                     expires_at,status,subtotal_cents
                   ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,'held',$9)""",
                hold["hold_id"],
                hold["seller_id"],
                hold["item_id"],
                hold["workflow_id"],
                int(hold["quantity"]),
                _parse(hold["window_start"]),
                _parse(hold["window_end"]),
                _parse(hold["expires_at"]),
                int(hold["subtotal_cents"]),
            )
            return "held"

    async def get_hold(self, hold_id: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow("SELECT * FROM seller_holds WHERE hold_id=$1", hold_id)
        return _jsonable(row) if row else None

    async def commit_hold(
        self, hold_id: str, payment_reference: str, confirmation_code: str, now: str
    ) -> str:
        async with self._pool().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT status, expires_at FROM seller_holds WHERE hold_id=$1 FOR UPDATE", hold_id
            )
            if row is None:
                return "missing"
            if row["status"] == "committed":
                return "duplicate"
            if row["status"] != "held":
                return str(row["status"])
            if row["expires_at"] <= _parse(now):
                await connection.execute(
                    "UPDATE seller_holds SET status='expired',updated_at=now() WHERE hold_id=$1",
                    hold_id,
                )
                return "expired"
            await connection.execute(
                """UPDATE seller_holds SET status='committed', payment_reference=$2,
                     confirmation_code=$3, updated_at=now() WHERE hold_id=$1""",
                hold_id,
                payment_reference,
                confirmation_code,
            )
            return "committed"

    async def release_hold(self, hold_id: str, reason: str) -> bool:
        result = await self._pool().execute(
            """UPDATE seller_holds SET status='released', release_reason=$2, updated_at=now()
               WHERE hold_id=$1 AND status IN ('held','committed')""",
            hold_id,
            reason,
        )
        return result.endswith("1")

    async def used_quantity(
        self, seller_id: str, item_id: str, start: str, end: str, now: str
    ) -> int:
        value = await self._pool().fetchval(
            f"""SELECT COALESCE(SUM(quantity),0) FROM seller_holds
                WHERE seller_id=$1 AND item_id=$2
                  AND window_start < $4::timestamptz AND $3::timestamptz < window_end
                  AND {ACTIVE_HOLD_SQL}""",
            seller_id,
            item_id,
            _parse(start),
            _parse(end),
            _parse(now),
        )
        return int(value)

    async def get_fault_mode(self, seller_id: str) -> str | None:
        value = await self._pool().fetchval(
            "SELECT fault_mode FROM seller_settings WHERE seller_id=$1", seller_id
        )
        return str(value) if value else None

    async def set_fault_mode(self, seller_id: str, mode: str) -> None:
        await self._pool().execute(
            """INSERT INTO seller_settings(seller_id,fault_mode) VALUES($1,$2)
               ON CONFLICT(seller_id) DO UPDATE SET fault_mode=EXCLUDED.fault_mode""",
            seller_id,
            mode,
        )

    async def record_seller_outcome(self, seller_id: str, counter: str) -> None:
        if counter not in SELLER_COUNTERS:
            raise ValueError(f"unknown seller counter: {counter}")
        await self._pool().execute(
            f"""INSERT INTO seller_stats(seller_id,{counter}) VALUES($1,1)
                ON CONFLICT(seller_id) DO UPDATE SET {counter}=seller_stats.{counter}+1""",
            seller_id,
        )

    async def seller_records(self, seller_ids: list[str]) -> dict[str, dict[str, int]]:
        rows = await self._pool().fetch(
            "SELECT * FROM seller_stats WHERE seller_id = ANY($1)", seller_ids
        )
        found = {
            row["seller_id"]: {counter: int(row[counter]) for counter in SELLER_COUNTERS}
            for row in rows
        }
        return {
            seller_id: found.get(seller_id, dict.fromkeys(SELLER_COUNTERS, 0))
            for seller_id in seller_ids
        }


def build_store() -> DurableStore:
    database_url = os.getenv("DATABASE_URL", "").strip()
    return PostgresStore(database_url) if database_url else MemoryStore()


store: DurableStore = build_store()
