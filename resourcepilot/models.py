from __future__ import annotations

from typing import Any

from pydantic.v1 import Field
from uagents import Model


class TaskRequest(Model):
    workflow_id: str
    request_id: str
    requester: str
    session_id: str
    target_role: str
    payload: dict[str, Any]
    deadline_at: str


class TaskResult(Model):
    workflow_id: str
    request_id: str
    requester: str
    session_id: str
    source_role: str
    ok: bool
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class AuditEvent(Model):
    workflow_id: str
    requester: str
    session_id: str
    event_id: str
    event_type: str
    details: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(Model):
    status: str
    role: str
    address: str


class PaymentRecord(Model):
    reference: str
    workflow_id: str
    requester: str
    amount: str
    currency: str
    recipient: str
    network: str
    status: str
    transaction_id: str | None = None
    buyer_wallet: str | None = None
