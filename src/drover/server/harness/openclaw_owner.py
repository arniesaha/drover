"""Versioned owner-tool contract over the existing Drover continuity endpoint.

An OpenClaw plugin may expose this contract through normal tool registration.
This module does not register a runtime plugin, message a session, execute an
action, retry a transport call, or acknowledge a delivery implicitly.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

ENDPOINT = "/harness/factory-observer/continuity"
TOOL_NAME = "drover_continuity_owner"
Scope = Literal["implementation", "integration", "deployment"]
Id = Annotated[str, Field(min_length=1, max_length=191, pattern=r"\S")]
Checkpoint = Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]
EventId = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Epoch = Annotated[int, Field(ge=1)]
Kind = Literal["worker_awaiting_input", "worker_brief_completed", "ci_red", "ci_green"]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Poll(WireModel):
    operation: Literal["poll"]
    limit: Annotated[int, Field(ge=1, le=50)] = 20


class Lease(WireModel):
    operation: Literal["lease"]
    owner_epoch: Epoch | None = None
    lease_seconds: Annotated[int, Field(ge=1, le=300)] = 60


class Report(WireModel):
    operation: Literal["report"]
    source_event_id: Id
    subject: Id
    sequence: Annotated[int, Field(ge=0, le=2**63 - 1)]
    kind: Kind
    summary: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


class Consume(WireModel):
    operation: Literal["consume"]
    owner_epoch: Epoch


class Acknowledge(WireModel):
    operation: Literal["acknowledge"]
    owner_epoch: Epoch
    event_id: EventId
    checkpoint: Checkpoint


class OwnerToolCall(WireModel):
    version: Literal[1]
    request: Annotated[
        Poll | Lease | Report | Consume | Acknowledge, Field(discriminator="operation")
    ]


class OwnerAction(WireModel):
    action_id: str
    event_id: EventId
    type: Literal[
        "request_input",
        "review_worker_result",
        "correct_ci",
        "review_ci",
        "review_late_report",
        "request_explicit_approval",
    ]
    authority_scope: Scope
    authorized: bool

    @model_validator(mode="after")
    def check_authority(self):
        if self.action_id != "observer-action-" + self.event_id:
            raise ValueError("action identity must match its durable event")
        blocked = self.authority_scope == "deployment"
        if (
            self.authorized == blocked
            or (self.type == "request_explicit_approval") != blocked
        ):
            raise ValueError("deployment requires explicit external approval")
        return self


class OwnerLease(WireModel):
    id: Id | None
    epoch: Annotated[int, Field(ge=0)]
    lease_until: datetime | None
    live: bool


class InboxReport(WireModel):
    source: Id
    source_event_id: Id
    subject: Id
    sequence: Annotated[int, Field(ge=0, le=2**63 - 1)]
    kind: Kind
    summary: Annotated[str, Field(min_length=1, max_length=2000)]


class InboxEntry(WireModel):
    event_id: EventId
    state: Literal["pending", "delivered", "exhausted"]
    attempts: Annotated[int, Field(ge=0, le=3)]
    next_delivery_at: datetime | None
    report: InboxReport


class ContinuityProjection(WireModel):
    version: Literal[1]
    run_id: Id
    session_id: Id
    expected_revision: Annotated[int, Field(ge=0)]
    authority: Literal["taskflow"]
    objective: Checkpoint
    checkpoint: Checkpoint
    authority_scope: Scope
    scope_limit: Literal["commit_only", "publish_review", "explicit_approval_required"]
    owner: OwnerLease
    worker_state: Literal["unknown", "awaiting_input", "brief_completed"]
    terminal_release: Literal[False]
    next_action: OwnerAction | None
    recovery: Literal[
        "claim_owner",
        "reclaim_expired_owner",
        "approval_required",
        "manual_attention",
        "continue_owner",
    ]
    owner_wake: bool
    inbox_counts: dict[
        Literal["pending", "delivered", "acknowledged", "exhausted"],
        Annotated[int, Field(ge=0)],
    ]
    events: Annotated[list[InboxEntry], Field(max_length=50)]
    has_more: bool

    @model_validator(mode="after")
    def check_scope(self):
        limits = {
            "implementation": "commit_only",
            "integration": "publish_review",
            "deployment": "explicit_approval_required",
        }
        if self.scope_limit != limits[self.authority_scope]:
            raise ValueError("projection scope limit is inconsistent")
        if (
            self.next_action
            and self.next_action.authority_scope != self.authority_scope
        ):
            raise ValueError("action cannot broaden run authority")
        if self.authority_scope == "deployment" and self.owner_wake:
            raise ValueError("approval-blocked deployment cannot wake an executor")
        return self


class OwnerToolReply(WireModel):
    version: Literal[1] = 1
    continuity: ContinuityProjection
    event_id: EventId | None = None
    delivery: OwnerAction | None = None


class ContinuityEndpoint(Protocol):
    """Injected, normally authenticated HTTP/tool transport, not messaging RPC."""

    def get(self, path: str, *, query: dict) -> dict: ...
    def post(self, path: str, *, body: dict) -> dict: ...


def owner_tool_definition() -> dict:
    """Reviewable input schema for normal OpenClaw plugin-tool registration.

    The runtime still needs an execute handler/transport and existing tool
    authorization. Returning this descriptor does not register or enable it.
    """
    return {
        "name": TOOL_NAME,
        "description": "Read/report/lease/consume/ack Drover owner continuity. Actions are advisory; no Factory controls, merge or deployment. Ack only after idempotent owner reconciliation.",
        "parameters": OwnerToolCall.model_json_schema(),
    }


class OpenClawOwnerAdapter:
    """Explicit tool calls for one trusted owner/run/scope, with no local ledger.

    A runtime binds these values from trusted owner context, not model input.
    Initialize the correlated observer run through the existing endpoint first.
    HTTP failures propagate; an unknown consume outcome requires polling and
    reconciliation by action_id, never an automatic ack or blind action retry.
    """

    def __init__(
        self,
        endpoint: ContinuityEndpoint,
        *,
        run_id: str,
        owner_id: str,
        authority_scope: Scope,
    ):
        # Reuse strict field validation without accepting credentials or URLs.
        context = _OwnerContext(
            run_id=run_id, owner_id=owner_id, authority_scope=authority_scope
        )
        self.endpoint = endpoint
        self.context = context

    def invoke(self, payload: dict) -> OwnerToolReply:
        call = OwnerToolCall.model_validate(payload)
        request = call.request
        context = self.context
        if isinstance(request, Poll):
            raw = self.endpoint.get(
                ENDPOINT, query={"run_id": context.run_id, "limit": request.limit}
            )
        else:
            # Run scope is immutable. Check it before a mutation so a wrongly
            # bound adapter cannot acknowledge a different authority scope.
            self._reply(
                self.endpoint.get(
                    ENDPOINT, query={"run_id": context.run_id, "limit": 1}
                )
            )
            body = request.model_dump(exclude_none=True)
            body["run_id"] = context.run_id
            if isinstance(request, Report):
                body["source"] = "openclaw"
            else:
                body["owner_id"] = context.owner_id
            raw = self.endpoint.post(ENDPOINT, body=body)
        reply = self._reply(raw)
        projection = reply.continuity
        if isinstance(request, Consume):
            if "delivery" not in raw or (
                reply.delivery is not None and reply.delivery != projection.next_action
            ):
                raise ValueError(
                    "consume reply is missing or mismatches durable action"
                )
        elif "delivery" in raw:
            raise ValueError("only consume may deliver an owner action")
        if isinstance(request, (Lease, Consume, Acknowledge)):
            if projection.owner.id != context.owner_id or not projection.owner.live:
                raise ValueError("reply no longer carries the trusted live owner")
            if (
                isinstance(request, (Consume, Acknowledge))
                and projection.owner.epoch != request.owner_epoch
            ):
                raise ValueError("reply no longer carries the requested owner epoch")
        if isinstance(request, Report):
            if reply.event_id is None:
                raise ValueError("report reply must carry the durable event identity")
        elif "event_id" in raw:
            raise ValueError("only report may return an event identity")
        return reply

    def _reply(self, raw: dict) -> OwnerToolReply:
        # Strict JSON validation permits wire ISO timestamps without coercing
        # epoch strings/bools, unknown action names, or authority expansions.
        if "version" in raw:
            raise ValueError("endpoint must return the existing boundary envelope")
        reply = OwnerToolReply.model_validate_json(json.dumps({"version": 1, **raw}))
        projection = reply.continuity
        if (
            projection.run_id != self.context.run_id
            or projection.authority_scope != self.context.authority_scope
        ):
            raise ValueError("reply does not match the trusted owner run/scope")
        return reply


class _OwnerContext(WireModel):
    run_id: Id
    owner_id: Id
    authority_scope: Scope
