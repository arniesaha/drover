"""Control-plane checkpoint budget and health (2026-10-05 Studio incident).

The Studio's ``drover.registry.duckdb`` reached ~1.05 GB under a 256MB
instance limit and every checkpoint failed with "could not allocate block
(244.0 MiB/244.1 MiB used)" -- 6k+ times, while ``/readyz`` stayed green.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import duckdb
import pytest

from drover.server import db
from drover.server.db import (
    ROLE_DEFAULTS,
    control_plane_checkpoint_health,
    control_plane_checkpoint_memory_limit,
    control_plane_connection,
    control_plane_memory_limit,
    control_plane_path,
    duckdb_size_bytes,
    startup_control_plane_checkpoint,
)
from drover.server.harness.schema import bootstrap_harness_tables
from drover.server.metrics import MetricsCollector, start_metrics_server
from drover.server.readiness import (
    STATE_FAILED,
    STATE_OK,
    STORE_CONTROL_PLANE,
    ReadinessProbe,
)

OLD_LIMIT = "256MB"

_CHECKPOINT_FAILURE = (
    "FATAL Error: Failed to create checkpoint because of error: could not "
    "allocate block of size 256.0 KiB (244.0 MiB/244.1 MiB used)"
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for name in (
        "DROVER_DUCKDB_CONTROL_PLANE_MEMORY_LIMIT",
        "DROVER_CONTROL_PLANE_CHECKPOINT_MEMORY_LIMIT",
        "DROVER_CONTROL_PLANE_DUCKDB",
        "DROVER_CONTROL_PLANE_PIN",
    ):
        monkeypatch.delenv(name, raising=False)
    db._CONTROL_PLANE_CHECKPOINT_FAILURES.clear()
    yield
    db._CONTROL_PLANE_CHECKPOINT_FAILURES.clear()
    db.close_control_plane_connections()


def _instance_limit(con: duckdb.DuckDBPyConnection) -> int:
    return duckdb_size_bytes(
        con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    )


def _store(tmp_path: Path) -> Path:
    duckdb_path = tmp_path / "drover.duckdb"
    with control_plane_connection(duckdb_path) as con:
        bootstrap_harness_tables(con)
    return duckdb_path


# -- defaults and overrides ---------------------------------------------------


def test_control_plane_default_is_one_gigabyte_and_still_its_own_budget():
    assert ROLE_DEFAULTS["control_plane"]["memory_limit"] == "1GB"
    assert control_plane_memory_limit() == "1GB"
    # Still a fraction of the analytical instance it was split from.
    assert duckdb_size_bytes("1GB") < duckdb_size_bytes(
        db.ANALYTICAL_INSTANCE_DEFAULTS["memory_limit"]
    )
    # An explicit checkpoint gets the 2GB the incident was mitigated with.
    assert control_plane_checkpoint_memory_limit() == "2GB"


def test_default_instance_limit_is_applied_to_the_control_plane(tmp_path):
    with control_plane_connection(tmp_path / "drover.duckdb") as con:
        # DuckDB reports in its own units; 1GB comes back as ~953.6 MiB.
        assert _instance_limit(con) == pytest.approx(duckdb_size_bytes("1GB"), rel=0.05)


def test_control_plane_limits_honour_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("DROVER_DUCKDB_CONTROL_PLANE_MEMORY_LIMIT", "3GB")
    assert control_plane_memory_limit() == "3GB"
    # The checkpoint limit never drops below a larger instance limit...
    assert control_plane_checkpoint_memory_limit() == "3GB"
    with control_plane_connection(tmp_path / "drover.duckdb") as con:
        assert _instance_limit(con) == pytest.approx(duckdb_size_bytes("3GB"), rel=0.05)
    # ...and an explicit checkpoint override wins outright.
    monkeypatch.setenv("DROVER_CONTROL_PLANE_CHECKPOINT_MEMORY_LIMIT", "6GB")
    assert control_plane_checkpoint_memory_limit() == "6GB"

    monkeypatch.setenv("DROVER_CONTROL_PLANE_CHECKPOINT_MEMORY_LIMIT", "lots")
    with pytest.raises(ValueError, match="invalid DuckDB size format"):
        control_plane_checkpoint_memory_limit()


def test_explicit_checkpoint_runs_under_its_own_limit_then_restores(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DROVER_CONTROL_PLANE_CHECKPOINT_MEMORY_LIMIT", "3GB")
    seen = []
    with control_plane_connection(tmp_path / "drover.duckdb") as con:
        real = con.execute

        class Spy:
            def execute(self, sql, *args):
                if sql == "CHECKPOINT":
                    seen.append(_instance_limit(con))
                return real(sql, *args)

        db._checkpoint_control_plane(Spy())
        assert _instance_limit(con) == pytest.approx(duckdb_size_bytes("1GB"), rel=0.05)
    assert seen == [pytest.approx(duckdb_size_bytes("3GB"), rel=0.05)]


# -- health -------------------------------------------------------------------


def test_failed_checkpoint_is_recorded_fails_readiness_and_clears(
    tmp_path, monkeypatch, caplog
):
    duckdb_path = _store(tmp_path)
    probe = ReadinessProbe(duckdb_path, include_analytical=False, cache_seconds=60)
    assert probe.check().ok

    with caplog.at_level(logging.ERROR, logger="drover.db"):
        with pytest.raises(duckdb.FatalException):
            with control_plane_connection(duckdb_path):
                raise duckdb.FatalException(_CHECKPOINT_FAILURE)

    health = control_plane_checkpoint_health(duckdb_path)
    assert health["status"] == "checkpoint-failing"
    assert health["failures"] == 1
    assert any(
        r.levelno == logging.ERROR and "control-plane checkpoint failed" in r.message
        for r in caplog.records
    )

    # The cached green verdict is not reused. The probe's own window retries
    # the checkpoint; forced to fail, it stays red and counts the failure.
    def failing_checkpoint(con):
        raise duckdb.OutOfMemoryException(_CHECKPOINT_FAILURE)

    with monkeypatch.context() as patch:
        patch.setattr(db, "_checkpoint_control_plane", failing_checkpoint)
        report = probe.check()
    assert not report.ok
    (store,) = report.stores
    assert store.state == STATE_FAILED
    assert "checkpoint failure" in store.detail
    assert control_plane_checkpoint_health(duckdb_path)["failures"] == 2

    # A real checkpoint succeeding clears the failure, and readiness with it.
    report = probe.check()
    assert report.ok, report.as_dict()
    assert report.stores[0].state == STATE_OK
    assert control_plane_checkpoint_health(duckdb_path)["status"] == "ok"


def test_ordinary_errors_are_not_checkpoint_failures(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    with pytest.raises(duckdb.CatalogException):
        with control_plane_connection(duckdb_path) as con:
            con.execute("SELECT * FROM no_such_table")
    assert control_plane_checkpoint_health(duckdb_path)["status"] == "ok"


def test_failed_checkpoint_on_a_pinned_connection_replaces_the_pin(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DROVER_CONTROL_PLANE_PIN", "1")
    duckdb_path = _store(tmp_path)
    assert db.pin_control_plane_connection(duckdb_path) is True
    key = db._path_key(control_plane_path(duckdb_path))

    with pytest.raises(duckdb.FatalException):
        with control_plane_connection(duckdb_path):
            raise duckdb.FatalException(_CHECKPOINT_FAILURE)

    assert key not in db._CONTROL_PLANE_CONNECTIONS
    assert control_plane_checkpoint_health(duckdb_path)["failures"] == 1
    # The next window opens its own connection, checkpoints, and clears it.
    with control_plane_connection(duckdb_path) as con:
        con.execute("SELECT 1").fetchone()
    assert control_plane_checkpoint_health(duckdb_path)["status"] == "ok"


def _http_get(port: int, path: str):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=5
        ) as response:
            return response.status, response.headers, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode()


def test_healthz_and_readyz_report_a_failing_control_plane_checkpoint(
    tmp_path, monkeypatch
):
    from drover.schema import bootstrap

    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    db.record_control_plane_checkpoint_failure(
        duckdb_path, duckdb.FatalException(_CHECKPOINT_FAILURE)
    )
    monkeypatch.setattr(
        db,
        "_checkpoint_control_plane",
        mock.Mock(side_effect=duckdb.OutOfMemoryException(_CHECKPOINT_FAILURE)),
    )
    server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
    port = server.server_address[1]
    try:
        health_status, health_headers, health_body = _http_get(port, "/healthz")
        ready_status, _, ready_body = _http_get(port, "/readyz")
    finally:
        server.shutdown()

    # Liveness: still 200 with the exact body the cutover gate compares.
    assert health_status == 200
    assert health_body == "ok\nanalytical=ok\n"
    assert health_headers["X-Drover-Control-Plane"] == "checkpoint-failing"
    # Readiness: red, and it names the store.
    assert ready_status == 503
    states = {s["store"]: s["state"] for s in json.loads(ready_body)["stores"]}
    assert states[STORE_CONTROL_PLANE] == STATE_FAILED


# -- startup checkpoint -------------------------------------------------------


def _leave_a_wal(store: Path) -> Path:
    script = f"""import duckdb, os
con = duckdb.connect({str(store)!r})
con.execute("CREATE TABLE harness_hosts (host_id VARCHAR PRIMARY KEY)")
con.execute("INSERT INTO harness_hosts VALUES ('studio'), ('laptop')")
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script], check=True)
    wal = Path(str(store) + ".wal")
    assert wal.exists() and wal.stat().st_size > 0
    return wal


def test_startup_checkpoint_drains_the_control_plane_wal(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    store = control_plane_path(duckdb_path)
    wal = _leave_a_wal(store)
    db.record_control_plane_checkpoint_failure(
        duckdb_path, duckdb.FatalException(_CHECKPOINT_FAILURE)
    )

    assert startup_control_plane_checkpoint(duckdb_path) is True
    assert not wal.exists() or wal.stat().st_size == 0
    assert control_plane_checkpoint_health(duckdb_path)["status"] == "ok"
    con = duckdb.connect(str(store), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM harness_hosts").fetchone() == (2,)
    finally:
        con.close()


def test_startup_checkpoint_uses_the_checkpoint_limit(tmp_path, monkeypatch):
    duckdb_path = tmp_path / "drover.duckdb"
    store = control_plane_path(duckdb_path)
    store.write_bytes(b"")
    Path(str(store) + ".wal").write_bytes(b"wal")
    monkeypatch.setenv("DROVER_CONTROL_PLANE_CHECKPOINT_MEMORY_LIMIT", "5GB")
    with mock.patch("duckdb.connect") as connect:
        assert startup_control_plane_checkpoint(duckdb_path) is True
    connect.assert_called_once_with(
        str(store), config={"memory_limit": "5GB", "threads": "1"}
    )
    connect.return_value.execute.assert_called_once_with("CHECKPOINT")


def test_failing_startup_checkpoint_is_recorded_not_raised(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    store = control_plane_path(duckdb_path)
    store.write_bytes(b"")
    Path(str(store) + ".wal").write_bytes(b"wal")
    with mock.patch("duckdb.connect") as connect:
        connect.return_value.execute.side_effect = duckdb.OutOfMemoryException(
            _CHECKPOINT_FAILURE
        )
        assert startup_control_plane_checkpoint(duckdb_path) is False
    assert control_plane_checkpoint_health(duckdb_path)["status"] == (
        "checkpoint-failing"
    )


def test_startup_checkpoint_without_a_wal_is_a_no_op(tmp_path):
    duckdb_path = _store(tmp_path)
    with mock.patch("duckdb.connect") as connect:
        assert startup_control_plane_checkpoint(duckdb_path) is False
    connect.assert_not_called()


# -- regression: a store bigger than the old limit ----------------------------

#: Shaped like the real table: uuid event ids and sha256 dedup keys (the two
#: unique ART indexes) and kilobyte-scale terminal.output payloads that do not
#: compress away. Index memory is what scales with the row count.
_INSERT_EVENTS = """
                INSERT INTO harness_events (
                  event_id, session_id, event_type, normalized_type,
                  normalized_source, content_preview, payload_json,
                  created_at, seq, dedup_key)
                SELECT 'harness-event-' || uuid()::VARCHAR,
                       'session-' || (n % 3000)::VARCHAR,
                       'terminal.output', 'output', 'terminal',
                       left(sha256(n::VARCHAR || 'p'), 64),
                       '{{"data":"' || sha256(n::VARCHAR || 'a')
                         || sha256(n::VARCHAR || 'b') || sha256(n::VARCHAR || 'c')
                         || sha256(n::VARCHAR || 'd') || sha256(n::VARCHAR || 'e')
                         || sha256(n::VARCHAR || 'f') || sha256(n::VARCHAR || 'g')
                         || sha256(n::VARCHAR || 'h') || '"}}',
                       now(), (n // 3000)::INTEGER,
                       sha256(n::VARCHAR)
                  FROM (SELECT i + {start} AS n FROM range({step}) t(i))
                """


def _leave_uncheckpointed_events(store: Path, start: int, count: int) -> None:
    """Commit ``count`` more events to the WAL and die before checkpointing.

    The steady state of the incident: once checkpoints fail, everything since
    the last good one lives only in the WAL, which every open replays.
    """
    # Small transactions, as ingest makes them. One bulk append would be
    # written straight to the database file and leave the WAL nearly empty.
    batch = 10_000
    statements = [
        _INSERT_EVENTS.format(start=offset, step=min(batch, start + count - offset))
        for offset in range(start, start + count, batch)
    ]
    script = f"""import duckdb, os
con = duckdb.connect({str(store)!r}, config={{"memory_limit": "8GB",
    "checkpoint_threshold": "100GB"}})
for sql in {statements!r}:
    con.execute(sql)
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script], check=True)


def _grow(store: Path, *, events: int, wal_events: int) -> None:
    con = duckdb.connect(str(store), config={"memory_limit": "8GB"})
    try:
        bootstrap_harness_tables(con)
        step = 100_000
        for start in range(0, events, step):
            con.execute(_INSERT_EVENTS.format(start=start, step=step))
        con.execute("CHECKPOINT")
    finally:
        con.close()
    _leave_uncheckpointed_events(store, events, wal_events)


def _start_write_and_checkpoint(con: duckdb.DuckDBPyConnection) -> str:
    """What a hub start and its first write do; returns the step in progress."""
    step = "bootstrap"  # drops and rebuilds the unique dedup_key index
    bootstrap_harness_tables(con)
    step = "write"
    con.execute("""
        INSERT INTO harness_events (event_id, session_id, event_type,
          payload_json, dedup_key)
        VALUES ('harness-event-regression', 'session-new', 'terminal.output',
                '{}', 'regression')
        ON CONFLICT DO NOTHING
        """)
    step = "checkpoint"
    con.execute("CHECKPOINT")
    return step


def _at_old_limit(tmp_path: Path, store: Path) -> tuple[str, BaseException | None]:
    """Replay the old 256MB budget on a scratch copy of ``store`` and its WAL."""
    old = tmp_path / "old-limit.registry.duckdb"
    shutil.copy2(store, old)
    shutil.copy2(Path(str(store) + ".wal"), Path(str(old) + ".wal"))
    step = "open"  # replays the WAL
    try:
        con = duckdb.connect(str(old), config={"memory_limit": OLD_LIMIT})
        try:
            step = "bootstrap"
            step = _start_write_and_checkpoint(con)
        finally:
            con.close()
    except duckdb.Error as exc:
        return step, exc
    return step, None


def _checkpoints_at_the_new_default(duckdb_path: Path, total: int) -> float:
    """Through the real window, at the shipped default. Returns resident MiB."""
    store = control_plane_path(duckdb_path)
    wal = Path(str(store) + ".wal")
    with control_plane_connection(duckdb_path) as con:
        assert _instance_limit(con) == pytest.approx(duckdb_size_bytes("1GB"), rel=0.05)
        _start_write_and_checkpoint(con)
        resident = con.execute(
            "SELECT sum(memory_usage_bytes) FROM duckdb_memory()"
        ).fetchone()[0]
    assert control_plane_checkpoint_health(duckdb_path)["status"] == "ok"
    assert not wal.exists() or wal.stat().st_size == 0
    con = duckdb.connect(str(store), read_only=True)
    try:
        count = con.execute("SELECT count(*) FROM harness_events").fetchone()[0]
    finally:
        con.close()
    assert count == total + 1
    return resident / 2**20


def test_a_store_bigger_than_the_old_limit_checkpoints_at_the_new_one(tmp_path):
    """600k events: ~310 MiB on disk plus a ~145 MiB WAL, both over 256MB."""
    duckdb_path = tmp_path / "drover.duckdb"
    store = control_plane_path(duckdb_path)
    _grow(store, events=400_000, wal_events=200_000)
    assert store.stat().st_size > duckdb_size_bytes(OLD_LIMIT)
    resident = _checkpoints_at_the_new_default(duckdb_path, 600_000)
    print(f"\n600k events: {resident:.1f} MiB resident after checkpoint at 1GB")


@pytest.mark.skipif(
    os.environ.get("DROVER_CONTROL_PLANE_SCALE_TEST") != "1",
    reason="Studio-sized (~1.4 GB, ~35s); set DROVER_CONTROL_PLANE_SCALE_TEST=1",
)
def test_a_studio_sized_store_fails_at_256mb_and_checkpoints_at_1gb(tmp_path):
    """The incident, reproduced: 2M events, a ~1.37 GB store and a WAL.

    Measured on DuckDB 1.5.5: at 256MB this fails with "(244.1 MiB/244.1 MiB
    used)", the ceiling in the Studio's log; at 1GB it checkpoints with ~204
    MiB resident. 1.2M events (~755 MiB) still fit in 256MB, which is why the
    old budget survived until the store grew past roughly a gigabyte.
    """
    duckdb_path = tmp_path / "drover.duckdb"
    store = control_plane_path(duckdb_path)
    _grow(store, events=1_800_000, wal_events=200_000)

    step, error = _at_old_limit(tmp_path, store)
    assert isinstance(error, duckdb.OutOfMemoryException), (step, error)
    assert "244.1 MiB/244.1 MiB used" in str(error)

    resident = _checkpoints_at_the_new_default(duckdb_path, 2_000_000)
    print(
        f"\n2M events, {store.stat().st_size / 2**20:.1f} MiB: 256MB failed at "
        f"{step}; 1GB resident after checkpoint {resident:.1f} MiB"
    )
