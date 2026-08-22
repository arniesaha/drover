"""A control-plane connect must survive another process's brief window.

DuckDB grants one process at a time write access to a file. The hub opens the
control plane per window (the pin is off by default), so any other opener --
a CLI command, a second server started by mistake -- owns the file for the
length of its own window. The hub's next connect lands inside it and fails.

Captured live: 48 such failures, surfacing as

    WARNING drover.metrics: failed to register harness host nas:
    IO Error: Could not set lock on file "drover.registry.duckdb"

Each one returns HTTP 500 and drops that heartbeat. The fleet self-heals on
the next poll 15s later, which is why hosts stayed "online" throughout -- but
losing a window to a collision that lasts milliseconds is avoidable.
"""

import time

import duckdb
import pytest

from drover.server import db as db_mod


class _LockedFor:
    """Fail `n` connects with DuckDB's real lock error, then succeed."""

    def __init__(self, n, real):
        self.remaining = n
        self.real = real
        self.attempts = 0

    def __call__(self, *args, **kwargs):
        self.attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise duckdb.IOException(
                'Could not set lock on file "x.duckdb": Conflicting lock is '
                "held in /usr/bin/python (PID 1) by user someone."
            )
        return self.real(*args, **kwargs)


def _control_plane(tmp_path):
    return tmp_path / "drover.duckdb"


def test_a_brief_conflicting_window_is_retried_not_surfaced(tmp_path, monkeypatch):
    fake = _LockedFor(2, duckdb.connect)
    monkeypatch.setattr(db_mod.duckdb, "connect", fake)

    con = db_mod._connect_control_plane(_control_plane(tmp_path))
    try:
        assert con.execute("SELECT 1").fetchone()[0] == 1
    finally:
        con.close()
    assert fake.attempts == 3, "it should have retried past both failures"


def test_a_lock_held_throughout_still_raises(tmp_path, monkeypatch):
    # Retrying forever would trade a fast error for a hung request. A holder
    # that never lets go must still surface, and as the original error.
    fake = _LockedFor(10_000, duckdb.connect)
    monkeypatch.setattr(db_mod.duckdb, "connect", fake)

    with pytest.raises(duckdb.IOException, match="Could not set lock"):
        db_mod._connect_control_plane(_control_plane(tmp_path))


def test_giving_up_is_bounded_so_a_request_cannot_hang(tmp_path, monkeypatch):
    fake = _LockedFor(10_000, duckdb.connect)
    monkeypatch.setattr(db_mod.duckdb, "connect", fake)

    started = time.monotonic()
    with pytest.raises(duckdb.IOException):
        db_mod._connect_control_plane(_control_plane(tmp_path))
    assert time.monotonic() - started < 5


def test_an_unrelated_io_error_is_not_retried(tmp_path, monkeypatch):
    # Only the lock collision is transient. Retrying a missing file or a
    # corrupt store just delays an error the caller needs now.
    class _Broken:
        def __init__(self):
            self.attempts = 0

        def __call__(self, *a, **k):
            self.attempts += 1
            raise duckdb.IOException("Cannot open file: no such file or directory")

    fake = _Broken()
    monkeypatch.setattr(db_mod.duckdb, "connect", fake)
    with pytest.raises(duckdb.IOException):
        db_mod._connect_control_plane(_control_plane(tmp_path))
    assert fake.attempts == 1, "a non-lock error must surface immediately"
