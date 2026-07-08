"""Registry helpers for Drover Meta Harness hosts and sessions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

import duckdb

from drover.server.harness.models import (
    HarnessEvent,
    HarnessHost,
    HarnessSession,
    HarnessTranscriptChunk,
)
from drover.server.harness.events import normalize_harness_event

# DuckDB's Python client is not safe against two threads in one process
# calling duckdb.connect() on the same database file at nearly the same
# instant: the loser raises "Binder Error: Unique file handle conflict"
# instead of waiting (observed live in the structured-session E2E when two
# sessions' pump threads wrote to one registry concurrently -- see
# tests/test_structured_e2e.py). Serialize the ENTIRE connect->use->close
# window per resolved database path, process-wide: the lock table is
# module-level (not per-instance) because central constructs a fresh
# HarnessRegistry per request, so per-instance locks would not stop
# cross-instance collisions on the same file.
_DB_LOCKS: dict[str, threading.Lock] = {}
_DB_LOCKS_GUARD = threading.Lock()


def _db_lock(duckdb_path: Path) -> threading.Lock:
    key = str(duckdb_path.expanduser().resolve())
    with _DB_LOCKS_GUARD:
        lock = _DB_LOCKS.get(key)
        if lock is None:
            lock = _DB_LOCKS[key] = threading.Lock()
        return lock


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_dumps(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def _rows(
    con: duckdb.DuckDBPyConnection, query: str, params: list[Any]
) -> list[dict[str, Any]]:
    result = con.execute(query, params)
    cols = [desc[0] for desc in result.description]
    return [dict(zip(cols, row)) for row in result.fetchall()]


class HarnessRegistry:
    """Small DuckDB-backed registry for Meta Harness control-plane state."""

    def __init__(self, duckdb_path: str | Path):
        self.duckdb_path = Path(duckdb_path)

    @contextmanager
    def _connect(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Yield a connection, holding this database's process-wide lock.

        Concurrent duckdb.connect() calls to one file from multiple threads
        raise BinderException ("Unique file handle conflict"), so the whole
        connect -> use -> close window is serialized per resolved path (see
        _db_lock above). Existing ``with self._connect() as con:`` call
        sites work unchanged.
        """
        with _db_lock(self.duckdb_path):
            con = duckdb.connect(str(self.duckdb_path))
            try:
                yield con
            finally:
                con.close()

    def register_host(
        self,
        *,
        host_id: str,
        display_name: str,
        kind: str,
        local_url: str | None = None,
        tailscale_url: str | None = None,
        capabilities: dict[str, Any] | None = None,
        status: str = "online",
    ) -> HarnessHost:
        now = _now()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO harness_hosts (
                  host_id, display_name, kind, local_url, tailscale_url, status,
                  capabilities_json, last_seen_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                  display_name = excluded.display_name,
                  kind = excluded.kind,
                  local_url = excluded.local_url,
                  tailscale_url = excluded.tailscale_url,
                  status = excluded.status,
                  capabilities_json = excluded.capabilities_json,
                  last_seen_at = excluded.last_seen_at,
                  updated_at = excluded.updated_at
                """,
                [
                    host_id,
                    display_name,
                    kind,
                    local_url,
                    tailscale_url,
                    status,
                    _json_dumps(capabilities),
                    now,
                    now,
                    now,
                ],
            )
        host = self.get_host(host_id)
        if host is None:
            raise RuntimeError(f"failed to register harness host {host_id!r}")
        return host

    def get_host(self, host_id: str) -> HarnessHost | None:
        with self._connect() as con:
            rows = _rows(
                con,
                "SELECT * FROM harness_hosts WHERE host_id = ?",
                [host_id],
            )
        return HarnessHost.from_row(rows[0]) if rows else None

    def list_hosts(self, *, status: str | None = None) -> list[HarnessHost]:
        query = "SELECT * FROM harness_hosts"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY display_name, host_id"
        with self._connect() as con:
            return [HarnessHost.from_row(row) for row in _rows(con, query, params)]

    def create_session(
        self,
        *,
        host_id: str,
        harness: str,
        command: str,
        session_id: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        branch: str | None = None,
        cwd: str | None = None,
        status: str = "created",
        started_at: datetime | None = None,
        native_session_id: str | None = None,
        native_resume_label: str | None = None,
        source_session_id: str | None = None,
        handoff_mode: str | None = None,
        mode: str = "pty",
    ) -> HarnessSession:
        now = _now()
        session_id = session_id or f"harness-{uuid4()}"
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO harness_sessions (
                  session_id, host_id, harness, repo_owner, repo_name, branch, cwd,
                  command, status, started_at, updated_at, native_session_id,
                  native_resume_label, source_session_id, handoff_mode, mode
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    session_id,
                    host_id,
                    harness,
                    repo_owner,
                    repo_name,
                    branch,
                    cwd,
                    command,
                    status,
                    started_at,
                    now,
                    native_session_id,
                    native_resume_label,
                    source_session_id,
                    handoff_mode,
                    mode,
                ],
            )
        session = self.get_session(session_id)
        if session is None:
            raise RuntimeError(f"failed to create harness session {session_id!r}")
        return session

    def get_session(self, session_id: str) -> HarnessSession | None:
        with self._connect() as con:
            rows = _rows(
                con,
                "SELECT * FROM harness_sessions WHERE session_id = ?",
                [session_id],
            )
        return HarnessSession.from_row(rows[0]) if rows else None

    def list_sessions(
        self,
        *,
        host_id: str | None = None,
        status: str | None = None,
    ) -> list[HarnessSession]:
        filters = []
        params: list[Any] = []
        if host_id is not None:
            filters.append("host_id = ?")
            params.append(host_id)
        if status is not None:
            filters.append("status = ?")
            params.append(status)
        query = "SELECT * FROM harness_sessions"
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY updated_at DESC, session_id"
        with self._connect() as con:
            return [HarnessSession.from_row(row) for row in _rows(con, query, params)]

    def update_session_status(
        self,
        session_id: str,
        status: str,
        *,
        last_error: str | None = None,
        ended_at: datetime | None = None,
        summary_session_id: str | None = None,
    ) -> HarnessSession:
        now = _now()
        with self._connect() as con:
            con.execute(
                """
                UPDATE harness_sessions
                   SET status = ?,
                       updated_at = ?,
                       ended_at = COALESCE(?, ended_at),
                       last_error = ?,
                       summary_session_id = COALESCE(?, summary_session_id)
                 WHERE session_id = ?
                """,
                [
                    status,
                    now,
                    ended_at,
                    last_error,
                    summary_session_id,
                    session_id,
                ],
            )
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(f"unknown harness session {session_id!r}")
        return session

    def update_session_activity(
        self,
        session_id: str,
        *,
        awaiting: str | None,
        last_activity: datetime | None = None,
    ) -> None:
        stamp = last_activity or _now()
        with self._connect() as con:
            con.execute(
                "UPDATE harness_sessions SET awaiting = ?, last_activity = ? "
                "WHERE session_id = ?",
                [awaiting, stamp, session_id],
            )

    def append_event(
        self,
        *,
        session_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        harness: str | None = None,
        normalized_type: str | None = None,
        normalized_source: str | None = None,
        content_preview: str | None = None,
        event_id: str | None = None,
        created_at: datetime | None = None,
        seq: int | None = None,
    ) -> HarnessEvent:
        event_id = event_id or f"harness-event-{uuid4()}"
        created_at = created_at or _now()
        normalized = normalize_harness_event(
            event_type=event_type,
            payload=payload,
            harness=harness,
            normalized_type=normalized_type,
            normalized_source=normalized_source,
            content_preview=content_preview,
        )
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO harness_events (
                  event_id, session_id, event_type, normalized_type,
                  normalized_source, content_preview, payload_json, created_at, seq
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    event_id,
                    session_id,
                    event_type,
                    normalized["normalized_type"],
                    normalized["normalized_source"],
                    normalized["content_preview"],
                    _json_dumps(payload),
                    created_at,
                    seq,
                ],
            )
        event = self.get_event(event_id)
        if event is None:
            raise RuntimeError(f"failed to append harness event {event_id!r}")
        return event

    def max_event_seq(self, session_id: str) -> int:
        with self._connect() as con:
            row = con.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM harness_events "
                "WHERE session_id = ?",
                [session_id],
            ).fetchone()
        return int(row[0] or 0)

    def list_events_after(self, session_id: str, after_seq: int) -> list[HarnessEvent]:
        with self._connect() as con:
            rows = _rows(
                con,
                "SELECT * FROM harness_events WHERE session_id = ? "
                "AND seq IS NOT NULL AND seq > ? ORDER BY seq",
                [session_id, after_seq],
            )
        return [HarnessEvent.from_row(row) for row in rows]

    def get_event(self, event_id: str) -> HarnessEvent | None:
        with self._connect() as con:
            rows = _rows(
                con,
                "SELECT * FROM harness_events WHERE event_id = ?",
                [event_id],
            )
        return HarnessEvent.from_row(rows[0]) if rows else None

    def list_events(self, session_id: str) -> list[HarnessEvent]:
        with self._connect() as con:
            return [
                HarnessEvent.from_row(row)
                for row in _rows(
                    con,
                    """
                    SELECT * FROM harness_events
                    WHERE session_id = ?
                    ORDER BY created_at, event_id
                    """,
                    [session_id],
                )
            ]

    def append_transcript_chunk(
        self,
        *,
        session_id: str,
        content_redacted: str,
        sequence: int | None = None,
        byte_count: int | None = None,
        chunk_id: str | None = None,
        created_at: datetime | None = None,
    ) -> HarnessTranscriptChunk:
        chunk_id = chunk_id or f"harness-chunk-{uuid4()}"
        created_at = created_at or _now()
        byte_count = (
            byte_count
            if byte_count is not None
            else len(content_redacted.encode("utf-8"))
        )
        with self._connect() as con:
            if sequence is None:
                sequence = con.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM harness_transcript_chunks
                    WHERE session_id = ?
                    """,
                    [session_id],
                ).fetchone()[0]
            con.execute(
                """
                INSERT INTO harness_transcript_chunks (
                  chunk_id, session_id, sequence, content_redacted, byte_count,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    chunk_id,
                    session_id,
                    sequence,
                    content_redacted,
                    byte_count,
                    created_at,
                ],
            )
        chunk = self.get_transcript_chunk(chunk_id)
        if chunk is None:
            raise RuntimeError(f"failed to append transcript chunk {chunk_id!r}")
        return chunk

    def get_transcript_chunk(self, chunk_id: str) -> HarnessTranscriptChunk | None:
        with self._connect() as con:
            rows = _rows(
                con,
                "SELECT * FROM harness_transcript_chunks WHERE chunk_id = ?",
                [chunk_id],
            )
        return HarnessTranscriptChunk.from_row(rows[0]) if rows else None

    def list_transcript_chunks(self, session_id: str) -> list[HarnessTranscriptChunk]:
        with self._connect() as con:
            return [
                HarnessTranscriptChunk.from_row(row)
                for row in _rows(
                    con,
                    """
                    SELECT * FROM harness_transcript_chunks
                    WHERE session_id = ?
                    ORDER BY sequence, created_at, chunk_id
                    """,
                    [session_id],
                )
            ]
