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

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from drover.server import db as db_module
from drover.server.db import (
    AnalyticalStoreUnavailable,
    analytical_store_health,
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


def _wait_recovery(path, status="ok"):
    deadline = time.monotonic() + 5
    while analytical_store_health(path)["status"] != status:
        assert time.monotonic() < deadline, "recovery did not finish"
        time.sleep(0.01)
    assert analytical_store_health(path)["status"] == status


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

    with pytest.raises(AnalyticalStoreUnavailable):
        open_duckdb_connection(db)
    _wait_recovery(db)
    healed = open_duckdb_connection(db)
    try:
        assert healed.execute("SELECT id FROM t").fetchone() == (7,)
    finally:
        healed.close()
    assert poisoned, "the poisoned connection was never opened"


def test_open_restores_analytical_pin_after_invalidation(
    tmp_path: Path, monkeypatch
) -> None:
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
        with pytest.raises(AnalyticalStoreUnavailable):
            open_duckdb_connection(db)
        _wait_recovery(db)
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


@pytest.mark.parametrize("method", ["execute", "fetchone", "close", "cursor"])
def test_use_detects_fatal_and_recovers_once(tmp_path, monkeypatch, caplog, method):
    path = tmp_path / "store.duckdb"
    con = open_duckdb_connection(path)
    con.execute("CREATE TABLE preserved AS SELECT 42 AS n")
    original = con._inner
    entered, release = threading.Event(), threading.Event()
    reset = db_module.reset_invalidated_instance
    resets = []

    def held_reset(path):
        resets.append(path)
        entered.set()
        assert release.wait(5)
        return reset(path)

    class Poisoned:
        def __getattr__(self, name):
            if name == method:

                def fail(*args, **kwargs):
                    raise duckdb.FatalException(
                        _MESSAGE + " Original error: checkpoint OOM"
                    )

                return fail
            return getattr(original, name)

    monkeypatch.setattr(db_module, "reset_invalidated_instance", held_reset)
    con._inner = Poisoned()
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(
                    getattr(con, method), *(["SELECT 1"] if method == "execute" else [])
                )
                for _ in range(8)
            ]
            failures = 0
            for future in futures:
                try:
                    future.result(timeout=2)
                except AnalyticalStoreUnavailable:
                    failures += 1
                else:
                    assert method == "close"  # recovery owns subsequent cleanup
            assert failures >= 1
        assert entered.wait(1)
        assert analytical_store_health(path)["status"] == "recovering"
        with pytest.raises(AnalyticalStoreUnavailable):
            open_duckdb_connection(path)
        assert len(resets) == 1
        fatal_logs = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(fatal_logs) == 1
        assert "Original error: checkpoint OOM" in fatal_logs[0].message
    finally:
        con._inner = original
        release.set()
        _wait_recovery(path)
    with open_duckdb_connection(path) as healed:
        assert healed.execute("SELECT * FROM preserved").fetchone() == (42,)
    with pytest.raises(AnalyticalStoreUnavailable):
        con.execute("SELECT 1")


def test_recovery_retries_past_fast_window_without_touching_control_plane(
    tmp_path, monkeypatch
):
    path = tmp_path / "store.duckdb"
    con = open_duckdb_connection(path)
    monkeypatch.setenv("DROVER_CONTROL_PLANE_PIN", "1")
    assert db_module.pin_control_plane_connection(path)
    with db_module.control_plane_connection(path) as control:
        control.execute("CREATE TABLE preserved AS SELECT 9 AS n")
    original_open = db_module._open_analytical_handle
    original_recover = db_module._recover_analytical_store
    slow_retry, release = threading.Event(), threading.Event()
    delays, attempts, recoverers = [], [], []
    first = True

    def recover(*args):
        recoverers.append(threading.current_thread())
        return original_recover(*args)

    def fail_then_recover(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            raise duckdb.FatalException("checkpoint fatal")
        attempts.append(threading.current_thread())
        if len(attempts) <= 11:
            raise duckdb.OutOfMemoryException("sustained memory pressure")
        return original_open(*args, **kwargs)

    def backoff(delay):
        delays.append(delay)
        if len(delays) == 4:
            slow_retry.set()
            assert release.wait(5)

    monkeypatch.setattr(db_module, "_open_analytical_handle", fail_then_recover)
    monkeypatch.setattr(db_module, "_recover_analytical_store", recover)
    monkeypatch.setattr(
        db_module,
        "time",
        SimpleNamespace(monotonic=time.monotonic, time=time.time, sleep=backoff),
    )
    try:
        with pytest.raises(AnalyticalStoreUnavailable):
            open_duckdb_connection(path)
        assert slow_retry.wait(2)
        assert analytical_store_health(path) == {
            "status": "failed-retrying",
            "recovery_attempts": 3,
        }
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(open_duckdb_connection, path) for _ in range(16)]
            for future in futures:
                with pytest.raises(AnalyticalStoreUnavailable):
                    future.result(timeout=1)
        db_module._invalidate_analytical_store(
            path, 0, duckdb.FatalException("late failure")
        )
        assert len(recoverers) == 1
        assert len(attempts) == 3
        with db_module.control_plane_connection(path) as same_control:
            assert same_control is control
            assert same_control.execute("SELECT * FROM preserved").fetchone() == (9,)
    finally:
        release.set()
        _wait_recovery(path)
        con.close()
        db_module.close_control_plane_connections()
    assert len(recoverers) == 1
    assert set(attempts) == set(recoverers)
    assert len(attempts) == 12
    assert delays == [0.25, 0.5, 1, 2, 4, 8, 16, 32, 60, 60, 60, 60]
    assert analytical_store_health(path) == {"status": "ok", "recovery_attempts": 12}


def test_ordinary_query_oom_does_not_invalidate(tmp_path):
    path = tmp_path / "store.duckdb"
    con = open_duckdb_connection(path)
    original = con._inner

    class OOM:
        def execute(self, *args):
            raise duckdb.OutOfMemoryException("query budget exhausted")

    con._inner = OOM()
    try:
        with pytest.raises(duckdb.OutOfMemoryException):
            con.execute("SELECT 1")
        assert analytical_store_health(path)["status"] == "ok"
    finally:
        con._inner = original
        con.close()


def test_concurrent_opens_and_cursors_share_one_recovery(tmp_path, monkeypatch):
    path = tmp_path / "store.duckdb"
    seed = open_duckdb_connection(path)
    cursor = seed.cursor()
    entered, release = threading.Event(), threading.Event()
    reset = db_module.reset_invalidated_instance
    resets = []
    opens = []
    original_open = db_module._open_analytical_handle

    def held_reset(path):
        resets.append(path)
        entered.set()
        assert release.wait(5)
        return reset(path)

    def poisoned_open(*args, **kwargs):
        opens.append(1)
        if len(opens) == 1:
            raise duckdb.FatalException("fatal checkpoint error")
        return original_open(*args, **kwargs)

    monkeypatch.setattr(db_module, "reset_invalidated_instance", held_reset)
    monkeypatch.setattr(db_module, "_open_analytical_handle", poisoned_open)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(open_duckdb_connection, path) for _ in range(8)]
            for future in futures:
                with pytest.raises(AnalyticalStoreUnavailable):
                    future.result(timeout=2)
        assert entered.wait(1)
        with pytest.raises(AnalyticalStoreUnavailable):
            seed.cursor()
        assert len(resets) == 1
        assert len(opens) == 1
    finally:
        release.set()
        _wait_recovery(path)
    assert len(opens) == 2
    # Duplicated cursors are tracked, so none keeps the poisoned instance alive.
    with pytest.raises(duckdb.ConnectionException):
        cursor._inner.execute("SELECT 1")
    seed.close()


def test_lazy_relation_failure_is_monitored(tmp_path):
    path = tmp_path / "store.duckdb"
    con = open_duckdb_connection(path)
    relation = con.sql("SELECT 1")

    class PoisonedResult:
        def fetchall(self):
            raise duckdb.FatalException("fatal checkpoint error")

    relation._inner = PoisonedResult()
    with pytest.raises(AnalyticalStoreUnavailable):
        relation.fetchall()
    _wait_recovery(path)
    con.close()


def test_snapshot_checkpoint_does_not_swallow_invalidation(tmp_path, monkeypatch):
    path = tmp_path / "store.duckdb"
    seed = open_duckdb_connection(path)
    original_connect = duckdb.connect
    fail = True

    class CheckpointFailure:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *args):
            nonlocal fail
            if sql == "CHECKPOINT" and fail:
                fail = False
                raise duckdb.FatalException("checkpoint OOM")
            result = self.inner.execute(sql, *args)
            return self if result is self.inner else result

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(
        duckdb,
        "connect",
        lambda *args, **kw: CheckpointFailure(original_connect(*args, **kw)),
    )
    with pytest.raises(AnalyticalStoreUnavailable):
        db_module._checkpoint_before_snapshot(path)
    _wait_recovery(path)
    assert not fail
    seed.close()


def test_first_set_failure_closes_off_the_request_thread(tmp_path, monkeypatch):
    path = tmp_path / "store.duckdb"
    real_connect = duckdb.connect
    closing, release = threading.Event(), threading.Event()
    first = True

    class FatalSetup:
        def execute(self, *args):
            raise duckdb.FatalException("checkpoint OOM on first SET")

        def close(self):
            closing.set()
            assert release.wait(5)

    def connect(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            return FatalSetup()
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(duckdb, "connect", connect)
    try:
        started = time.monotonic()
        with pytest.raises(AnalyticalStoreUnavailable):
            open_duckdb_connection(path)
        assert time.monotonic() - started < 1
        assert closing.wait(1)
        assert analytical_store_health(path)["status"] == "recovering"
    finally:
        release.set()
        _wait_recovery(path)


def test_query_finally_close_does_not_wait_for_recovery(tmp_path):
    path = tmp_path / "store.duckdb"
    con = open_duckdb_connection(path)
    original = con._inner
    closing, release = threading.Event(), threading.Event()

    class FatalQuery:
        def execute(self, *args):
            raise duckdb.FatalException("checkpoint OOM")

        def close(self):
            closing.set()
            assert release.wait(5)
            original.close()

    con._inner = FatalQuery()
    try:
        started = time.monotonic()
        with pytest.raises(AnalyticalStoreUnavailable):
            try:
                con.execute("SELECT 1")
            finally:
                con.close()
        assert time.monotonic() - started < 1
        assert closing.wait(1)
    finally:
        release.set()
        _wait_recovery(path)
