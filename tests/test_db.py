from __future__ import annotations

import os
import sys
import time

import duckdb
import pytest

from drover.server import db as db_module
from drover.server.db import (
    ROLE_DEFAULTS,
    copy_duckdb_store,
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


def _wal_from_a_different_database(tmp_path) -> bytes:
    """A real WAL whose contents do not describe the store we pair it with.

    This is what a torn copy produces: the ``.duckdb`` captured at one instant
    and the ``.wal`` at another, so the log no longer describes the file beside
    it. Building it from a genuinely different database is the honest way to
    get those bytes -- DuckDB skips a WAL of zeros, so a hand-rolled one would
    prove nothing.
    """
    other = tmp_path / "other.duckdb"
    con = duckdb.connect(str(other))
    con.execute("CREATE TABLE unrelated (x INTEGER)")
    con.execute("INSERT INTO unrelated VALUES (1)")
    # Read it while the connection is open: DuckDB checkpoints on last close,
    # which would fold the WAL away before we could take it.
    payload = (tmp_path / "other.duckdb.wal").read_bytes()
    con.close()
    return payload


def test_the_snapshot_is_not_paired_with_a_wal_it_cannot_vouch_for(tmp_path):
    """A snapshot carries the store alone, never a separately-captured WAL.

    The store and its WAL are two files taken at two instants, so a copy that
    carries both can only vouch for the pair by luck. Dropping the WAL costs
    the rows written since the last checkpoint -- a store on its own is still a
    valid database as of that checkpoint -- and stale is a trade a metrics
    snapshot can make. Unopenable is not.

    Note what this deliberately does *not* claim. A mismatched WAL turns out to
    replay harmlessly, so it is **not** the mechanism behind the allocator and
    segment-tree failures seen in production; those come from the store itself
    being read torn while DuckDB writes into it, which the atomic capture
    answers. This locks the pairing half only.
    """
    source = tmp_path / "live.duckdb"
    con = duckdb.connect(str(source))
    con.execute("CREATE TABLE t AS SELECT 1 AS a")
    con.close()

    (tmp_path / "live.duckdb.wal").write_bytes(_wal_from_a_different_database(tmp_path))

    destination = tmp_path / "snap.duckdb"
    copy_duckdb_store(source, destination)

    assert not (tmp_path / "snap.duckdb.wal").exists()

    snap = duckdb.connect(str(destination))
    try:
        assert snap.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    finally:
        snap.close()


def test_the_store_is_cloned_rather_than_read_in_chunks(tmp_path, monkeypatch):
    """A chunked read of a live store is the bug; a clone is the fix.

    ``shutil.copy2`` walks the file in chunks, so a store DuckDB is writing
    into can be captured half way through a page write. Failing the test if
    that path is taken is the only way to state "do not read this file in
    chunks" as something CI can check.
    """
    if not sys.platform == "darwin":
        pytest.skip("clonefile is Darwin-only; the fallback has its own test")

    source = tmp_path / "live.duckdb"
    con = duckdb.connect(str(source))
    con.execute("CREATE TABLE t AS SELECT 1 AS a")
    con.close()

    def _refuse(*args, **kwargs):
        raise AssertionError("the live store was read in chunks instead of cloned")

    monkeypatch.setattr(db_module.shutil, "copy2", _refuse)

    copy_duckdb_store(source, tmp_path / "snap.duckdb")

    snap = duckdb.connect(str(tmp_path / "snap.duckdb"))
    try:
        assert snap.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    finally:
        snap.close()


def test_a_store_that_cannot_be_cloned_is_still_captured_without_its_wal(
    tmp_path, monkeypatch
):
    """Cross-volume and non-APFS still have to produce an openable snapshot.

    The fallback keeps the tearing risk a chunked read carries -- there is no
    way around that off APFS -- but it must not also reintroduce the WAL
    pairing hazard, which is independent of how the store itself is captured.
    """
    monkeypatch.setattr(db_module, "_clone_file", lambda source, destination: False)

    source = tmp_path / "live.duckdb"
    con = duckdb.connect(str(source))
    con.execute("CREATE TABLE t AS SELECT 1 AS a")
    con.close()
    (tmp_path / "live.duckdb.wal").write_bytes(_wal_from_a_different_database(tmp_path))

    copy_duckdb_store(source, tmp_path / "snap.duckdb")

    assert not (tmp_path / "snap.duckdb.wal").exists()
    snap = duckdb.connect(str(tmp_path / "snap.duckdb"))
    try:
        assert snap.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    finally:
        snap.close()


def test_snapshot_scratch_sits_beside_the_store_not_in_system_temp(tmp_path):
    """Snapshots belong on the store's own volume, for two reasons at once.

    ``clonefile`` is same-volume only, so a snapshot written to the system temp
    dir while the store lives elsewhere silently falls back to a chunked read --
    the copy the clone exists to replace. Landing beside the store makes the
    clone engage, which also makes the copy nearly free: copy-on-write extents
    cost no space until they diverge.

    The system temp dir is on the boot volume here, and these copies are
    350-950 MB each, arriving every 15-20 minutes. That filled the disk and
    took the hub down (#171).
    """
    root = db_module.snapshot_scratch_root(tmp_path / "live.duckdb")

    assert root.parent == tmp_path
    assert root.is_dir()
    assert "/var/folders/" not in str(root)


def test_orphaned_snapshot_scratch_is_swept_but_live_work_is_kept(tmp_path):
    """Cleanup on graceful exit is not enough, as the disk proved.

    A hub killed rather than asked to stop leaves its copies behind, and they
    accumulated across every restart. The sweep is what makes the leak
    self-limiting instead of monotonic -- but it has to be able to tell an
    abandoned directory from one another process is filling right now.
    """
    root = db_module.snapshot_scratch_root(tmp_path / "live.duckdb")
    stale = root / "drover-control-plane-oldone"
    stale.mkdir()
    fresh = root / "drover-control-plane-newone"
    fresh.mkdir()
    old = time.time() - (6 * 3600)
    os.utime(stale, (old, old))

    db_module.sweep_orphaned_snapshot_scratch(
        tmp_path / "live.duckdb", older_than_seconds=3600
    )

    assert not stale.exists()
    assert fresh.exists()
