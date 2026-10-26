"""Bounded read model for continuity operational health."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from drover.server.db import control_plane_connection


class ContinuityStatusService:
    """Collect lightweight latency samples and project durable wake state."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._latencies = {
            "wake_to_consume": deque(maxlen=1000),
            "consume_to_ack": deque(maxlen=1000),
        }

    def record_transition(self, kind: str, latency_ms: int) -> None:
        if kind not in self._latencies or type(latency_ms) is not int or latency_ms < 0:
            raise ValueError("bounded continuity latency is required")
        self._latencies[kind].append(latency_ms)

    @staticmethod
    def _summary(values: deque[int]) -> dict[str, int]:
        if not values:
            return {"avg": 0, "p50": 0, "p95": 0}
        ordered = sorted(values)
        percentile = lambda fraction: ordered[
            min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5))
        ]
        return {
            "avg": round(sum(ordered) / len(ordered)),
            "p50": percentile(0.50),
            "p95": percentile(0.95),
        }

    @staticmethod
    def _rows(cursor) -> list[dict[str, Any]]:
        return [
            dict(zip((col[0] for col in cursor.description), row))
            for row in cursor.fetchall()
        ]

    def get_status(self, now: datetime | None = None) -> dict[str, Any]:
        at = now or datetime.now(timezone.utc)
        if at.tzinfo is None:
            raise ValueError("now must include a timezone")
        under, middle = at - timedelta(minutes=2), at - timedelta(minutes=10)
        with control_plane_connection(self.path, timeout=5) as con:
            pending = con.execute(
                """SELECT count(*) AS total,
                    sum(CASE WHEN created_at > ? THEN 1 ELSE 0 END) AS under_2m,
                    sum(CASE WHEN created_at <= ? AND created_at > ? THEN 1 ELSE 0 END) AS between_2m_and_10m,
                    sum(CASE WHEN created_at <= ? THEN 1 ELSE 0 END) AS over_10m
                    FROM continuity_wakes WHERE state = 'pending'""",
                [under, under, middle, middle],
            ).fetchone()
            leases = self._rows(
                con.execute(
                    """SELECT issue_key, owner_id, owner_epoch, lease_until
                   FROM continuity_owner_leases WHERE lease_until > ?
                   ORDER BY issue_key""",
                    [at],
                )
            )
            heartbeats = self._rows(
                con.execute("""SELECT session_key, owner_id, last_poll_at, status,
                          consecutive_failures, last_error, latency_ms
                   FROM continuity_plugin_heartbeats ORDER BY session_key""")
            )
            has_worker_states = con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = 'harness_worker_states'"
            ).fetchone()[0]
            idle = (
                con.execute(
                    "SELECT count(*) FROM harness_worker_states WHERE state = 'working' AND declared = false"
                ).fetchone()[0]
                if has_worker_states
                else 0
            )
            redelivered = con.execute(
                "SELECT count(*) FROM continuity_wakes WHERE attempts > 1 AND state <> 'acknowledged'"
            ).fetchone()[0]

        def iso(value):
            return value.isoformat() if isinstance(value, datetime) else value

        return {
            "version": 1,
            "pending_wakes": {
                "total": int(pending[0] or 0),
                "under_2m": int(pending[1] or 0),
                "between_2m_and_10m": int(pending[2] or 0),
                "over_10m": int(pending[3] or 0),
            },
            "latencies": {
                "sample_count": sum(len(values) for values in self._latencies.values()),
                "wake_to_consume_ms": self._summary(self._latencies["wake_to_consume"]),
                "consume_to_ack_ms": self._summary(self._latencies["consume_to_ack"]),
            },
            "active_leases": [
                {
                    **row,
                    "lease_until": iso(row["lease_until"]),
                    "remaining_seconds": max(
                        0, round((row["lease_until"] - at).total_seconds())
                    ),
                }
                for row in leases
            ],
            "idle_undeclared_sessions": int(idle),
            "redelivered_wakes_count": int(redelivered),
            "plugin_heartbeats": [
                {**row, "last_poll_at": iso(row["last_poll_at"])} for row in heartbeats
            ],
            "active_alerts": [],
        }
