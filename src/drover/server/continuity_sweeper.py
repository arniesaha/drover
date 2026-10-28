"""Attributed outbox alert sweeper for continuity wakes and deliveries."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from drover.server.control_outbox import canonical_payload
from drover.server.db import control_plane_connection

UNCONSUMED_THRESHOLD_MINUTES = 10
STALE_ACK_THRESHOLD_MINUTES = 15
DEFAULT_OPERATOR_ACTOR = "arnab"


def _as_utc(dt: Any) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _resolve_responsible_actor(
    con: Any, issue_key: str, wake_row: dict[str, Any], payload: dict[str, Any]
) -> str:
    # 1. Check bound owner in continuity_owner_leases
    try:
        lease = con.execute(
            "SELECT owner_id FROM continuity_owner_leases WHERE issue_key = ?",
            [issue_key],
        ).fetchone()
        if lease and lease[0]:
            return str(lease[0])
    except Exception:
        pass

    # 2. Check owner_id on wake row
    if wake_row.get("owner_id"):
        return str(wake_row["owner_id"])

    # 3. Check payload for bound owner or launcher
    for key in ("owner_id", "authorized_owner_id", "responsible_actor", "launcher"):
        val = payload.get(key)
        if val:
            return str(val)

    # 4. Fallback to default operator / launcher
    return DEFAULT_OPERATOR_ACTOR


def _is_alert_wake(wake_row: dict[str, Any], payload: dict[str, Any]) -> bool:
    if wake_row.get("reason") == "alert" or wake_row.get("action") == "alert":
        return True
    if payload.get("reason") == "alert":
        return True
    if payload.get("alert_type") is not None:
        return True
    if str(wake_row.get("fingerprint", "")).startswith("alert:"):
        return True
    return False


def _wake_id(issue_key: str, action: str, fingerprint: str) -> str:
    return hashlib.sha256(
        canonical_payload(
            {"issue_key": issue_key, "action": action, "fingerprint": fingerprint}
        ).encode("utf-8")
    ).hexdigest()


def sweep_alerts_on_con(con: Any, now: datetime | None = None) -> list[str]:
    at = _as_utc(now) or datetime.now(timezone.utc)
    cutoff_pending = at - timedelta(minutes=UNCONSUMED_THRESHOLD_MINUTES)
    cutoff_delivered = at - timedelta(minutes=STALE_ACK_THRESHOLD_MINUTES)

    enqueued_wake_ids: list[str] = []

    # 1. Unconsumed wakes: state = 'pending' AND created_at <= cutoff_pending
    pending_cursor = con.execute(
        """SELECT wake_id, issue_key, action, fingerprint, payload_json, state,
                  owner_id, owner_epoch, attempts, next_delivery_at, created_at, delivered_at
             FROM continuity_wakes
            WHERE state = 'pending' AND created_at <= ?
            ORDER BY created_at, wake_id""",
        [cutoff_pending],
    )
    cols = [col[0] for col in pending_cursor.description]
    pending_rows = [dict(zip(cols, r)) for r in pending_cursor.fetchall()]

    for row in pending_rows:
        try:
            payload = (
                json.loads(row["payload_json"])
                if isinstance(row["payload_json"], str)
                else (row["payload_json"] or {})
            )
        except Exception:
            payload = {}

        if _is_alert_wake(row, payload):
            continue

        issue_key = str(row["issue_key"])
        stuck_wake_id = str(row["wake_id"])
        responsible = _resolve_responsible_actor(con, issue_key, row, payload)
        alert_type = "stuck_wake"
        fingerprint = hashlib.sha256(
            f"alert:{issue_key}:{alert_type}:{stuck_wake_id}".encode("utf-8")
        ).hexdigest()

        created_at_dt = _as_utc(row.get("created_at"))
        age_seconds = (
            max(0, int((at - created_at_dt).total_seconds()))
            if created_at_dt
            else UNCONSUMED_THRESHOLD_MINUTES * 60
        )

        alert_payload = {
            "alert_type": alert_type,
            "wake_id": stuck_wake_id,
            "age_seconds": age_seconds,
            "responsible_actor": responsible,
            "reason": "alert",
            "issue_key": issue_key,
        }
        alert_payload_json = canonical_payload(alert_payload)
        alert_wake_id = _wake_id(issue_key, "wake", fingerprint)

        con.execute(
            """INSERT INTO continuity_wakes
            (wake_id, issue_key, action, fingerprint, payload_json, state, owner_id, next_delivery_at, created_at)
            VALUES (?, ?, 'wake', ?, ?, 'pending', ?, ?, ?)
            ON CONFLICT DO NOTHING""",
            [
                alert_wake_id,
                issue_key,
                fingerprint,
                alert_payload_json,
                responsible,
                at,
                at,
            ],
        )
        enqueued_wake_ids.append(alert_wake_id)

    # 2. Stale acks: state = 'delivered' AND delivered_at <= cutoff_delivered
    delivered_cursor = con.execute(
        """SELECT wake_id, issue_key, action, fingerprint, payload_json, state,
                  owner_id, owner_epoch, attempts, next_delivery_at, created_at, delivered_at
             FROM continuity_wakes
            WHERE state = 'delivered' AND delivered_at <= ?
            ORDER BY delivered_at, wake_id""",
        [cutoff_delivered],
    )
    cols = [col[0] for col in delivered_cursor.description]
    delivered_rows = [dict(zip(cols, r)) for r in delivered_cursor.fetchall()]

    for row in delivered_rows:
        try:
            payload = (
                json.loads(row["payload_json"])
                if isinstance(row["payload_json"], str)
                else (row["payload_json"] or {})
            )
        except Exception:
            payload = {}

        if _is_alert_wake(row, payload):
            continue

        issue_key = str(row["issue_key"])
        stale_wake_id = str(row["wake_id"])
        consuming_owner = row.get("owner_id")
        responsible = (
            str(consuming_owner)
            if consuming_owner
            else _resolve_responsible_actor(con, issue_key, row, payload)
        )
        alert_type = "stale_ack"
        fingerprint = hashlib.sha256(
            f"alert:{issue_key}:{alert_type}:{stale_wake_id}".encode("utf-8")
        ).hexdigest()

        delivered_at_dt = _as_utc(row.get("delivered_at"))
        elapsed_seconds = (
            max(0, int((at - delivered_at_dt).total_seconds()))
            if delivered_at_dt
            else STALE_ACK_THRESHOLD_MINUTES * 60
        )

        alert_payload = {
            "alert_type": alert_type,
            "wake_id": stale_wake_id,
            "elapsed_seconds": elapsed_seconds,
            "responsible_actor": responsible,
            "reason": "alert",
            "issue_key": issue_key,
        }
        alert_payload_json = canonical_payload(alert_payload)
        alert_wake_id = _wake_id(issue_key, "wake", fingerprint)

        con.execute(
            """INSERT INTO continuity_wakes
            (wake_id, issue_key, action, fingerprint, payload_json, state, owner_id, next_delivery_at, created_at)
            VALUES (?, ?, 'wake', ?, ?, 'pending', ?, ?, ?)
            ON CONFLICT DO NOTHING""",
            [
                alert_wake_id,
                issue_key,
                fingerprint,
                alert_payload_json,
                responsible,
                at,
                at,
            ],
        )
        enqueued_wake_ids.append(alert_wake_id)

    return enqueued_wake_ids


class ContinuityAlertSweeper:
    """Attributed outbox alert sweeper for stuck wakes and stale deliveries."""

    def __init__(self, path_or_con: Any = None):
        if path_or_con is None or isinstance(path_or_con, (str, Path)):
            self.path = Path(path_or_con) if path_or_con is not None else None
            self.con = None
        else:
            self.path = None
            self.con = path_or_con

    def sweep_alerts(
        self_or_con: Any,
        con: Any = None,
        now: datetime | None = None,
    ) -> list[str]:
        if isinstance(self_or_con, ContinuityAlertSweeper):
            target_con = con if con is not None else self_or_con.con
            target_now = now
            if target_con is None:
                if self_or_con.path is None:
                    raise ValueError("Database connection or path is required")
                with control_plane_connection(self_or_con.path, timeout=5) as c:
                    c.execute("BEGIN")
                    try:
                        res = sweep_alerts_on_con(c, target_now)
                        c.execute("COMMIT")
                        return res
                    except Exception:
                        c.execute("ROLLBACK")
                        raise
            return sweep_alerts_on_con(target_con, target_now)
        # Called as ContinuityAlertSweeper.sweep_alerts(con, now) or similar
        target_con = self_or_con
        target_now = con if now is None else now
        return sweep_alerts_on_con(target_con, target_now)


def sweep_alerts(con: Any, now: datetime | None = None) -> list[str]:
    return ContinuityAlertSweeper.sweep_alerts(con, now)
