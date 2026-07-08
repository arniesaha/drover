"""Typed records for Drover Meta Harness state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
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
            capabilities=_loads_object(row.get("capabilities_json")),
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
class HarnessTranscriptChunk:
    chunk_id: str
    session_id: str
    sequence: int
    content_redacted: str
    byte_count: int
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "HarnessTranscriptChunk":
        return cls(
            chunk_id=row["chunk_id"],
            session_id=row["session_id"],
            sequence=row["sequence"],
            content_redacted=row["content_redacted"],
            byte_count=row["byte_count"],
            created_at=row.get("created_at"),
        )
