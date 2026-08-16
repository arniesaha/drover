"""Registry helpers for Drover harness hosts and sessions."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb

from drover.server.db import control_plane_connection, control_plane_path
from drover.server.harness.auth import redact_auth_text
from drover.server.harness.events import normalize_harness_event
from drover.server.harness.model_catalog import CatalogEnvelope
from drover.server.harness.models import (
    HarnessEvent,
    HarnessEventPage,
    HarnessHost,
    HarnessSession,
)
from drover.server.harness.recap_jobs import (
    LiveRecap,
    enqueue_live_recap,
    latest_live_recaps,
)

_SESSION_PREVIEW_CANDIDATE_LIMIT = 5
_MODEL_CATALOG_CACHE_MAX_BYTES = 512 * 1024
_MODEL_CATALOG_SCOPES_PER_HARNESS = 2

#: Statuses that mean a session is finished and may therefore be capped out of
#: a listing. Deliberately an allowlist rather than "not running": statuses are
#: written from several places, and a new one appearing must not make a real
#: session silently invisible. Anything unrecognised counts as live.
ARCHIVED_SESSION_STATUSES: tuple[str, ...] = (
    "completed",
    "terminated",
    "errored",
    "failed",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


#: Awaiting values that mean the agent is blocked on the user, and therefore
#: worth a notification. Anything else (including None) is a clear.
_AWAITING_STATES = ("input", "approval")


def _derive_structured_awaiting(
    *, event_type: str, payload: dict[str, Any], current: str | None
) -> str | None:
    """Apply the structured-session awaiting state machine to one event."""
    if event_type == "approval_prompt":
        return "approval"
    if event_type in {"approval_response", "user_input"}:
        return None
    if event_type == "status" and payload.get("awaiting") == "input":
        return "input"
    return current


def _dispatch_awaiting_push(
    *,
    session_id: str,
    awaiting: str | None,
    harness: str | None,
    cwd: str | None,
    preview: str = "",
) -> None:
    """Tell the push layer a session changed awaiting state.

    Imported lazily and wrapped: push is an optional, best-effort add-on, and
    nothing about recording harness activity may fail because APNs is
    misconfigured, unreachable, or not installed.
    """
    try:
        from drover.server.push import AwaitingTransition, dispatch_awaiting_transition

        dispatch_awaiting_transition(
            AwaitingTransition(
                session_id=session_id,
                harness=harness or "",
                cwd=cwd,
                awaiting=awaiting,
                preview=preview,
            )
        )
    except Exception:  # noqa: BLE001 - never break activity recording
        pass


def _json_dumps(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def _looks_like_traceback(value: str) -> bool:
    lowered = value.lower()
    return (
        "traceback (most recent call last)" in lowered
        or lowered.startswith("stack trace")
        or "\n  file " in lowered
    )


def _is_turn_completion_payload(payload: dict[str, Any]) -> bool:
    """Accept both legacy flat payloads and StructuredMessage wire envelopes."""
    if payload.get("turn_complete") is True:
        return True
    inner = payload.get("payload")
    return isinstance(inner, dict) and inner.get("turn_complete") is True


def _supports_live_recaps(mode: str | None, harness: str) -> bool:
    return mode == "structured" or (mode is None and harness != "shell")


def _enqueue_recap_if_completion(
    con: duckdb.DuckDBPyConnection,
    *,
    session_id: str,
    event_type: str,
    payload: dict[str, Any] | None,
    seq: int | None,
) -> bool:
    """Queue a recap only for a completed turn from a structured session."""
    if (
        event_type != "status"
        or not payload
        or not _is_turn_completion_payload(payload)
        or not isinstance(seq, int)
        or isinstance(seq, bool)
    ):
        return False
    session = con.execute(
        "SELECT mode, harness FROM harness_sessions WHERE session_id = ?",
        [session_id],
    ).fetchone()
    if session is None:
        return False
    mode, harness = session
    if not _supports_live_recaps(mode, harness):
        return False
    return enqueue_live_recap(con, session_id, seq)


def _enqueue_latest_stored_completion(
    con: duckdb.DuckDBPyConnection, session_id: str
) -> bool:
    """Recover the newest completion that arrived before session metadata."""
    rows = con.execute(
        """SELECT payload_json, seq
             FROM harness_events
            WHERE session_id = ? AND event_type = 'status' AND seq IS NOT NULL
            ORDER BY seq DESC, created_at DESC""",
        [session_id],
    ).fetchall()
    for payload_json, seq in rows:
        try:
            payload = json.loads(payload_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not _is_turn_completion_payload(payload):
            continue
        return _enqueue_recap_if_completion(
            con,
            session_id=session_id,
            event_type="status",
            payload=payload,
            seq=seq,
        )
    return False


def _rows(
    con: duckdb.DuckDBPyConnection, query: str, params: list[Any]
) -> list[dict[str, Any]]:
    result = con.execute(query, params)
    cols = [desc[0] for desc in result.description]
    return [dict(zip(cols, row)) for row in result.fetchall()]


class HarnessRegistry:
    """Small DuckDB-backed registry for Drover command-plane state.

    ``duckdb_path`` is the *lakehouse* path every caller already has; the
    registry resolves its own store from it (``control_plane_path``) and never
    opens the lakehouse itself. Passing the control-plane path directly also
    works -- the resolution is idempotent.
    """

    def __init__(self, duckdb_path: str | Path):
        self.duckdb_path = Path(duckdb_path)
        self.control_plane_path = control_plane_path(duckdb_path)

    @contextmanager
    def _connect(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Yield the control plane's connection for this database.

        This registry *is* the control plane -- ``/harness``,
        ``/harness/hosts`` and ``/harness/sessions`` are all calls on it --
        so every window goes through ``control_plane_connection``: its own
        database file, its own lock, and on the hub server its own pinned
        connection, none of them reachable by an analytical reader (#95).

        The file is the part that matters. PR #104 gave this path its own lock
        and the wedge recurred on ``c7900e7`` anyway, because a connection to
        ``drover.duckdb`` joins ``drover.duckdb``'s DuckDB instance whatever
        lock opened it -- one scheduler, one buffer manager, one
        ``memory_limit`` shared with every parquet scan in the process.

        Windows are still serialized against each other: two threads racing
        ``duckdb.connect()`` on one file raise BinderException ("Unique file
        handle conflict"), and DuckDB's Python connection is not safe for
        concurrent use either. Existing ``with self._connect() as con:`` call
        sites work unchanged.
        """
        with control_plane_connection(self.control_plane_path) as con:
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
        agent_version: str | None = None,
    ) -> HarnessHost:
        now = _now()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO harness_hosts (
                  host_id, display_name, kind, local_url, tailscale_url,
                  connection_kind, status, capabilities_json, agent_version,
                  last_seen_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                  display_name = excluded.display_name,
                  kind = excluded.kind,
                  local_url = excluded.local_url,
                  tailscale_url = excluded.tailscale_url,
                  connection_kind = excluded.connection_kind,
                  status = excluded.status,
                  capabilities_json = excluded.capabilities_json,
                  agent_version = excluded.agent_version,
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
                    agent_version,
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

    def save_model_catalog(
        self,
        host_id: str,
        harness: str,
        scope_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Persist one validated live catalog in the host's bounded LKG cache."""
        with self._connect() as con:
            row = con.execute(
                "SELECT model_catalogs_json FROM harness_hosts WHERE host_id = ?",
                [host_id],
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown harness host: {host_id}")

            envelope = CatalogEnvelope.from_wire(payload, host_id, harness)
            if envelope.stale:
                raise ValueError("only non-stale model catalogs may be saved")
            if (
                envelope.account_scope_id is None
                or envelope.account_scope_id != scope_id
            ):
                raise ValueError("model catalog account scope does not match scope_id")

            try:
                catalogs = json.loads(row[0])
            except (TypeError, json.JSONDecodeError):
                catalogs = {}
            if not isinstance(catalogs, dict):
                catalogs = {}

            raw_entry = catalogs.get(harness)
            entry = raw_entry if isinstance(raw_entry, dict) else {}
            raw_scopes = entry.get("scopes")
            scopes = raw_scopes if isinstance(raw_scopes, dict) else {}
            scopes = dict(scopes)
            scopes.pop(scope_id, None)
            scopes[scope_id] = envelope.to_wire()
            while len(scopes) > _MODEL_CATALOG_SCOPES_PER_HARNESS:
                del scopes[next(iter(scopes))]
            catalogs[harness] = {
                "latest_scope_id": scope_id,
                "scopes": scopes,
            }
            serialized = _json_dumps(catalogs)
            if len(serialized.encode("utf-8")) > _MODEL_CATALOG_CACHE_MAX_BYTES:
                raise ValueError("model catalog cache exceeds 512 KiB")
            con.execute(
                "UPDATE harness_hosts SET model_catalogs_json = ?, updated_at = ? "
                "WHERE host_id = ?",
                [serialized, _now(), host_id],
            )

    def latest_model_catalog(self, host_id: str, harness: str) -> dict[str, Any] | None:
        """Return a validated copy of the latest persisted catalog, if any."""
        with self._connect() as con:
            row = con.execute(
                "SELECT model_catalogs_json FROM harness_hosts WHERE host_id = ?",
                [host_id],
            ).fetchone()
        if row is None:
            return None
        try:
            catalogs = json.loads(row[0])
            entry = catalogs[harness]
            latest_scope_id = entry["latest_scope_id"]
            payload = entry["scopes"][latest_scope_id]
            envelope = CatalogEnvelope.from_wire(payload, host_id, harness)
        except (KeyError, TypeError, json.JSONDecodeError, ValueError):
            return None
        if envelope.stale or envelope.account_scope_id != latest_scope_id:
            return None
        return envelope.to_wire()

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
            con.execute("BEGIN TRANSACTION")
            try:
                con.execute(
                    """
                    INSERT INTO harness_sessions (
                      session_id, host_id, harness, repo_owner, repo_name, branch, cwd,
                      command, status, started_at, updated_at, native_session_id,
                      native_resume_label, source_session_id, handoff_mode, mode,
                      permission_mode, model, thinking_effort,
                      recap_reconcile_needed
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        _supports_live_recaps(mode, harness),
                    ],
                )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            con.execute("BEGIN TRANSACTION")
            try:
                _enqueue_latest_stored_completion(con, session_id)
                con.execute(
                    "UPDATE harness_sessions SET recap_reconcile_needed = FALSE "
                    "WHERE session_id = ?",
                    [session_id],
                )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
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

    def reconcile_orphan_completions(self, *, limit: int = 100) -> int:
        """Retry derived recap reconciliation left pending by session creation."""
        with self._connect() as con:
            con.execute("BEGIN TRANSACTION")
            try:
                rows = con.execute(
                    """SELECT s.session_id
                         FROM harness_sessions s
                        WHERE s.recap_reconcile_needed
                        ORDER BY s.updated_at, s.session_id
                        LIMIT ?""",
                    [max(1, int(limit))],
                ).fetchall()
                enqueued = 0
                for (session_id,) in rows:
                    enqueued += _enqueue_latest_stored_completion(con, str(session_id))
                    con.execute(
                        "UPDATE harness_sessions "
                        "SET recap_reconcile_needed = FALSE "
                        "WHERE session_id = ?",
                        [session_id],
                    )
                con.execute("COMMIT")
                return enqueued
            except Exception:
                con.execute("ROLLBACK")
                raise

    def list_sessions(
        self,
        *,
        host_id: str | None = None,
        status: str | None = None,
        archived_limit: int | None = None,
    ) -> list[HarnessSession]:
        """List sessions, optionally keeping only the newest archived ones.

        Every fleet poll returned every session that had ever run -- 115 of
        120 were `terminated` when this was added, and the list only grows.
        ``archived_limit`` bounds the finished ones while leaving live
        sessions untouched: not being able to see a running session is a far
        worse failure than a long list, so the cap only ever applies to
        statuses known to be terminal, and anything unrecognised counts as
        live.
        """
        filters = []
        params: list[Any] = []
        if host_id is not None:
            filters.append("host_id = ?")
            params.append(host_id)
        if status is not None:
            filters.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(filters)) if filters else ""
        order = " ORDER BY updated_at DESC, session_id"

        if archived_limit is None:
            query = f"SELECT * FROM harness_sessions{where}{order}"
        else:
            # Rank archived rows among themselves so the cap cannot consume
            # the budget a live session would have occupied.
            placeholders = ", ".join("?" for _ in ARCHIVED_SESSION_STATUSES)
            query = f"""
                SELECT * FROM (
                  SELECT *,
                         CASE WHEN status IN ({placeholders})
                              THEN row_number() OVER (
                                     PARTITION BY status IN ({placeholders})
                                     ORDER BY updated_at DESC, session_id
                                   )
                         END AS _archived_rank
                    FROM harness_sessions{where}
                )
                 WHERE _archived_rank IS NULL OR _archived_rank <= ?
                {order}
            """
            statuses = list(ARCHIVED_SESSION_STATUSES)
            params = statuses + statuses + params + [max(0, int(archived_limit))]

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
            # Read the prior value inside the same window as the write: this
            # is the one chokepoint both the local emit() path and the remote
            # /harness/events ingest path funnel through, so a transition seen
            # here is seen exactly once however the event arrived.
            previous = con.execute(
                "SELECT awaiting, harness, cwd FROM harness_sessions "
                "WHERE session_id = ?",
                [session_id],
            ).fetchone()
            con.execute(
                "UPDATE harness_sessions SET awaiting = ?, last_activity = ? "
                "WHERE session_id = ?",
                [awaiting, stamp, session_id],
            )
            # Only a real change notifies. A harness that re-emits "still
            # awaiting input" every few seconds must not produce a banner every
            # few seconds, and that dedup belongs here rather than in the
            # sender: the state machine is what knows the difference.
            changed = previous is not None and previous[0] != awaiting
            # Read what the agent last said while the window is still open,
            # rather than paying a second serialized _connect() for it. Only
            # for a transition that will actually alert -- a clear costs
            # nothing extra.
            preview = (
                self._attention_preview(con, session_id)
                if changed and awaiting in _AWAITING_STATES
                else ""
            )
        if not changed:
            return
        _dispatch_awaiting_push(
            session_id=session_id,
            awaiting=awaiting,
            harness=previous[1],
            cwd=previous[2],
            preview=preview,
        )

    @staticmethod
    def _attention_preview(con: duckdb.DuckDBPyConnection, session_id: str) -> str:
        """The agent's last words, for the body of a "needs you" alert.

        "claude-code needs you" tells you nothing you could act on; what the
        agent actually just said is the whole reason to pick the phone up. The
        newest ``assistant_output`` is that text, and at the moment of an
        awaiting transition it is always present -- unlike a live recap, which
        a worker generates asynchronously and may not have written yet.

        Rows whose ``content_preview`` is just the event type are the harness's
        placeholder for a thinking-only turn with no visible text; they are
        skipped rather than shown, so a few candidates are fetched instead of
        one.
        """
        rows = con.execute(
            """
            SELECT content_preview, payload_json, event_type
              FROM harness_events
             WHERE session_id = ?
               AND event_type = 'assistant_output'
             ORDER BY COALESCE(seq, 0) DESC, created_at DESC, event_id DESC
             LIMIT ?
            """,
            [session_id, _SESSION_PREVIEW_CANDIDATE_LIMIT],
        ).fetchall()
        for content_preview, payload_json, event_type in rows:
            candidate = str(content_preview or "").strip()
            if not candidate or candidate == str(event_type or ""):
                continue
            # Same redaction and traceback filtering the session list uses --
            # this text leaves the host for Apple's servers, so a leaked token
            # here would be worse than anywhere else it is shown.
            safe = HarnessRegistry._safe_session_preview(candidate)
            if safe:
                return safe
        return ""

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
            con.execute("BEGIN TRANSACTION")
            try:
                # An event already here is a re-delivery, not an error. The
                # host daemon retains undelivered batches and re-offers them
                # (#101), so a delivery whose acknowledgement was lost arrives
                # again with the same event_id -- and the mirror path's "have
                # I got this one?" check is a check-then-act that two
                # concurrent deliveries both pass. That raised 195
                # duplicate-key tracebacks in a single server log, every one
                # for an event the hub already held.
                #
                # DO NOTHING rather than an upsert: the stored copy is the one
                # the app has already read, and a replay carries no promise of
                # identical derived fields.
                inserted = (
                    con.execute(
                        """
                        INSERT INTO harness_events (
                          event_id, session_id, event_type, normalized_type,
                          normalized_source, content_preview, payload_json,
                          created_at, seq
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (event_id) DO NOTHING
                        RETURNING event_id
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
                    ).fetchone()
                    is not None
                )
                if inserted:
                    # Only for a genuinely new event: a re-delivered
                    # completion must not enqueue a second recap for work
                    # that was already summarised.
                    _enqueue_recap_if_completion(
                        con,
                        session_id=session_id,
                        event_type=event_type,
                        payload=payload,
                        seq=seq,
                    )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
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
            inserted_records: list[tuple[dict[str, Any], int | None]] = []
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
                seq = record.get("seq")
                if not isinstance(seq, int) or isinstance(seq, bool):
                    seq = None
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
                        seq,
                    ]
                )
                inserted_records.append((record, seq))
            if not params:
                return 0
            con.execute("BEGIN TRANSACTION")
            try:
                # Same reasoning as the single-event path above: the SELECT
                # into `existing` narrows the batch, but it is still a
                # check-then-act, and two batches carrying the same
                # re-delivered event both pass it.
                con.executemany(
                    """
                    INSERT INTO harness_events (
                      event_id, session_id, event_type, normalized_type,
                      normalized_source, content_preview, payload_json, created_at, seq
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (event_id) DO NOTHING
                    """,
                    params,
                )
                for record, seq in inserted_records:
                    _enqueue_recap_if_completion(
                        con,
                        session_id=record["session_id"],
                        event_type=record["event_type"],
                        payload=record.get("payload"),
                        seq=seq,
                    )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        return len(params)

    def ingest_structured_events(self, records: list[dict[str, Any]]) -> int:
        """Atomically insert remote structured events and project session state.

        Central replay is idempotent by ``event_id``. Event rows and their
        derived ``awaiting``/activity/native-session projection share one
        transaction so a crash can never commit one without the other.
        """
        unique: dict[str, dict[str, Any]] = {}
        for record in records:
            event_id = str(record.get("event_id") or "").strip()
            if event_id and event_id not in unique:
                unique[event_id] = record
        if not unique:
            return 0

        notifications: list[tuple[str, str | None, str | None, str | None, str]] = []
        inserted_count = 0
        with self._connect() as con:
            con.execute("BEGIN TRANSACTION")
            try:
                placeholders = ", ".join("?" for _ in unique)
                existing_ids = {
                    str(row[0])
                    for row in con.execute(
                        "SELECT event_id FROM harness_events "
                        f"WHERE event_id IN ({placeholders})",
                        list(unique),
                    ).fetchall()
                }
                incoming = [
                    record
                    for event_id, record in unique.items()
                    if event_id not in existing_ids
                ]
                if not incoming:
                    con.execute("COMMIT")
                    return 0

                session_ids = sorted({str(record["session_id"]) for record in incoming})
                session_placeholders = ", ".join("?" for _ in session_ids)
                session_rows = {
                    str(row["session_id"]): row
                    for row in _rows(
                        con,
                        "SELECT session_id, awaiting, last_activity, "
                        "native_session_id, harness, cwd "
                        "FROM harness_sessions "
                        f"WHERE session_id IN ({session_placeholders})",
                        session_ids,
                    )
                }
                existing_max = {
                    str(row["session_id"]): int(row["max_seq"] or 0)
                    for row in _rows(
                        con,
                        "SELECT session_id, COALESCE(MAX(seq), 0) AS max_seq "
                        "FROM harness_events "
                        f"WHERE session_id IN ({session_placeholders}) "
                        "GROUP BY session_id",
                        session_ids,
                    )
                }

                inserted_by_session: dict[str, list[dict[str, Any]]] = {}
                for record in incoming:
                    event_id = str(record["event_id"])
                    session_id = str(record["session_id"])
                    event_type = str(record["event_type"])
                    payload = (
                        dict(record["payload"])
                        if isinstance(record.get("payload"), dict)
                        else {}
                    )
                    seq = record.get("seq")
                    if not isinstance(seq, int) or isinstance(seq, bool):
                        seq = None
                    created_at = record.get("created_at") or _now()
                    normalized = normalize_harness_event(
                        event_type=event_type,
                        payload=payload,
                        normalized_source="structured",
                    )
                    inserted = con.execute(
                        """
                        INSERT INTO harness_events (
                          event_id, session_id, event_type, normalized_type,
                          normalized_source, content_preview, payload_json,
                          created_at, seq
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (event_id) DO NOTHING
                        RETURNING event_id
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
                    ).fetchone()
                    if inserted is None:
                        continue
                    _enqueue_recap_if_completion(
                        con,
                        session_id=session_id,
                        event_type=event_type,
                        payload=payload,
                        seq=seq,
                    )
                    inserted_count += 1
                    inserted_by_session.setdefault(session_id, []).append(
                        {
                            "event_id": event_id,
                            "event_type": event_type,
                            "payload": payload,
                            "created_at": created_at,
                            "seq": seq,
                        }
                    )

                for session_id, inserted_events in inserted_by_session.items():
                    previous = session_rows.get(session_id)
                    if previous is None:
                        continue
                    rebuild = any(
                        isinstance(event.get("seq"), int)
                        and event["seq"] <= existing_max.get(session_id, 0)
                        for event in inserted_events
                    )
                    if rebuild:
                        projection_events = _rows(
                            con,
                            "SELECT event_id, event_type, payload_json, "
                            "created_at, seq FROM harness_events "
                            "WHERE session_id = ? AND seq IS NOT NULL AND seq > 0 "
                            "ORDER BY seq, created_at, event_id",
                            [session_id],
                        )
                        awaiting: str | None = None
                    else:
                        projection_events = sorted(
                            inserted_events,
                            key=lambda event: (
                                event.get("seq") or 0,
                                event.get("created_at") or _now(),
                                event["event_id"],
                            ),
                        )
                        awaiting = previous.get("awaiting")

                    latest_activity: datetime | None = None
                    native_session_id: str | None = None
                    for event in projection_events:
                        raw_payload = event.get("payload")
                        if raw_payload is None:
                            try:
                                raw_payload = json.loads(
                                    event.get("payload_json") or "{}"
                                )
                            except (TypeError, ValueError):
                                raw_payload = {}
                        inner_payload = (
                            raw_payload.get("payload")
                            if isinstance(raw_payload, dict)
                            else None
                        )
                        if not isinstance(inner_payload, dict):
                            inner_payload = {}
                        awaiting = _derive_structured_awaiting(
                            event_type=str(event["event_type"]),
                            payload=inner_payload,
                            current=awaiting,
                        )
                        candidate_native_id = inner_payload.get("native_session_id")
                        if (
                            isinstance(candidate_native_id, str)
                            and candidate_native_id.strip()
                        ):
                            native_session_id = candidate_native_id.strip()
                        created_at = event.get("created_at")
                        if created_at is not None and (
                            latest_activity is None or created_at > latest_activity
                        ):
                            latest_activity = created_at

                    con.execute(
                        """
                        UPDATE harness_sessions
                           SET awaiting = ?,
                               last_activity = CASE
                                 WHEN ? IS NULL THEN last_activity
                                 WHEN last_activity IS NULL OR last_activity < ? THEN ?
                                 ELSE last_activity
                               END,
                               native_session_id = COALESCE(?, native_session_id)
                         WHERE session_id = ?
                        """,
                        [
                            awaiting,
                            latest_activity,
                            latest_activity,
                            latest_activity,
                            native_session_id,
                            session_id,
                        ],
                    )
                    if previous.get("awaiting") != awaiting:
                        preview = (
                            self._attention_preview(con, session_id)
                            if awaiting in _AWAITING_STATES
                            else ""
                        )
                        notifications.append(
                            (
                                session_id,
                                awaiting,
                                previous.get("harness"),
                                previous.get("cwd"),
                                preview,
                            )
                        )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

        for session_id, awaiting, harness, cwd, preview in notifications:
            _dispatch_awaiting_push(
                session_id=session_id,
                awaiting=awaiting,
                harness=harness,
                cwd=cwd,
                preview=preview,
            )
        return inserted_count

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

    def latest_live_recaps(self, session_ids: list[str]) -> dict[str, LiveRecap]:
        """Return the durable recap projection for the requested sessions."""
        session_ids = [session_id for session_id in session_ids if session_id]
        if not session_ids:
            return {}
        with self._connect() as con:
            return latest_live_recaps(con, session_ids)

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

    def list_events_for_reconciliation(
        self,
        *,
        host_id: str | None = None,
        after_created_at: datetime | None = None,
        after_event_id: str | None = None,
        limit: int = 100,
    ) -> list[HarnessEvent]:
        """Return one durable page of structured events for reconciliation.

        The cursor is the final ``(created_at, event_id)`` pair from the prior
        page. No age window or total-row cap is applied: an old interior gap
        is still a gap, and callers page until the local ledger is exhausted.
        """
        page_limit = max(1, int(limit))
        cursor_event_id = after_event_id or ""
        with self._connect() as con:
            rows = _rows(
                con,
                """
                SELECT e.*
                FROM harness_events e
                LEFT JOIN harness_sessions s ON e.session_id = s.session_id
                WHERE (
                    e.normalized_source = 'structured'
                    OR s.mode = 'structured'
                    OR (e.seq IS NOT NULL AND e.normalized_source IS NULL)
                  )
                  AND (? IS NULL OR s.host_id IS NULL OR s.host_id = ?)
                  AND (
                    ? IS NULL
                    OR e.created_at > ?
                    OR (e.created_at = ? AND e.event_id > ?)
                  )
                ORDER BY e.created_at ASC, e.event_id ASC
                LIMIT ?
                """,
                [
                    host_id,
                    host_id,
                    after_created_at,
                    after_created_at,
                    after_created_at,
                    cursor_event_id,
                    page_limit,
                ],
            )
        return [HarnessEvent.from_row(row) for row in rows]

    def reconcile_unsent_events(
        self,
        pusher: Any,
        *,
        host_id: str | None = None,
        batch_size: int = 100,
    ) -> int | None:
        """Helper to reconcile unsent events to central via pusher."""
        from drover.server.harness.structured.pusher import reconcile_unsent_events

        return reconcile_unsent_events(
            self,
            pusher,
            host_id=host_id,
            batch_size=batch_size,
        )
