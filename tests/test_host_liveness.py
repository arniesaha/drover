"""One derivation of host liveness (#474): online / stale / offline / retired."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from drover.schema import bootstrap
from drover.server.harness.liveness import (
    DEFAULT_OFFLINE_AFTER_SECONDS,
    DEFAULT_STALE_AFTER_SECONDS,
    OFFLINE_ENV,
    STALE_ENV,
    host_liveness,
    liveness_thresholds,
)
from drover.server.harness.models import HarnessHost
from drover.server.harness.registry import HarnessRegistry
from drover.server.metrics import MetricsCollector

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


def _host(kind="direct", age=None, status="online", retired=False):
    return HarnessHost(
        host_id="work-laptop",
        display_name="work-laptop",
        kind="macos",
        status=status,
        connection_kind=kind,
        last_seen_at=None if age is None else NOW - timedelta(seconds=age),
        retired_at=NOW if retired else None,
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(STALE_ENV, raising=False)
    monkeypatch.delenv(OFFLINE_ENV, raising=False)


@pytest.mark.parametrize("kind", ["direct", "relay"])
def test_state_follows_heartbeat_age_for_every_connection_kind(kind):
    def state(age):
        return host_liveness(_host(kind, age), now=NOW).state

    assert state(10) == "online"
    assert state(DEFAULT_STALE_AFTER_SECONDS) == "online"
    assert state(DEFAULT_STALE_AFTER_SECONDS + 1) == "stale"
    assert state(DEFAULT_OFFLINE_AFTER_SECONDS) == "stale"
    assert state(DEFAULT_OFFLINE_AFTER_SECONDS + 1) == "offline"
    assert state(6 * 86400) == "offline"


def test_relay_host_that_went_dark_is_not_online():
    # The work-laptop gap: relay hosts were exempt from staleness entirely.
    assert host_liveness(_host("relay", 120), now=NOW).state == "stale"
    assert host_liveness(_host("relay", 6 * 86400), now=NOW).state == "offline"


def test_unresponsive_relay_socket_vetoes_a_fresh_heartbeat():
    host = _host("relay", 5)
    assert host_liveness(host, now=NOW, relay_responsive=False).state == "offline"
    assert host_liveness(host, now=NOW, relay_responsive=True).state == "online"
    # The veto is for relay hosts only.
    assert (
        host_liveness(_host("direct", 5), now=NOW, relay_responsive=False).state
        == "online"
    )


def test_retired_and_stored_offline_win_and_never_heartbeat_is_unknown():
    assert host_liveness(_host(age=1, retired=True), now=NOW).state == "retired"
    assert host_liveness(_host(age=1, status="offline"), now=NOW).state == "offline"
    assert host_liveness(_host(age=None), now=NOW).state == "online"


def test_thresholds_are_configurable_and_sanitised(monkeypatch):
    assert liveness_thresholds() == (45.0, 600.0)
    monkeypatch.setenv(STALE_ENV, "10")
    monkeypatch.setenv(OFFLINE_ENV, "20")
    assert liveness_thresholds() == (10.0, 20.0)
    assert host_liveness(_host(age=15), now=NOW).state == "stale"
    assert host_liveness(_host(age=25), now=NOW).state == "offline"
    monkeypatch.setenv(STALE_ENV, "nope")
    monkeypatch.setenv(OFFLINE_ENV, "-3")
    assert liveness_thresholds() == (45.0, 600.0)
    # Offline can never be tighter than stale.
    monkeypatch.setenv(STALE_ENV, "100")
    monkeypatch.setenv(OFFLINE_ENV, "50")
    assert liveness_thresholds() == (100.0, 100.0)


def test_naive_local_last_seen_against_aware_now():
    local = datetime.now()
    aware = datetime.now(timezone.utc)

    def host(age):
        return HarnessHost(
            host_id="h",
            display_name="h",
            kind="linux",
            status="online",
            last_seen_at=local - timedelta(seconds=age),
        )

    for now in (None, aware):
        assert host_liveness(host(5), now=now).state == "online"
        assert host_liveness(host(120), now=now).state == "stale"
        assert host_liveness(host(3600), now=now).state == "offline"


def test_snapshot_reports_derived_liveness_for_relay_hosts(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="work-laptop",
        display_name="work-laptop",
        kind="macos",
        connection_kind="relay",
    )
    dark = datetime.now() - timedelta(days=6)
    with registry._connect() as con:
        con.execute("UPDATE harness_hosts SET last_seen_at = ?", [dark])

    class _Relay:
        def is_responsive(self, host_id):
            return True  # attached and chatty, but no heartbeat for days

    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
        relay_manager=_Relay(),
    )
    host = json.loads(collector.render_harness_json())["hosts"][0]

    assert host["liveness"] == host["status"] == "offline"
    assert host["heartbeat_age_seconds"] > 5 * 86400
    assert host["stale_after_seconds"] == 45.0
    assert host["offline_after_seconds"] == 600.0
