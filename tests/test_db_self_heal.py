"""Reopening a DuckDB instance that a fatal error left invalidated.

A fatal error (an out-of-memory inside a checkpoint, in every case seen on
the hub) puts the *instance* into a state where every later statement fails
with "database has been invalidated ... must be restarted prior to being used
again". The file on disk is intact; only the in-memory instance is poisoned,
and it stays alive as long as any connection to it does. Long-lived worker
connections therefore kept the hub broken until the process restarted, six
times in one day (drover#363).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from drover.server.db import (
    close_analytical_connections,
    is_invalidated_error,
    open_duckdb_connection,
    pin_analytical_connection,
    reset_invalidated_instance,
    supports_atomic_duckdb_clone,
)

_MESSAGE = (
    "FATAL Error: Failed: database has been invalidated because of a previous "
    "fatal error. The database must be restarted prior to being used again."
)


def test_is_invalidated_error_matches_the_real_message() -> None:
    assert is_invalidated_error(duckdb.FatalException(_MESSAGE))
    assert is_invalidated_error(RuntimeError(_MESSAGE))


def test_is_invalidated_error_ignores_ordinary_failures() -> None:
    assert not is_invalidated_error(duckdb.BinderException("no such column"))
    assert not is_invalidated_error(
        duckdb.OutOfMemoryException("Out of Memory Error: failed to allocate")
    )
    assert not is_invalidated_error(RuntimeError("something else"))


def test_reset_closes_every_handle_the_process_holds(tmp_path: Path) -> None:
    db = tmp_path / "store.duckdb"
    first = open_duckdb_connection(db)
    second = open_duckdb_connection(db)
    first.execute("CREATE TABLE t AS SELECT 1 AS id")

    closed = reset_invalidated_instance(db)

    assert closed >= 2
    for handle in (first, second):
        with pytest.raises(Exception):
            handle.execute("SELECT 1").fetchone()
    # The data is on disk; a fresh open still sees it.
    healed = open_duckdb_connection(db)
    try:
        assert healed.execute("SELECT id FROM t").fetchone() == (1,)
    finally:
        healed.close()


def test_open_heals_an_invalidated_instance(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "store.duckdb"
    seed = open_duckdb_connection(db)
    seed.execute("CREATE TABLE t AS SELECT 7 AS id")

    real_connect = duckdb.connect
    poisoned: list[duckdb.DuckDBPyConnection] = []

    class _Poisoned:
        """A handle that answers every statement the way a dead instance does."""

        def __init__(self, inner: duckdb.DuckDBPyConnection) -> None:
            self._inner = inner

        def execute(self, *args, **kwargs):
            raise duckdb.FatalException(_MESSAGE)

        def close(self) -> None:
            self._inner.close()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def fake_connect(path, *args, **kwargs):
        con = real_connect(path, *args, **kwargs)
        if len(poisoned) < 1:
            poisoned.append(con)
            return _Poisoned(con)
        return con

    monkeypatch.setattr(duckdb, "connect", fake_connect)

    healed = open_duckdb_connection(db)
    try:
        assert healed.execute("SELECT id FROM t").fetchone() == (7,)
    finally:
        healed.close()
    assert poisoned, "the poisoned connection was never opened"


def test_open_restores_analytical_pin_after_invalidation(tmp_path: Path, monkeypatch) -> None:
    probe = tmp_path / "clone-probe"
    probe.write_bytes(b"probe")
    if not supports_atomic_duckdb_clone(probe):
        pytest.skip("atomic database/WAL cloning requires APFS")
    db = tmp_path / "store.duckdb"
    monkeypatch.setenv("DROVER_ANALYTICAL_PIN", "1")
    assert pin_analytical_connection(db)
    real_connect = duckdb.connect
    poisoned = False

    class Poisoned:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, *args, **kwargs):
            raise duckdb.FatalException(_MESSAGE)

        def close(self):
            self.inner.close()

        def __getattr__(self, name):
            return getattr(self.inner, name)

    def connect(path, *args, **kwargs):
        nonlocal poisoned
        inner = real_connect(path, *args, **kwargs)
        if not poisoned:
            poisoned = True
            return Poisoned(inner)
        return inner

    monkeypatch.setattr(duckdb, "connect", connect)
    try:
        healed = open_duckdb_connection(db)
        healed.close()
        assert poisoned
        writer = open_duckdb_connection(db)
        try:
            writer.execute("CREATE TABLE pin_recovery (id INTEGER)")
        finally:
            writer.close()
        assert Path(str(db) + ".wal").exists()
    finally:
        close_analytical_connections()


def test_open_does_not_reset_on_an_ordinary_error(tmp_path: Path, monkeypatch) -> None:
    """Only the invalidated state justifies closing other threads' handles."""
    db = tmp_path / "store.duckdb"
    keep = open_duckdb_connection(db)
    keep.execute("CREATE TABLE t AS SELECT 1 AS id")
    real_connect = duckdb.connect
    calls: list[int] = []

    class _Binder:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, *args, **kwargs):
            raise duckdb.BinderException("no such column")

        def close(self):
            self._inner.close()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def fake_connect(path, *args, **kwargs):
        calls.append(1)
        return _Binder(real_connect(path, *args, **kwargs))

    monkeypatch.setattr(duckdb, "connect", fake_connect)
    with pytest.raises(duckdb.BinderException):
        open_duckdb_connection(db)
    assert len(calls) == 1, "an ordinary error must not trigger a reconnect"
    # The pre-existing handle was not closed behind the caller's back.
    assert keep.execute("SELECT id FROM t").fetchone() == (1,)
    keep.close()
