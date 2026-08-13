"""The hub has to keep the version each host reports, not just receive it.

#132 added `agent_version` to the harnessd registration payload so the hub
could "see version skew across the fleet without asking". The producer landed
and the consumer did not: `register_host` never took the field, no column held
it, and it was absent from every API response. Every host has been sending its
version into a hole.

That leaves a rollout with no way to answer the only question that matters
while it is running -- which hosts have actually moved.
"""

from __future__ import annotations

from drover.schema import bootstrap
from drover.server.harness.registry import HarnessRegistry


def _registry(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    return HarnessRegistry(duckdb_path)


def test_a_reported_version_survives_the_round_trip(tmp_path):
    registry = _registry(tmp_path)

    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="macos",
        agent_version="0.1.2",
    )

    assert registry.get_host("mac-mini").agent_version == "0.1.2"


def test_a_host_that_reports_no_version_stores_none(tmp_path):
    """Hosts on an older build simply omit it; that is not an error."""
    registry = _registry(tmp_path)

    registry.register_host(host_id="nas", display_name="NAS", kind="linux")

    assert registry.get_host("nas").agent_version is None


def test_upgrading_a_host_updates_the_recorded_version(tmp_path):
    """The whole point: this is what shows a rollout actually converging."""
    registry = _registry(tmp_path)
    registry.register_host(
        host_id="nas", display_name="NAS", kind="linux", agent_version="0.1.2"
    )

    registry.register_host(
        host_id="nas", display_name="NAS", kind="linux", agent_version="0.1.3"
    )

    assert registry.get_host("nas").agent_version == "0.1.3"


def test_listed_hosts_carry_their_versions(tmp_path):
    registry = _registry(tmp_path)
    registry.register_host(
        host_id="mac-mini", display_name="Mac", kind="macos", agent_version="0.1.2"
    )
    registry.register_host(
        host_id="nas", display_name="NAS", kind="linux", agent_version="0.1.1"
    )

    versions = {h.host_id: h.agent_version for h in registry.list_hosts()}

    assert versions == {"mac-mini": "0.1.2", "nas": "0.1.1"}


def test_an_existing_database_gains_the_column_without_losing_rows(tmp_path):
    """Every live host predates this column; none of them may be dropped.

    Reproduces the real upgrade: a table created without `agent_version` and
    already carrying a host, then bootstrapped again by the new code.
    """
    import duckdb

    from drover.server.harness.schema import bootstrap_harness_tables

    db = tmp_path / "old.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("""
            CREATE TABLE harness_hosts (
              host_id VARCHAR PRIMARY KEY, display_name VARCHAR NOT NULL,
              kind VARCHAR NOT NULL, local_url VARCHAR, tailscale_url VARCHAR,
              connection_kind VARCHAR, status VARCHAR NOT NULL,
              capabilities_json VARCHAR NOT NULL, last_seen_at TIMESTAMP,
              created_at TIMESTAMP NOT NULL DEFAULT now(),
              updated_at TIMESTAMP NOT NULL DEFAULT now()
            )
            """)
        con.execute(
            "INSERT INTO harness_hosts (host_id, display_name, kind, status, "
            "capabilities_json) VALUES ('nas', 'NAS', 'linux', 'online', '{}')"
        )

    with duckdb.connect(str(db)) as con:
        bootstrap_harness_tables(con)
        columns = {
            row[1]
            for row in con.execute("PRAGMA table_info('harness_hosts')").fetchall()
        }
        rows = con.execute(
            "SELECT host_id, agent_version FROM harness_hosts"
        ).fetchall()

    assert "agent_version" in columns
    assert rows == [("nas", None)]


def _collector(tmp_path):
    from drover.schema import bootstrap as _bootstrap
    from drover.server.metrics import MetricsCollector

    duckdb_path = tmp_path / "drover.duckdb"
    _bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    return MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )


def test_the_registration_endpoint_keeps_the_reported_version(tmp_path):
    """The payload has carried this since #132; the hub threw it away."""
    collector = _collector(tmp_path)

    status, body = collector.register_harness_host(
        {
            "host_id": "mac-mini",
            "display_name": "Mac Mini",
            "kind": "macos",
            "agent_version": "0.1.2",
        }
    )

    assert status == 200, body
    assert '"agent_version": "0.1.2"' in body or "0.1.2" in body
    stored = HarnessRegistry(tmp_path / "drover.duckdb").get_host("mac-mini")
    assert stored.agent_version == "0.1.2"


def test_a_non_string_version_is_ignored_rather_than_stored(tmp_path):
    """Hosts are authenticated but not trusted to send well-typed junk."""
    collector = _collector(tmp_path)

    status, _ = collector.register_harness_host(
        {
            "host_id": "nas",
            "display_name": "NAS",
            "kind": "linux",
            "agent_version": {"nope": True},
        }
    )

    assert status == 200
    stored = HarnessRegistry(tmp_path / "drover.duckdb").get_host("nas")
    assert stored.agent_version is None
