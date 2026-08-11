from __future__ import annotations

import duckdb

from drover.server.db import (
    ROLE_DEFAULTS,
    open_duckdb_connection,
    snapshot_thread_default,
)


def test_snapshot_thread_default_scales_with_cores_but_always_leaves_one():
    """The snapshot role gets parallelism, never the whole machine.

    threads is a DuckDB *instance* setting, so a role that can be pointed at
    the live database must stay at 1 (issue #91). This role is only ever used
    against a private copy, which is its own instance -- but the copy still
    shares CPU with the live server, so it never takes the last core.
    """
    assert snapshot_thread_default(1) == 1
    assert snapshot_thread_default(2) == 1
    assert snapshot_thread_default(4) == 3
    # Measured on the 2.32M-row store: 4 threads and 8 threads are within
    # noise of each other (6.6s vs 6.3s), so 4 is the knee, not a budget.
    assert snapshot_thread_default(10) == 4
    assert snapshot_thread_default(64) == 4


def test_diagnostic_role_stays_single_threaded():
    """Regression guard for the 2026-08-04 outage (#91, PR #76, PR #93).

    ``diagnostic`` still governs readers pointed at the *live* database, where
    raising threads raises the shared instance's thread count for every other
    connection. Faster snapshots come from the separate ``snapshot`` role, not
    from loosening this one.
    """
    assert ROLE_DEFAULTS["diagnostic"]["threads"] == "1"


def test_open_duckdb_connection_supports_snapshot_profile(tmp_path):
    db = tmp_path / "copy.duckdb"
    con = open_duckdb_connection(db, role="snapshot")
    try:
        threads = con.execute("SELECT current_setting('threads')").fetchone()[0]
    finally:
        con.close()
    assert threads == int(ROLE_DEFAULTS["snapshot"]["threads"])
    assert threads >= 1


def test_threads_is_instance_wide_so_a_shared_file_leaks_it(tmp_path, monkeypatch):
    """The reason ``snapshot`` may only ever read a private copy.

    ``threads`` is a DuckDB *instance* setting, not a connection one. Two
    connections to the same file share an instance, so raising it on one
    raises it on the other -- which is how a diagnostic scan came to starve
    the live harness registry on 2026-08-04 (#91). This pins the mechanism.
    """
    monkeypatch.setenv("DROVER_DUCKDB_SNAPSHOT_THREADS", "4")
    shared = tmp_path / "live.duckdb"

    live = open_duckdb_connection(shared, role="worker")
    try:
        assert live.execute("SELECT current_setting('threads')").fetchone() == (2,)
        greedy = open_duckdb_connection(shared, role="snapshot")
        try:
            assert live.execute("SELECT current_setting('threads')").fetchone() == (4,)
        finally:
            greedy.close()
    finally:
        live.close()


def test_a_snapshot_of_a_copy_cannot_change_the_live_instance(tmp_path, monkeypatch):
    """...and the reason the copy makes the extra threads safe.

    A separate file is a separate DuckDB instance with its own scheduler, so
    the snapshot role's thread count cannot reach the live one.
    """
    monkeypatch.setenv("DROVER_DUCKDB_SNAPSHOT_THREADS", "4")

    live = open_duckdb_connection(tmp_path / "live.duckdb", role="worker")
    try:
        copy = open_duckdb_connection(tmp_path / "copy.duckdb", role="snapshot")
        try:
            assert copy.execute("SELECT current_setting('threads')").fetchone() == (4,)
            assert live.execute("SELECT current_setting('threads')").fetchone() == (2,)
        finally:
            copy.close()
    finally:
        live.close()


def test_snapshot_role_ignores_the_diagnostic_thread_override(tmp_path, monkeypatch):
    """Scoping is the point: throttling live diagnostics must not throttle the
    isolated copy readers, and vice versa."""
    monkeypatch.setenv("DROVER_DUCKDB_DIAGNOSTIC_THREADS", "1")
    monkeypatch.setenv("DROVER_DUCKDB_WORKER_THREADS", "1")
    monkeypatch.setenv("DROVER_DUCKDB_SNAPSHOT_THREADS", "3")

    con = open_duckdb_connection(tmp_path / "copy.duckdb", role="snapshot")
    try:
        assert con.execute("SELECT current_setting('threads')").fetchone() == (3,)
    finally:
        con.close()


def test_open_duckdb_connection_applies_settings_without_config_conflict(tmp_path):
    db = tmp_path / "drover.duckdb"
    writer = duckdb.connect(str(db))
    try:
        writer.execute("CREATE TABLE items (id INTEGER)")
        writer.execute("INSERT INTO items VALUES (1)")

        # DuckDB rejects a second connection opened with config=... while a
        # default connection is alive. Drover applies SET statements after open
        # to avoid that live daemon/diagnostic conflict.
        worker = open_duckdb_connection(db)
        try:
            assert worker.execute("SELECT count(*) FROM items").fetchone() == (1,)
            assert worker.execute("SELECT current_setting('threads')").fetchone() == (
                2,
            )
        finally:
            worker.close()
    finally:
        writer.close()


def test_open_duckdb_connection_supports_diagnostic_snapshot_profile(tmp_path):
    db = tmp_path / "snapshot.duckdb"
    writer = duckdb.connect(str(db))
    try:
        writer.execute("CREATE TABLE items (id INTEGER)")
        writer.execute("INSERT INTO items VALUES (1)")
    finally:
        writer.close()

    diagnostic = open_duckdb_connection(db, read_only=True, role="diagnostic")
    try:
        assert diagnostic.execute("SELECT count(*) FROM items").fetchone() == (1,)
        assert diagnostic.execute("SELECT current_setting('threads')").fetchone() == (
            1,
        )
        assert diagnostic.execute(
            "SELECT current_setting('memory_limit')"
        ).fetchone() == ("1.8 GiB",)
    finally:
        diagnostic.close()


def test_open_duckdb_connection_supports_summarizer_profile(tmp_path):
    con = open_duckdb_connection(tmp_path / "drover.duckdb", role="summarizer")
    try:
        assert con.execute("SELECT current_setting('threads')").fetchone() == (1,)
        assert con.execute("SELECT current_setting('memory_limit')").fetchone() == (
            "1.8 GiB",
        )
    finally:
        con.close()


def test_diagnostic_open_coexists_with_live_worker_connection(tmp_path):
    """Reproduces issue #2: a diagnostic (read_only) open while a worker
    connection is alive must not raise "Can't open a connection to same
    database file with a different configuration"."""
    db = tmp_path / "drover.duckdb"
    worker = open_duckdb_connection(db)
    try:
        worker.execute("CREATE TABLE items (id INTEGER)")
        worker.execute("INSERT INTO items VALUES (1)")
        diagnostic = open_duckdb_connection(db, read_only=True, role="diagnostic")
        try:
            assert diagnostic.execute("SELECT count(*) FROM items").fetchone() == (1,)
        finally:
            diagnostic.close()
    finally:
        worker.close()


def test_concurrent_role_opens_do_not_conflict(tmp_path):
    """Worker + summarizer + diagnostic connections may all be open at once
    (drain loops race diagnostics in the live server)."""
    db = tmp_path / "drover.duckdb"
    cons = []
    try:
        cons.append(open_duckdb_connection(db, role="worker"))
        cons.append(open_duckdb_connection(db, role="summarizer"))
        cons.append(open_duckdb_connection(db, read_only=True, role="diagnostic"))
        for con in cons:
            assert con.execute("SELECT 1").fetchone() == (1,)
    finally:
        for con in cons:
            con.close()


def test_open_duckdb_connection_honors_role_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("DROVER_DUCKDB_WORKER_MEMORY_LIMIT", "64MB")
    monkeypatch.setenv("DROVER_DUCKDB_WORKER_THREADS", "1")

    con = open_duckdb_connection(tmp_path / "drover.duckdb")
    try:
        assert con.execute("SELECT current_setting('memory_limit')").fetchone() == (
            "61.0 MiB",
        )
        assert con.execute("SELECT current_setting('threads')").fetchone() == (1,)
    finally:
        con.close()


def test_open_duckdb_connection_honors_summarizer_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("DROVER_DUCKDB_SUMMARIZER_MEMORY_LIMIT", "1536MB")
    monkeypatch.setenv("DROVER_DUCKDB_SUMMARIZER_THREADS", "2")

    con = open_duckdb_connection(tmp_path / "drover.duckdb", role="summarizer")
    try:
        assert con.execute("SELECT current_setting('threads')").fetchone() == (2,)
        assert con.execute("SELECT current_setting('memory_limit')").fetchone() == (
            "1.4 GiB",
        )
    finally:
        con.close()
