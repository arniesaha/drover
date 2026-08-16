"""Host update state is reported to the hub so refused versions are visible.

Issue #205: HarnessUpdater.status() was previously dead code. Now it tracks:
- pending_version
- update_blocked
- reason (e.g. 'smoke_test', 'not_quiescent', 'install_failed')
- observed_at (ISO timestamp)

This state is carried on the heartbeat to the central hub, persisted in
harness_hosts.update_json, and surfaced in the fleet APIs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.harness import daemon as daemon_module
from drover.server.harness.models import HarnessHost
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.schema import bootstrap_harness_tables
from drover.server.metrics import MetricsCollector


def _registry(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    return HarnessRegistry(duckdb_path)


def test_reported_update_state_survives_the_round_trip(tmp_path):
    registry = _registry(tmp_path)
    update_payload = {
        "pending_version": "0.3.0",
        "update_blocked": True,
        "reason": "smoke_test",
        "observed_at": "2026-08-16T15:00:00+00:00",
    }

    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        agent_version="0.2.0",
        update=update_payload,
    )

    host = registry.get_host("mac-mini")
    assert host is not None
    assert host.update == update_payload


def test_a_host_that_reports_no_update_stores_none(tmp_path):
    registry = _registry(tmp_path)

    registry.register_host(
        host_id="nas",
        display_name="NAS",
        kind="linux",
    )

    host = registry.get_host("nas")
    assert host is not None
    assert host.update is None


def test_subsequent_heartbeat_updates_recorded_update_state(tmp_path):
    registry = _registry(tmp_path)
    initial_update = {
        "pending_version": "0.3.0",
        "update_blocked": True,
        "reason": "smoke_test",
        "observed_at": "2026-08-16T15:00:00+00:00",
    }
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        agent_version="0.2.0",
        update=initial_update,
    )
    assert registry.get_host("mac-mini").update == initial_update

    resolved_update = {
        "pending_version": "0.3.1",
        "update_blocked": False,
        "reason": None,
        "observed_at": None,
    }
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        agent_version="0.2.0",
        update=resolved_update,
    )
    assert registry.get_host("mac-mini").update == resolved_update


def test_listed_hosts_carry_their_update_state(tmp_path):
    registry = _registry(tmp_path)
    update_mini = {
        "pending_version": "0.3.0",
        "update_blocked": True,
        "reason": "smoke_test",
        "observed_at": "2026-08-16T15:00:00+00:00",
    }
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        update=update_mini,
    )
    registry.register_host(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        update=None,
    )

    hosts_by_id = {h.host_id: h for h in registry.list_hosts()}
    assert hosts_by_id["mac-mini"].update == update_mini
    assert hosts_by_id["nas"].update is None


def test_an_existing_database_gains_update_json_column_without_losing_rows(tmp_path):
    """A database created before update_json existed gains the column cleanly."""
    db = tmp_path / "old.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("""
            CREATE TABLE harness_hosts (
              host_id VARCHAR PRIMARY KEY, display_name VARCHAR NOT NULL,
              kind VARCHAR NOT NULL, local_url VARCHAR, tailscale_url VARCHAR,
              connection_kind VARCHAR, status VARCHAR NOT NULL,
              capabilities_json VARCHAR NOT NULL, agent_version VARCHAR,
              last_seen_at TIMESTAMP,
              created_at TIMESTAMP NOT NULL DEFAULT now(),
              updated_at TIMESTAMP NOT NULL DEFAULT now()
            )
            """)
        con.execute(
            "INSERT INTO harness_hosts (host_id, display_name, kind, status, "
            "capabilities_json, agent_version) VALUES ('nas', 'NAS', 'linux', 'online', '{}', '0.2.0')"
        )

    with duckdb.connect(str(db)) as con:
        bootstrap_harness_tables(con)
        columns = {
            row[1]
            for row in con.execute("PRAGMA table_info('harness_hosts')").fetchall()
        }
        rows = con.execute(
            "SELECT host_id, agent_version, update_json FROM harness_hosts"
        ).fetchall()

    assert "update_json" in columns
    assert rows == [("nas", "0.2.0", None)]


def _collector(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    return MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )


def test_registration_endpoint_stores_and_surfaces_update_state(tmp_path):
    collector = _collector(tmp_path)
    update_payload = {
        "pending_version": "0.3.0",
        "update_blocked": True,
        "reason": "smoke_test",
        "observed_at": "2026-08-16T15:00:00+00:00",
    }

    status, body = collector.register_harness_host(
        {
            "host_id": "mac-mini",
            "display_name": "Mac Mini",
            "kind": "macos",
            "agent_version": "0.2.0",
            "update": update_payload,
        }
    )

    assert status == 200
    parsed = json.loads(body)
    assert parsed["host"]["update"] == update_payload

    # Also verified in render_harness_json
    rendered = json.loads(collector.render_harness_json())
    host_json = next(h for h in rendered["hosts"] if h["host_id"] == "mac-mini")
    assert host_json["update"] == update_payload


def test_non_dict_update_payload_is_ignored(tmp_path):
    collector = _collector(tmp_path)

    status, body = collector.register_harness_host(
        {
            "host_id": "nas",
            "display_name": "NAS",
            "kind": "linux",
            "update": "not-a-dict",
        }
    )

    assert status == 200
    stored = HarnessRegistry(tmp_path / "drover.duckdb").get_host("nas")
    assert stored.update is None


def test_register_daemon_host_remote_includes_updater_status(monkeypatch):
    sent_payload = {}

    def fake_post(state, path, payload):
        sent_payload.update(payload)
        return {"status": "ok"}

    monkeypatch.setattr(daemon_module, "_post_central_json", fake_post)

    class FakeUpdater:
        def status(self):
            return {
                "pending_version": "0.3.0",
                "update_blocked": True,
                "reason": "smoke_test",
                "observed_at": "2026-08-16T15:00:00+00:00",
            }

    state = SimpleNamespace(
        central_url="http://127.0.0.1:7080",
        host_id="test-daemon",
        display_name="Test Daemon",
        kind="macos",
        local_url="http://127.0.0.1:0",
        tailscale_url=None,
        relay=False,
        capabilities=lambda: {"pty": True},
        updater=FakeUpdater(),
    )

    res = daemon_module.register_daemon_host_remote(state)
    assert res == {"status": "ok"}
    assert sent_payload["update"] == {
        "pending_version": "0.3.0",
        "update_blocked": True,
        "reason": "smoke_test",
        "observed_at": "2026-08-16T15:00:00+00:00",
    }
