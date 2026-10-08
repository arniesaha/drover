"""Durable explicit stop intent. Lifecycle policy never dispatches a stop."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from drover.server.harness.models import TERMINAL_SESSION_STATUSES
from drover.server.harness.registry import HarnessRegistry


class LifecycleStore:
    def __init__(self, path):
        self.registry = HarnessRegistry(path)

    def request_stop(self, session_id):
        with self.registry._connect() as con:
            row = con.execute(
                "SELECT host_id, lifecycle_generation FROM harness_sessions WHERE session_id = ?",
                [session_id],
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            host, generation = row
            key = f"stop:{session_id}:{host}:{generation}"
            operation_id = hashlib.sha256(key.encode()).hexdigest()
            con.execute(
                "INSERT INTO session_lifecycle_operations "
                "(operation_id, session_id, host_id, generation, action, reason, actor, idempotency_key) "
                "VALUES (?, ?, ?, ?, 'stop', 'user', 'authenticated_user', ?) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                [operation_id, session_id, host, generation, key],
            )
        return operation_id

    def pending(self, host_id=None, session_id=None):
        with self.registry._connect() as con:
            result = con.execute(
                "SELECT o.operation_id, o.session_id, o.host_id, o.generation "
                "FROM session_lifecycle_operations o JOIN harness_sessions s "
                "ON s.session_id = o.session_id AND s.host_id = o.host_id "
                "AND s.lifecycle_generation = o.generation "
                "WHERE o.state = 'pending' AND (? IS NULL OR o.host_id = ?) "
                "AND (? IS NULL OR o.session_id = ?)",
                [host_id, host_id, session_id, session_id],
            ).fetchall()
        return result

    def attempted(self, operation_id, error=None):
        with self.registry._connect() as con:
            con.execute(
                "UPDATE session_lifecycle_operations SET attempts = attempts + 1, "
                "last_error = ?, updated_at = ? WHERE operation_id = ? AND state = 'pending'",
                [error, datetime.now(timezone.utc), operation_id],
            )

    def confirm(self, operation_id, payload):
        # A 202 or an empty/invalid response is not an acknowledgement.
        status = payload.get("status")
        if status not in TERMINAL_SESSION_STATUSES:
            return False
        if payload.get("terminated") is False:
            return False
        now = datetime.now(timezone.utc)
        with self.registry._connect() as con:
            con.execute("BEGIN")
            try:
                row = con.execute(
                    "SELECT o.session_id FROM session_lifecycle_operations o "
                    "JOIN harness_sessions s ON s.session_id = o.session_id "
                    "AND s.host_id = o.host_id AND s.lifecycle_generation = o.generation "
                    "WHERE o.operation_id = ? AND o.state = 'pending'",
                    [operation_id],
                ).fetchone()
                if row is None or payload.get("session_id") != row[0]:
                    con.execute("ROLLBACK")
                    return False
                con.execute(
                    "UPDATE harness_sessions SET status = ?, ended_at = COALESCE(ended_at, ?), "
                    "end_reason = COALESCE(end_reason, 'user'), updated_at = ? WHERE session_id = ?",
                    [status, now, now, row[0]],
                )
                con.execute(
                    "UPDATE session_lifecycle_operations SET state = 'confirmed', result_json = ?, "
                    "last_error = NULL, updated_at = ? WHERE operation_id = ?",
                    [json.dumps({"status": status}), now, operation_id],
                )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        return True
