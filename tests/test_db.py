from __future__ import annotations

import duckdb

from drover.server.db import open_duckdb_connection


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
