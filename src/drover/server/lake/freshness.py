"""Control-store freshness only; never opens the analytical lake."""

from datetime import datetime, timezone

from drover.server.db import control_plane_connection

LAG_SECONDS = 30
STALL_SECONDS = 120


def read_freshness(path):
    with control_plane_connection(path, timeout=2) as control:
        control.execute("BEGIN READ ONLY")
        try:
            # LOCAL avoids changing timeout policy for the next pool borrower.
            control.execute("SET LOCAL statement_timeout = '2s'")
            last, count, oldest, pending = control.execute(
                """SELECT x.last_success, b.count, b.oldest, p.oldest
                FROM (SELECT max(acknowledged_at) AS last_success
                      FROM lake_export_batches) x
                CROSS JOIN (SELECT count(*) AS count, min(created_at) AS oldest
                            FROM control_outbox_batches
                            WHERE acknowledged_at IS NULL) b
                CROSS JOIN (SELECT min(committed_at) AS oldest
                            FROM control_outbox_events WHERE state='pending') p"""
            ).fetchone()
            control.execute("COMMIT")
        except BaseException:
            control.execute("ROLLBACK")
            raise
    return {
        "last_successful_export_at": last.isoformat() if last else None,
        "unacknowledged_batches": count,
        "oldest_unacknowledged_at": oldest.isoformat() if oldest else None,
        "oldest_pending_at": pending.isoformat() if pending else None,
        "freshness_error": None,
    }


def freshness_status(snapshot, *, running, last_error=None, restarts=0, now=None):
    now = now or datetime.now(timezone.utc)

    def age(value):
        return (
            max(0, (now - datetime.fromisoformat(value)).total_seconds())
            if value
            else 0
        )

    batch_age = age(snapshot.get("oldest_unacknowledged_at"))
    oldest_age = max(batch_age, age(snapshot.get("oldest_pending_at")))
    state = (
        "stopped"
        if not running
        else (
            "stalled"
            if snapshot.get("freshness_error") or oldest_age >= STALL_SECONDS
            else "lagging" if oldest_age >= LAG_SECONDS else "ok"
        )
    )
    action = {
        "ok": "No action needed.",
        "lagging": "Wait for the exporter to catch up; check again shortly.",
        "stalled": "Check exporter logs and control/catalog connectivity; supervised recovery is bounded.",
        "stopped": "Correct the exporter error, then restart the hub to reset the recovery budget.",
    }[state]
    return {
        "last_successful_export_at": None,
        "unacknowledged_batches": None,
        "oldest_unacknowledged_at": None,
        "oldest_pending_at": None,
        "freshness_error": None,
        **snapshot,
        "state": state,
        "running": running,
        "enabled": running,
        "oldest_unacknowledged_age_seconds": batch_age,
        "oldest_outstanding_age_seconds": oldest_age,
        "last_error": last_error,
        "restart_count": restarts,
        "recovery_action": action,
    }
