"""Bounded read model for continuity operational health."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from drover.server.db import control_plane_connection


class ContinuityStatusService:
    """Project durable continuity health without process-local state."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

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
            claims = self._rows(
                con.execute("""SELECT wake_id, goal_id, owner_id, run_id, session_id,
                       client_turn_id, state, claim_until, created_at
                    FROM continuity_owner_turn_claims
                    WHERE state IN ('claimed', 'sent')
                    ORDER BY created_at, wake_id""")
            )
            attempts = con.execute("""SELECT coalesce(sum(i.attempts), 0)
                FROM factory_observer_inbox i
                JOIN factory_observer_runs r ON r.run_id = i.run_id
                WHERE r.authorized_owner_id LIKE 'drover:owner/%'""").fetchone()[0]
            dead_letters = con.execute("""SELECT count(*), min(i.received_at)
                FROM factory_observer_inbox i
                JOIN factory_observer_runs r ON r.run_id = i.run_id
                WHERE i.state = 'exhausted'
                AND r.authorized_owner_id LIKE 'drover:owner/%'""").fetchone()
            incomplete_receipts = self._rows(
                con.execute("""SELECT wake_id, action_fingerprint, run_id, owner_id,
                       owner_epoch, created_at
                    FROM continuity_owner_action_intents
                    WHERE state = 'intent' AND owner_id LIKE 'drover:owner/%'
                    ORDER BY created_at, wake_id, action_fingerprint""")
            )

        def iso(value):
            return (
                value.astimezone(timezone.utc).isoformat()
                if isinstance(value, datetime)
                else value
            )

        def age_seconds(value):
            return max(0, round((at - value).total_seconds())) if value else 0

        active_claims = [
            {
                **claim,
                "claim_until": iso(claim["claim_until"]),
                "created_at": iso(claim["created_at"]),
                "stuck": claim["claim_until"] <= at,
            }
            for claim in claims
        ]

        return {
            "version": 1,
            "pending_wakes": {
                "total": int(pending[0] or 0),
                "under_2m": int(pending[1] or 0),
                "between_2m_and_10m": int(pending[2] or 0),
                "over_10m": int(pending[3] or 0),
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
            "native_owner": {
                "active_turn_claims": active_claims,
                "attempts": int(attempts or 0),
                "dead_lettered_wakes": {
                    "count": int(dead_letters[0] or 0),
                    "oldest_age_seconds": age_seconds(dead_letters[1]),
                },
                "incomplete_receipts": [
                    {**receipt, "created_at": iso(receipt["created_at"])}
                    for receipt in incomplete_receipts
                ],
            },
            "active_alerts": [],
        }
