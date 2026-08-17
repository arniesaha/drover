"""Typed records for Drover harness state."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _loads_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        return {}
    return loaded


@dataclass(frozen=True)
class HarnessHost:
    host_id: str
    display_name: str
    kind: str
    status: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    local_url: str | None = None
    tailscale_url: str | None = None
    connection_kind: str = "direct"
    # What the host says it is running. None for a host old enough not to
    # report one, which is ordinary during a rollout rather than an error.
    agent_version: str | None = None
    # Live update state reported on heartbeat: pending_version, update_blocked,
    # reason, and observed_at.
    update: dict[str, Any] | None = None
    last_seen_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "HarnessHost":
        return cls(
            host_id=row["host_id"],
            display_name=row["display_name"],
            kind=row["kind"],
            status=row["status"],
            local_url=row.get("local_url"),
            tailscale_url=row.get("tailscale_url"),
            connection_kind=row.get("connection_kind") or "direct",
            capabilities=_loads_object(row.get("capabilities_json")),
            agent_version=row.get("agent_version"),
            update=_loads_object(row.get("update_json")) or None,
            last_seen_at=row.get("last_seen_at"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )


@dataclass(frozen=True)
class HarnessSession:
    session_id: str
    host_id: str
    harness: str
    command: str
    status: str
    repo_owner: str | None = None
    repo_name: str | None = None
    branch: str | None = None
    cwd: str | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    ended_at: datetime | None = None
    last_error: str | None = None
    summary_session_id: str | None = None
    native_session_id: str | None = None
    native_resume_label: str | None = None
    source_session_id: str | None = None
    handoff_mode: str | None = None
    mode: str | None = None
    permission_mode: str | None = None
    model: str | None = None
    thinking_effort: str | None = None
    awaiting: str | None = None
    last_activity: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "HarnessSession":
        return cls(
            session_id=row["session_id"],
            host_id=row["host_id"],
            harness=row["harness"],
            command=row["command"],
            status=row["status"],
            repo_owner=row.get("repo_owner"),
            repo_name=row.get("repo_name"),
            branch=row.get("branch"),
            cwd=row.get("cwd"),
            started_at=row.get("started_at"),
            updated_at=row.get("updated_at"),
            ended_at=row.get("ended_at"),
            last_error=row.get("last_error"),
            summary_session_id=row.get("summary_session_id"),
            native_session_id=row.get("native_session_id"),
            native_resume_label=row.get("native_resume_label"),
            source_session_id=row.get("source_session_id"),
            handoff_mode=row.get("handoff_mode"),
            mode=row.get("mode"),
            permission_mode=row.get("permission_mode"),
            model=row.get("model"),
            thinking_effort=row.get("thinking_effort"),
            awaiting=row.get("awaiting"),
            last_activity=row.get("last_activity"),
        )


@dataclass(frozen=True)
class HarnessEvent:
    event_id: str
    session_id: str
    event_type: str
    normalized_type: str | None = None
    normalized_source: str | None = None
    content_preview: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    seq: int | None = None

    def wire_payload(self) -> dict[str, Any]:
        if self.seq is None:
            raise ValueError("cannot serialize harness event without a sequence")
        payload = dict(self.payload)
        payload["event_id"] = self.event_id
        payload["session_id"] = self.session_id
        payload["seq"] = self.seq
        # Shell output already rides in `text`. The codex adapter used to put
        # a second copy in the payload as well, which on a live session was
        # 992KB across 236 tool results — for a field nothing has ever read.
        # The adapter no longer writes it, but every event already stored
        # does, so it is dropped here too rather than waiting for that history
        # to age out of the transcript window.
        inner = payload.get("payload")
        if isinstance(inner, dict) and "aggregated_output" in inner:
            payload["payload"] = {
                key: value for key, value in inner.items() if key != "aggregated_output"
            }
        return payload

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "HarnessEvent":
        return cls(
            event_id=row["event_id"],
            session_id=row["session_id"],
            event_type=row["event_type"],
            normalized_type=row.get("normalized_type"),
            normalized_source=row.get("normalized_source"),
            content_preview=row.get("content_preview"),
            payload=_loads_object(row.get("payload_json")),
            created_at=row.get("created_at"),
            seq=row.get("seq"),
        )


@dataclass(frozen=True)
class HarnessEventPage:
    events: list[HarnessEvent]
    page_min_seq: int | None
    page_max_seq: int | None
    max_seq: int
    has_older: bool
    has_newer: bool
