"""Registry helpers for Drover harness hosts and sessions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb

from drover.server.db import control_plane_connection
from drover.server.harness.models import (
    HarnessEvent,
    HarnessEventPage,
    HarnessHost,
    HarnessSession,
)
from drover.server.harness.auth import redact_auth_text
from drover.server.harness.events import normalize_harness_event

_SESSION_PREVIEW_CANDIDATE_LIMIT = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_dumps(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def _looks_like_traceback(value: str) -> bool:
    lowered = value.lower()
    return (
        "traceback (most recent call last)" in lowered
        or lowered.startswith("stack trace")
        or "\n  file " in lowered
    )


def _rows(
    con: duckdb.DuckDBPyConnection, query: str, params: list[Any]
) -> list[dict[str, Any]]:
    result = con.execute(query, params)
    cols = [desc[0] for desc in result.description]
    return [dict(zip(cols, row)) for row in result.fetchall()]


class HarnessRegistry:
    """Small DuckDB-backed registry for Drover command-plane state."""

    def __init__(self, duckdb_path: str | Path):
        self.duckdb_path = Path(duckdb_path)

    @contextmanager
    def _connect(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Yield the control plane's connection for this database.

        This registry *is* the control plane -- ``/harness``,
        ``/harness/hosts`` and ``/harness/sessions`` are all calls on it --
        so every window goes through ``control_plane_connection``: its own
        lock, and on the hub server its own pinned connection, neither of
        them reachable by an analytical reader (issue #95).

        Windows are still serialized against each other, which is what the
        process-wide connect lock used to buy here: two threads racing
        ``duckdb.connect()`` on one file raise BinderException ("Unique file
        handle conflict"), and DuckDB's Python connection is not safe for
        concurrent use either. Existing ``with self._connect() as con:`` call
        sites work unchanged.
        """
        with control_plane_connection(self.duckdb_path) as con:
            yield con

    def register_host(
        self,
        *,
        host_id: str,
        display_name: str,
        kind: str,
        local_url: str | None = None,
        tailscale_url: str | None = None,
        connection_kind: str = "direct",
        capabilities: dict[str, Any] | None = None,
        status: str = "online",
    ) -> HarnessHost:
        now = _now()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO harness_hosts (
                  host_id, display_name, kind, local_url, tailscale_url,
                  connection_kind, status, capabilities_json, last_seen_at,
                  created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                  display_name = excluded.display_name,
                  kind = excluded.kind,
                  local_url = excluded.local_url,
                  tailscale_url = excluded.tailscale_url,
                  connection_kind = excluded.connection_kind,
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
                    connection_kind,
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
        permission_mode: str | None = None,
        model: str | None = None,
        thinking_effort: str | None = None,
    ) -> HarnessSession:
        now = _now()
        started_at = started_at or now
        session_id = session_id or f"harness-{uuid4()}"
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO harness_sessions (
                  session_id, host_id, harness, repo_owner, repo_name, branch, cwd,
                  command, status, started_at, updated_at, native_session_id,
                  native_resume_label, source_session_id, handoff_mode, mode,
                  permission_mode, model, thinking_effort
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    permission_mode,
                    model,
                    thinking_effort,
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

    def mark_session_recovered(
        self, session_id: str, native_session_id: str
    ) -> HarnessSession:
        now = _now()
        with self._connect() as con:
            con.execute(
                """
                UPDATE harness_sessions
                   SET status = 'running',
                       updated_at = ?,
                       ended_at = NULL,
                       last_error = NULL,
                       awaiting = 'input',
                       native_session_id = ?
                 WHERE session_id = ?
                """,
                [now, native_session_id, session_id],
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

    def update_session_native_id(self, session_id: str, native_session_id: str) -> None:
        native_session_id = native_session_id.strip()
        if not native_session_id:
            return
        with self._connect() as con:
            con.execute(
                "UPDATE harness_sessions "
                "SET native_session_id = ?, updated_at = ? "
                "WHERE session_id = ?",
                [native_session_id, _now(), session_id],
            )

    def update_session_preferences(
        self,
        session_id: str,
        *,
        model: str | None = None,
        thinking_effort: str | None = None,
    ) -> None:
        if model is None and thinking_effort is None:
            return
        assignments = ["updated_at = ?"]
        params: list[Any] = [_now()]
        if model is not None:
            assignments.append("model = ?")
            params.append(model)
        if thinking_effort is not None:
            assignments.append("thinking_effort = ?")
            params.append(thinking_effort)
        params.append(session_id)
        with self._connect() as con:
            con.execute(
                f"UPDATE harness_sessions SET {', '.join(assignments)} WHERE session_id = ?",
                params,
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

    def append_events_if_new(self, records: list[dict[str, Any]]) -> int:
        """Insert many events in ONE connection window, skipping known ids.

        ``append_event`` is convenient but expensive: it opens a connection to
        insert and another to read the row back, and callers that dedupe first
        open a third. Every one of those windows holds this database's
        process-wide connect lock, contended with fleet renders and event
        ingestion from every host.

        The terminal mirror is the caller that cannot afford it -- it runs per
        PTY message at burst rates -- so it hands whole batches here and pays
        one window for all of them. Returns the number of rows inserted;
        ``event_id`` collisions (with the table or within the batch) are
        skipped, which is what makes replaying a message stream idempotent.
        """
        unique: dict[str, dict[str, Any]] = {}
        for record in records:
            event_id = str(record.get("event_id") or "").strip()
            if event_id and event_id not in unique:
                unique[event_id] = record
        if not unique:
            return 0
        with self._connect() as con:
            placeholders = ", ".join("?" for _ in unique)
            existing = {
                row[0]
                for row in con.execute(
                    "SELECT event_id FROM harness_events "
                    f"WHERE event_id IN ({placeholders})",
                    list(unique),
                ).fetchall()
            }
            params = []
            for event_id, record in unique.items():
                if event_id in existing:
                    continue
                normalized = normalize_harness_event(
                    event_type=record["event_type"],
                    payload=record.get("payload"),
                    harness=record.get("harness"),
                    normalized_type=record.get("normalized_type"),
                    normalized_source=record.get("normalized_source"),
                    content_preview=record.get("content_preview"),
                )
                params.append(
                    [
                        event_id,
                        record["session_id"],
                        record["event_type"],
                        normalized["normalized_type"],
                        normalized["normalized_source"],
                        normalized["content_preview"],
                        _json_dumps(record.get("payload")),
                        record.get("created_at") or _now(),
                        None,
                    ]
                )
            if not params:
                return 0
            con.executemany(
                """
                INSERT INTO harness_events (
                  event_id, session_id, event_type, normalized_type,
                  normalized_source, content_preview, payload_json, created_at, seq
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
        return len(params)

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

    def list_event_page(
        self,
        session_id: str,
        *,
        after_seq: int | None = None,
        before_seq: int | None = None,
        through_seq: int | None = None,
        limit: int | None = None,
    ) -> HarnessEventPage:
        page_limit = limit or 200
        with self._connect() as con:
            if through_seq is None:
                row = con.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM harness_events "
                    "WHERE session_id = ?",
                    [session_id],
                ).fetchone()
                max_seq = int(row[0] or 0)
            else:
                max_seq = through_seq

            if after_seq is not None:
                rows = _rows(
                    con,
                    "SELECT * FROM harness_events WHERE session_id = ? "
                    "AND seq > ? AND seq <= ? ORDER BY seq ASC LIMIT ?",
                    [session_id, after_seq, max_seq, page_limit + 1],
                )
                has_newer = len(rows) > page_limit
                rows = rows[:page_limit]
                has_older = after_seq > 0
            else:
                upper_bound = before_seq if before_seq is not None else max_seq + 1
                rows = _rows(
                    con,
                    "SELECT * FROM ("
                    "SELECT * FROM harness_events WHERE session_id = ? "
                    "AND seq IS NOT NULL AND seq > 0 AND seq < ? "
                    "ORDER BY seq DESC LIMIT ?"
                    ") page ORDER BY seq ASC",
                    [session_id, upper_bound, page_limit + 1],
                )
                has_older = len(rows) > page_limit
                if has_older:
                    rows = rows[1:]
                has_newer = before_seq is not None and before_seq <= max_seq

        events = [HarnessEvent.from_row(row) for row in rows]
        sequences = [event.seq for event in events if event.seq is not None]
        return HarnessEventPage(
            events=events,
            page_min_seq=min(sequences) if sequences else None,
            page_max_seq=max(sequences) if sequences else None,
            max_seq=max_seq,
            has_older=has_older,
            has_newer=has_newer,
        )

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

    def latest_session_previews(self, session_ids: list[str]) -> dict[str, str]:
        session_ids = [session_id for session_id in session_ids if session_id]
        if not session_ids:
            return {}
        placeholders = ", ".join("?" for _ in session_ids)
        with self._connect() as con:
            rows = _rows(
                con,
                f"""
                SELECT session_id, event_type, content_preview, payload_json
                FROM (
                  SELECT session_id,
                         event_type,
                         content_preview,
                         payload_json,
                         row_number() OVER (
                           PARTITION BY session_id
                           ORDER BY CASE event_type
                                      WHEN 'user_input' THEN 0
                                      WHEN 'terminal.input' THEN 1
                                      ELSE 2
                                    END,
                                    COALESCE(seq, 0) DESC,
                                    created_at DESC,
                                    event_id DESC
                         ) AS rn
                  FROM harness_events
                  WHERE session_id IN ({placeholders})
                    AND event_type IN ('user_input', 'assistant_output', 'terminal.input')
                )
                WHERE rn <= ?
                ORDER BY session_id, rn
                """,
                [*session_ids, _SESSION_PREVIEW_CANDIDATE_LIMIT],
            )
        previews: dict[str, str] = {}
        for row in rows:
            session_id = str(row.get("session_id") or "")
            if not session_id or session_id in previews:
                continue
            preview = self._session_event_preview(row)
            if preview:
                previews[session_id] = preview
        return previews

    @staticmethod
    def _session_event_preview(row: dict[str, Any]) -> str:
        stored_preview = str(row.get("content_preview") or "").strip()
        if stored_preview:
            return HarnessRegistry._safe_session_preview(stored_preview)
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, ValueError):
            return ""
        if not isinstance(payload, dict):
            return ""
        for key in ("text", "content", "summary", "message", "error", "command"):
            value = payload.get(key)
            if value:
                normalized = normalize_harness_event(
                    event_type=str(row.get("event_type") or ""),
                    payload=payload,
                    content_preview=str(value),
                )
                return HarnessRegistry._safe_session_preview(
                    normalized["content_preview"]
                )
        return ""

    @staticmethod
    def _safe_session_preview(value: str) -> str:
        preview = redact_auth_text(value).strip()
        if _looks_like_traceback(preview):
            return ""
        return preview

    # Session conversation lives entirely in harness_events, for both PTY and
    # structured sessions. PTY output arrives as terminal.output events; the
    # separate transcript-chunk table it used to be duplicated into is gone
    # (the two held byte-identical text).
    _TRANSCRIPT_EVENT_ROLES = {
        "user_input": "user",
        "assistant_output": "assistant",
        "tool_action": "tool",
        "tool_result": "tool-result",
        "terminal.output": "terminal",
    }

    def transcript_text(self, session_id: str, *, limit: int = 200) -> str:
        """Best-effort readable transcript for a session.

        Returns "" when the session has no content-bearing events.
        """
        with self._connect() as con:
            rows = _rows(
                con,
                """
                SELECT event_type, payload_json
                FROM harness_events
                WHERE session_id = ? AND event_type IN
                      ('user_input', 'assistant_output', 'tool_action',
                       'tool_result', 'terminal.output')
                ORDER BY COALESCE(seq, 0), created_at, event_id
                """,
                [session_id],
            )
        lines: list[str] = []
        for row in rows[-limit:]:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except (TypeError, ValueError):
                continue
            text = str(payload.get("text") or "").strip()
            if not text:
                continue
            label = self._TRANSCRIPT_EVENT_ROLES.get(str(row.get("event_type")), "note")
            lines.append(f"[{label}] {text}")
        return "\n".join(lines).strip()
