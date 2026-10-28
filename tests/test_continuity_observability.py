import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from drover.schema import bootstrap_control_plane_store
from drover.server.continuity_hub import ContinuityHub
from drover.server.continuity_sweeper import ContinuityAlertSweeper
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


def test_alert_unconsumed_wake_attribution(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    issue_key = "arniesaha/drover#821"

    with control_plane_connection(path) as con:
        # 1. Lease bound to an owner
        con.execute(
            """INSERT INTO continuity_owner_leases
            (issue_key, owner_id, owner_epoch, lease_until)
            VALUES (?, 'agent:coder:main', 1, ?)""",
            [issue_key, now + timedelta(minutes=30)],
        )
        # 2. Unconsumed wake pending for 11 minutes (> 10m threshold)
        con.execute(
            """INSERT INTO continuity_wakes
            (wake_id, issue_key, action, fingerprint, payload_json, state, created_at, next_delivery_at)
            VALUES ('wake-stuck-1', ?, 'wake', 'fp-stuck', '{"reason": "review"}', 'pending', ?, ?)""",
            [issue_key, now - timedelta(minutes=11), now - timedelta(minutes=11)],
        )
        # 3. Recent unconsumed wake pending for only 5 minutes (<= 10m threshold)
        con.execute(
            """INSERT INTO continuity_wakes
            (wake_id, issue_key, action, fingerprint, payload_json, state, created_at, next_delivery_at)
            VALUES ('wake-fresh-1', ?, 'wake', 'fp-fresh', '{"reason": "review"}', 'pending', ?, ?)""",
            [issue_key, now - timedelta(minutes=5), now - timedelta(minutes=5)],
        )

    sweeper = ContinuityAlertSweeper(path)
    with control_plane_connection(path) as con:
        alert_ids = sweeper.sweep_alerts(con, now=now)

    assert len(alert_ids) == 1

    with control_plane_connection(path) as con:
        row = con.execute(
            """SELECT wake_id, issue_key, action, fingerprint, payload_json, state, owner_id
                 FROM continuity_wakes WHERE wake_id = ?""",
            [alert_ids[0]],
        ).fetchone()

    assert row is not None
    assert row[1] == issue_key
    assert row[2] == "wake"
    assert row[5] == "pending"
    assert row[6] == "agent:coder:main"

    payload = json.loads(row[4])
    assert payload["alert_type"] == "stuck_wake"
    assert payload["responsible_actor"] == "agent:coder:main"
    assert payload["wake_id"] == "wake-stuck-1"
    assert payload["reason"] == "alert"
    assert payload["age_seconds"] >= 660


def test_alert_stale_ack_attribution(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    issue_key = "arniesaha/drover#821"

    with control_plane_connection(path) as con:
        # Delivered wake unacknowledged for 16 minutes (> 15m threshold)
        con.execute(
            """INSERT INTO continuity_wakes
            (wake_id, issue_key, action, fingerprint, payload_json, state,
             owner_id, owner_epoch, delivery_token, attempts, created_at, delivered_at, next_delivery_at)
            VALUES ('wake-delivered-1', ?, 'wake', 'fp-deliv', '{"reason": "review"}', 'delivered',
                    'agent:coder:subworker-1', 1, 'tok-1', 1, ?, ?, ?)""",
            [
                issue_key,
                now - timedelta(minutes=20),
                now - timedelta(minutes=16),
                now + timedelta(minutes=5),
            ],
        )
        # Delivered wake unacknowledged for only 10 minutes (<= 15m threshold)
        con.execute(
            """INSERT INTO continuity_wakes
            (wake_id, issue_key, action, fingerprint, payload_json, state,
             owner_id, owner_epoch, delivery_token, attempts, created_at, delivered_at, next_delivery_at)
            VALUES ('wake-delivered-fresh', ?, 'wake', 'fp-deliv-fresh', '{"reason": "review"}', 'delivered',
                    'agent:coder:subworker-1', 1, 'tok-2', 1, ?, ?, ?)""",
            [
                issue_key,
                now - timedelta(minutes=12),
                now - timedelta(minutes=10),
                now + timedelta(minutes=5),
            ],
        )

    with control_plane_connection(path) as con:
        alert_ids = ContinuityAlertSweeper.sweep_alerts(con, now=now)

    assert len(alert_ids) == 1

    with control_plane_connection(path) as con:
        row = con.execute(
            """SELECT wake_id, issue_key, action, fingerprint, payload_json, state, owner_id
                 FROM continuity_wakes WHERE wake_id = ?""",
            [alert_ids[0]],
        ).fetchone()

    assert row is not None
    assert row[1] == issue_key
    assert row[2] == "wake"
    assert row[5] == "pending"
    assert row[6] == "agent:coder:subworker-1"

    payload = json.loads(row[4])
    assert payload["alert_type"] == "stale_ack"
    assert payload["responsible_actor"] == "agent:coder:subworker-1"
    assert payload["wake_id"] == "wake-delivered-1"
    assert payload["reason"] == "alert"
    assert payload["elapsed_seconds"] >= 960


def test_alert_recursion_prevented(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    issue_key = "arniesaha/drover#821"

    with control_plane_connection(path) as con:
        # Existing alert wake in pending state older than 10 minutes
        con.execute(
            """INSERT INTO continuity_wakes
            (wake_id, issue_key, action, fingerprint, payload_json, state,
             owner_id, created_at, next_delivery_at)
            VALUES ('alert-wake-1', ?, 'wake', 'fp-alert-1',
                    '{"alert_type": "stuck_wake", "reason": "alert", "responsible_actor": "agent:coder:main"}',
                    'pending', 'agent:coder:main', ?, ?)""",
            [issue_key, now - timedelta(minutes=20), now - timedelta(minutes=20)],
        )
        # Existing alert wake in delivered state older than 15 minutes
        con.execute(
            """INSERT INTO continuity_wakes
            (wake_id, issue_key, action, fingerprint, payload_json, state,
             owner_id, delivery_token, attempts, created_at, delivered_at, next_delivery_at)
            VALUES ('alert-wake-2', ?, 'wake', 'fp-alert-2',
                    '{"alert_type": "stale_ack", "reason": "alert", "responsible_actor": "agent:coder:main"}',
                    'delivered', 'agent:coder:main', 'tok-3', 1, ?, ?, ?)""",
            [
                issue_key,
                now - timedelta(minutes=30),
                now - timedelta(minutes=25),
                now + timedelta(minutes=5),
            ],
        )

    with control_plane_connection(path) as con:
        alert_ids = ContinuityAlertSweeper.sweep_alerts(con, now=now)
        total_wakes = con.execute("SELECT count(*) FROM continuity_wakes").fetchone()[0]

    assert alert_ids == []
    assert total_wakes == 2


def test_dashboard_json_continuity_row():
    dashboard_path = (
        Path(__file__).resolve().parent.parent
        / "deploy"
        / "grafana"
        / "drover-pipeline-observatory.json"
    )
    with open(dashboard_path, "r", encoding="utf-8") as f:
        dashboard = json.load(f)

    assert dashboard["title"] == "Drover Pipeline Observatory"
    panels = dashboard.get("panels", [])

    # Find the row panel
    row_panels = [
        p
        for p in panels
        if p.get("type") == "row" and p.get("title") == "Continuity Health & Latency"
    ]
    assert len(row_panels) == 1

    panel_titles = {p.get("title") for p in panels}
    expected_titles = {
        "Continuity Health & Latency",
        "Pending Wakes by Age",
        "Wake-to-Consume & Consume-to-Ack Latency",
        "Active Owner Leases & Epochs",
        "Attributed Continuity Alerts",
    }
    assert expected_titles.issubset(panel_titles)

    # Check datasource bindings
    for panel in panels:
        if panel.get("type") != "row":
            assert panel.get("datasource", {}).get("type") == "prometheus"
