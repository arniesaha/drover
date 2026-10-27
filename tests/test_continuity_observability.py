from datetime import datetime, timezone

from drover.schema import bootstrap_control_plane_store
from drover.server.continuity_hub import ContinuityHub
from drover.server.db import control_plane_connection
from drover.server.mcp.tools import drover_continuity_status
from drover.server.metrics import _append_continuity_metrics


def test_status_empty_store(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)

    status = ContinuityHub(path).observability_status(
        now=datetime(2026, 10, 26, tzinfo=timezone.utc)
    )

    assert status == {
        "version": 1,
        "pending_wakes": {
            "total": 0,
            "under_2m": 0,
            "between_2m_and_10m": 0,
            "over_10m": 0,
        },
        "active_leases": [],
        "idle_undeclared_sessions": 0,
        "redelivered_wakes_count": 0,
        "native_owner": {
            "active_turn_claims": [],
            "attempts": 0,
            "dead_lettered_wakes": {"count": 0, "oldest_age_seconds": 0},
            "incomplete_receipts": [],
        },
        "active_alerts": [],
    }
    with control_plane_connection(path) as con:
        assert con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = 'continuity_plugin_heartbeats'"
        ).fetchone() == (0,)


def test_status_projects_durable_native_owner_state(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    with control_plane_connection(path) as con:
        con.execute(
            """INSERT INTO factory_observer_runs
            (run_id, session_id, expected_revision, objective, checkpoint,
             authority_scope, authorized_owner_id, updated_at)
            VALUES ('run-1', 'session-1', 1, 'objective', 'checkpoint',
                    'implementation', 'drover:owner/goal-1', ?)""",
            [now],
        )
        con.execute(
            """INSERT INTO factory_observer_inbox
            (event_id, run_id, source, subject, sequence, payload_json, state,
             attempts, next_delivery_at, received_at)
            VALUES ('dead-wake', 'run-1', 'test', 'subject', 1, '{}', 'exhausted',
                    3, ?, ?)""",
            [now, now.replace(hour=10)],
        )
        con.execute(
            """INSERT INTO factory_observer_inbox
            (event_id, run_id, source, subject, sequence, payload_json, state,
             attempts, next_delivery_at, received_at)
            VALUES ('active-wake', 'run-1', 'test', 'subject', 2, '{}', 'delivered',
                    2, ?, ?)""",
            [now, now],
        )
        con.execute(
            """INSERT INTO continuity_owner_turn_claims
            (wake_id, goal_id, owner_id, run_id, session_id, client_turn_id, state,
             claim_until, created_at, updated_at)
            VALUES ('active-wake', 'goal-1', 'drover:owner/goal-1', 'run-1',
                    'session-1', 'turn-1', 'sent', ?, ?, ?)""",
            [now.replace(hour=11), now.replace(hour=10), now.replace(hour=10)],
        )
        con.execute(
            """INSERT INTO continuity_owner_action_intents
            (wake_id, action_fingerprint, run_id, owner_id, owner_epoch, state,
             artifact, created_at)
            VALUES ('active-wake', 'action-1', 'run-1', 'drover:owner/goal-1',
                    2, 'intent', NULL, ?)""",
            [now.replace(hour=10)],
        )

    status = ContinuityHub(path).observability_status(now=now)

    assert status["native_owner"] == {
        "active_turn_claims": [
            {
                "wake_id": "active-wake",
                "goal_id": "goal-1",
                "owner_id": "drover:owner/goal-1",
                "run_id": "run-1",
                "session_id": "session-1",
                "client_turn_id": "turn-1",
                "state": "sent",
                "claim_until": "2026-10-26T11:00:00+00:00",
                "created_at": "2026-10-26T10:00:00+00:00",
                "stuck": True,
            }
        ],
        "attempts": 5,
        "dead_lettered_wakes": {"count": 1, "oldest_age_seconds": 7200},
        "incomplete_receipts": [
            {
                "wake_id": "active-wake",
                "action_fingerprint": "action-1",
                "run_id": "run-1",
                "owner_id": "drover:owner/goal-1",
                "owner_epoch": 2,
                "created_at": "2026-10-26T10:00:00+00:00",
            }
        ],
    }


def test_status_metrics_export_dead_letter_and_stuck_owner_turn(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    now = datetime.now(timezone.utc)
    with control_plane_connection(path) as con:
        con.execute(
            """INSERT INTO factory_observer_runs
            (run_id, session_id, expected_revision, objective, checkpoint,
             authority_scope, authorized_owner_id, updated_at)
            VALUES ('run-1', 'session-1', 1, 'objective', 'checkpoint',
                    'implementation', 'drover:owner/goal-1', ?)""",
            [now],
        )
        con.execute(
            """INSERT INTO factory_observer_inbox
            (event_id, run_id, source, subject, sequence, payload_json, state,
             attempts, received_at)
            VALUES ('dead-wake', 'run-1', 'test', 'subject', 1, '{}', 'exhausted',
                    3, ?)""",
            [now],
        )
        con.execute(
            """INSERT INTO continuity_owner_turn_claims
            (wake_id, goal_id, owner_id, run_id, session_id, client_turn_id, state,
             claim_until, created_at, updated_at)
            VALUES ('stuck-wake', 'goal-1', 'drover:owner/goal-1', 'run-1',
                    'session-1', 'turn-1', 'sent', ?, ?, ?)""",
            [now.replace(year=now.year - 1), now, now],
        )

    lines: list[str] = []
    _append_continuity_metrics(lines, path)

    assert "drover_continuity_dead_letter 1" in lines
    assert "drover_continuity_stuck_owner_turn 1" in lines


def test_mcp_status_read_matches_hub_projection(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)

    assert drover_continuity_status(duckdb_path=path)["pending_wakes"]["total"] == 0
