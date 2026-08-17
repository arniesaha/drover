"""Host update state is reported to the hub so refused versions are visible.

Issue #205: HarnessUpdater.status() was previously dead code. Now it tracks:
- pending_version -- what we are waiting to activate, if anything
- blocked_version -- what a refusal is about; a failed install clears
  pending_version so the next beat retries, and "blocked, on nothing" is
  not something an operator can act on
- update_blocked
- reason (e.g. 'smoke_test', 'not_quiescent', 'install_failed')
- observed_at (ISO timestamp) -- when the current refusal began, not when it
  was last re-checked, so "refusing for half an hour" is answerable

This state is carried on the heartbeat to the central hub, persisted in
harness_hosts.update_json, and surfaced in the fleet APIs. update_json is a
single JSON blob column, so adding a key inside it needs no migration.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import duckdb
import pytest

from drover.config import default_config
from drover.schema import bootstrap
from drover.server.harness import daemon as daemon_module
from drover.server.harness.models import HarnessHost
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.schema import bootstrap_harness_tables
from drover.server.harness.updater import HostUpdater
from drover.server.metrics import MetricsCollector
from drover.server.runtime import RuntimeLayout


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
                "blocked_version": "0.3.0",
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
        "blocked_version": "0.3.0",
        "update_blocked": True,
        "reason": "smoke_test",
        "observed_at": "2026-08-16T15:00:00+00:00",
    }


def test_a_host_with_updates_switched_off_reports_no_update_state(monkeypatch):
    """[update] off leaves `updater` None, and that must not be an error."""
    sent_payload = {}
    monkeypatch.setattr(
        daemon_module,
        "_post_central_json",
        lambda state, path, payload: sent_payload.update(payload) or {"status": "ok"},
    )
    state = SimpleNamespace(
        central_url="http://127.0.0.1:7080",
        host_id="test-daemon",
        display_name="Test Daemon",
        kind="macos",
        local_url="http://127.0.0.1:0",
        tailscale_url=None,
        relay=False,
        capabilities=lambda: {"pty": True},
        updater=None,
    )

    assert daemon_module.register_daemon_host_remote(state) == {"status": "ok"}
    assert sent_payload["update"] is None


# --- the real updater on the real heartbeat path ------------------------------
#
# The tests above drive a fake updater, so they pin the wiring but cannot
# catch a mismatch between what HostUpdater.status() returns and what the
# registration payload, the registry and the hub model do with it. These use
# the real class end to end, which is exactly the gap that let an added key
# break test_daemon_can_register_host_with_central_server unnoticed.


def _installed(layout, version):
    binary = layout.version_dir(version) / "bin" / "drover-server"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)


def _idle_state():
    return SimpleNamespace(
        structured=SimpleNamespace(session_ids=lambda: [], is_alive=lambda s: False),
        pty=SimpleNamespace(list_sessions=lambda: ["t1"]),  # busy: never activates
    )


def _real_updater(tmp_path):
    layout = RuntimeLayout(tmp_path / "home")
    _installed(layout, "0.1.3")
    layout.flip("0.1.3")

    def install_broken(lay, artifact, **kwargs):
        binary = lay.version_dir(artifact.version) / "bin" / "drover-server"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        binary.chmod(0o755)
        return True

    return HostUpdater(
        _idle_state(),
        layout,
        default_config(),
        installer=install_broken,
        restarter=lambda: None,
    )


def test_a_real_refusal_reaches_the_hub_row_through_one_heartbeat(
    tmp_path, monkeypatch
):
    """A real HostUpdater's status, registered by the real hub endpoint."""
    collector = _collector(tmp_path)
    updater = _real_updater(tmp_path)
    state = SimpleNamespace(
        central_url="http://127.0.0.1:7080",
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        local_url="http://127.0.0.1:7081",
        tailscale_url=None,
        relay=False,
        capabilities=lambda: {"pty": True},
        updater=updater,
        registered_at_least_once=False,
        pusher=None,
    )

    # The hub answers each beat with the target it is publishing; the daemon
    # hands the body straight back to the updater.
    published: dict = {"target_version": "0.1.4"}

    def fake_post(_state, _path, payload):
        collector.register_harness_host(payload)
        return dict(published)

    monkeypatch.setattr(daemon_module, "_post_central_json", fake_post)

    daemon_module._heartbeat_once(state)  # beat 1: installs, fails smoke test
    daemon_module._heartbeat_once(state)  # beat 2: reports the refusal

    stored = HarnessRegistry(tmp_path / "drover.duckdb").get_host("mac-mini")
    assert stored is not None
    assert stored.update is not None
    assert stored.update["update_blocked"] is True
    assert stored.update["reason"] == "smoke_test"
    assert stored.update["pending_version"] == "0.1.4"
    assert stored.update["blocked_version"] == "0.1.4"
    refused_at = stored.update["observed_at"]
    assert refused_at is not None

    # Beats keep arriving; the refusal must not look newer each time.
    daemon_module._heartbeat_once(state)
    stored = HarnessRegistry(tmp_path / "drover.duckdb").get_host("mac-mini")
    assert stored.update["observed_at"] == refused_at

    # The operator pulls the bad release. The retraction is seen on the beat
    # after it is published (a beat registers before it reads the reply), and
    # reported on the one after that -- not latched until a daemon restart,
    # which is what used to happen.
    published.clear()
    daemon_module._heartbeat_once(state)  # sees the retraction, clears state
    daemon_module._heartbeat_once(state)  # reports the cleared state

    stored = HarnessRegistry(tmp_path / "drover.duckdb").get_host("mac-mini")
    assert stored.update == {
        "pending_version": None,
        "blocked_version": None,
        "update_blocked": False,
        "reason": None,
        "observed_at": None,
    }


def test_the_status_shape_the_daemon_sends_is_the_shape_the_model_reads(tmp_path):
    """Every key HostUpdater emits survives to HarnessHost.update, unchanged."""
    registry = _registry(tmp_path)
    updater = _real_updater(tmp_path)
    updater.observe({"target_version": "0.1.4", "artifact": {}})
    updater.maybe_activate()
    status = updater.status()

    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        update=status,
    )

    assert registry.get_host("mac-mini").update == status
    assert set(status) == {
        "pending_version",
        "blocked_version",
        "update_blocked",
        "reason",
        "observed_at",
    }
