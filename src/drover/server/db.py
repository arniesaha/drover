"""DuckDB connection helpers for live Drover workers and diagnostics.

Every in-process open of the central database must go through
``open_duckdb_connection`` (or hold ``duckdb_connect_lock`` around a plain
``duckdb.connect``). Two rules keep concurrent workers, drain loops, and
diagnostics from crashing each other (issue #2):

1. **One connection configuration per file.** DuckDB's in-process instance
   cache only shares a database between connections whose configs match
   exactly; a ``read_only=True`` open beside a live read-write connection
   raises "Can't open a connection to same database file with a different
   configuration". So every connection opens read-write with no
   ``config=``, and role settings are applied via ``SET`` afterwards.
2. **Serialized connects.** Two threads calling ``duckdb.connect()`` on the
   same file at nearly the same instant race the instance cache and the
   loser raises "Binder Error: Unique file handle conflict". The connect
   call itself is serialized per resolved path, process-wide.
3. **The control plane reads somewhere else.** ``/harness*`` state goes
   through ``control_plane_connection``, which always has its own lock and
   -- when ``DROVER_CONTROL_PLANE_PIN=1`` -- its own pinned connection.
   Nothing analytical may take that lock or be handed that connection;
   ``tests/test_control_plane_isolation.py`` enforces both directions.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Mapping, Optional

import duckdb

log = logging.getLogger("drover.db")

#: Parallelism ceiling for the ``snapshot`` role. Measured against the live
#: 2.32M-row / 6,505-file store: one snapshot took 14.8s/13.7s at 1 thread,
#: 6.6s/8.1s at 4, and 6.3s/7.0s at 8. Four threads is the knee; more buys
#: nothing and costs the live server cores it still needs.
_SNAPSHOT_MAX_THREADS = 4


def snapshot_thread_default(cpu_count: int) -> int:
    """Threads for the ``snapshot`` role on a machine with ``cpu_count`` cores.

    Always leaves at least one core for the live server, so a snapshot can
    slow itself down but never starve the hub of CPU outright.
    """
    return max(1, min(_SNAPSHOT_MAX_THREADS, int(cpu_count) - 1))


ROLE_DEFAULTS: dict[str, dict[str, str]] = {
    "worker": {
        "memory_limit": "2GB",
        "threads": "2",
        "preserve_insertion_order": "false",
    },
    "summarizer": {
        "memory_limit": "2GB",
        "threads": "1",
        "preserve_insertion_order": "false",
    },
    "diagnostic": {
        "memory_limit": "2GB",
        "threads": "1",
        "preserve_insertion_order": "false",
    },
    # Read-only analytics against a *private copy* of the database, never the
    # live file. `threads` is a DuckDB instance-wide setting: raising it on a
    # connection to the live database raises it for the harness registry and
    # every other live reader sharing that instance, which is how the
    # 2026-08-04 outage happened (#91, PR #76, PR #93). A copy is a separate
    # instance with its own scheduler, so its thread count cannot reach the
    # live one. Only the OS-level CPU share is shared, and that is bounded
    # both by snapshot_thread_default (never the last core) and by the fact
    # that finishing ~2x sooner shortens the window of contention.
    #
    # Do NOT point a `snapshot` connection at the live database.
    "snapshot": {
        "memory_limit": "2GB",
        "threads": str(snapshot_thread_default(os.cpu_count() or 1)),
        "preserve_insertion_order": "false",
    },
}

_CONNECT_LOCKS: dict[str, threading.Lock] = {}
_CONNECT_LOCKS_GUARD = threading.Lock()

#: Control-plane locks live in their own table, deliberately. See
#: ``control_plane_connection``.
_CONTROL_PLANE_LOCKS: dict[str, threading.Lock] = {}
_CONTROL_PLANE_CONNECTIONS: dict[str, duckdb.DuckDBPyConnection] = {}
_CONTROL_PLANE_GUARD = threading.Lock()

#: Errors that mean the pinned connection itself is gone, so the next window
#: has to reopen. Everything else -- a ConstraintException from replaying an
#: event id, a BinderException from a bad query -- leaves the connection
#: perfectly usable, and throwing it away for those would send the following
#: window back through the shared connect lock for no reason.
_CONTROL_PLANE_FATAL = (
    duckdb.ConnectionException,
    duckdb.FatalException,
    duckdb.IOException,
    duckdb.InternalException,
)


def _path_key(duckdb_path: str | Path) -> str:
    return str(Path(duckdb_path).expanduser().resolve())


def duckdb_connect_lock(duckdb_path: str | Path) -> threading.Lock:
    """Process-wide lock serializing duckdb.connect() per resolved path.

    Analytical readers only. The control plane has ``control_plane_lock``, and
    the separation is the point -- see ``control_plane_connection``.
    """
    key = _path_key(duckdb_path)
    with _CONNECT_LOCKS_GUARD:
        lock = _CONNECT_LOCKS.get(key)
        if lock is None:
            lock = _CONNECT_LOCKS[key] = threading.Lock()
        return lock


def control_plane_lock(duckdb_path: str | Path) -> threading.Lock:
    """Process-wide lock serializing control-plane windows per resolved path.

    Never the same object as ``duckdb_connect_lock`` for the same path, so an
    analytical reader and a fleet listing cannot queue behind each other.
    """
    key = _path_key(duckdb_path)
    with _CONTROL_PLANE_GUARD:
        lock = _CONTROL_PLANE_LOCKS.get(key)
        if lock is None:
            lock = _CONTROL_PLANE_LOCKS[key] = threading.Lock()
        return lock


def pin_control_plane_connection(duckdb_path: str | Path) -> bool:
    """Open and keep the control plane's own connection to ``duckdb_path``.

    **Off unless ``DROVER_CONTROL_PLANE_PIN=1``, and deliberately so.** A pin
    holds DuckDB's exclusive file lock for the life of the process. DuckDB
    grants one process at a time write access, so on a host where the hub
    server and ``drover-harnessd`` share ``cfg.duckdb_path`` -- the
    single-machine setup in getting-started.md -- a pin would take harnessd's
    registry permanently dark, and harnessd serves its fleet endpoints from
    that registry. That they collide today is not a guess:
    ``bootstrap_harnessd_schema`` catches "Could not set lock" and carries on
    "best-effort", which works only because the server's windows are short.
    Enable it on a hub whose database file no process else opens.

    The lock split in ``control_plane_connection`` is on unconditionally and
    is the part that fixes #95's queueing; this is the further latency win,
    staged behind a flag because its blast radius is another daemon.

    Returns False when declined, and also when the connection could not be
    opened -- a database another process already owns, or a path that does not
    exist. Neither is fatal: ``control_plane_connection`` falls back to the
    pre-#95 connect-per-window path, still under the control-plane lock.
    ``bootstrap_harnessd_schema`` sets the same precedent.

    Only the process that owns the database file should ever call this;
    harnessd and the CLI never do.
    """
    if os.environ.get("DROVER_CONTROL_PLANE_PIN", "0").strip().lower() not in {
        "1",
        "true",
        "yes",
    }:
        log.info(
            "control-plane connection not pinned (set DROVER_CONTROL_PLANE_PIN=1 "
            "on a hub whose DuckDB file no other process opens); /harness* keeps "
            "its own lock either way"
        )
        return False
    key = _path_key(duckdb_path)
    with control_plane_lock(duckdb_path):
        with _CONTROL_PLANE_GUARD:
            if key in _CONTROL_PLANE_CONNECTIONS:
                return True
        try:
            con = _connect_control_plane(duckdb_path)
        except duckdb.Error as exc:
            log.warning(
                "control plane could not pin %s (%s); "
                "falling back to a connection per request",
                duckdb_path,
                exc,
            )
            return False
        with _CONTROL_PLANE_GUARD:
            _CONTROL_PLANE_CONNECTIONS[key] = con
    return True


def close_control_plane_connections() -> None:
    """Release every pinned control-plane connection.

    The server calls this on shutdown so the next process can take the
    database lock instead of racing a connection its predecessor still holds.
    """
    with _CONTROL_PLANE_GUARD:
        connections = list(_CONTROL_PLANE_CONNECTIONS.items())
        _CONTROL_PLANE_CONNECTIONS.clear()
    for key, con in connections:
        try:
            con.close()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            log.debug("failed to close the control-plane connection for %s", key)


def _connect_control_plane(duckdb_path: str | Path) -> duckdb.DuckDBPyConnection:
    """Open one control-plane connection, serialized against other connects.

    This is the only moment the control plane touches ``duckdb_connect_lock``:
    two threads racing ``duckdb.connect()`` on one file still lose to a
    "Unique file handle conflict" (see the module docstring), and a pin is
    opened once per process. No role settings are applied on purpose --
    ``threads`` and ``memory_limit`` are DuckDB *instance* settings, so
    writing them here would reconfigure every analytical reader sharing the
    instance, and the control plane's indexed lookups do not care what they
    are set to.
    """
    with duckdb_connect_lock(duckdb_path):
        return duckdb.connect(str(duckdb_path))


@contextmanager
def control_plane_connection(
    duckdb_path: str | Path,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield the control plane's connection, under the control plane's lock.

    Issue #95: ``/healthz`` answered in 0.7ms while every ``/harness*``
    endpoint timed out at 20-40s, three times, with three different
    analytical scans to blame. The scans were never the whole story --
    ``HarnessRegistry`` opened a connection per window while holding the
    process-wide connect lock, so a fleet listing queued behind whichever
    worker happened to be connecting, and a connect on a saturated instance
    is where ``sample(1)`` found threads parked.

    Two things break that coupling, and both are needed:

    * **Its own lock.** Nothing analytical waits on it and it waits on
      nothing analytical, so control-plane latency stops being a function of
      any worker's query plan.
    * **Optionally a pinned connection**
      (``DROVER_CONTROL_PLANE_PIN=1``). The remaining coupling is that a
      connect is itself the call that blocks -- ``sample(1)`` found threads
      parked in ``DuckDBPyConnection::Connect`` on a saturated instance -- and
      a pin removes the connect along with the per-window instance teardown:
      measured at 3.5ms against a 279MB file versus 0.1ms with the instance
      held, and 300-500ms per window on the live hub (see
      ``_TerminalMirror``). It is off by default because it holds the file
      lock against a co-resident harnessd; see
      ``pin_control_plane_connection``.

    What this does *not* buy: a private DuckDB instance. One file is one
    instance in one process -- a second ``duckdb.connect`` joins the cached
    instance and ``ATTACH`` from a separate instance raises "Unique file
    handle conflict" -- so the task scheduler stays shared. Splitting the
    control-plane tables into their own file is the remaining step (option 1
    in #95); this is the staging post that stops the queueing.

    Without a pin -- the default, and always for harnessd and the CLI -- the
    window opens a connection of its own, still under the control-plane lock,
    and still holds the shared connect lock only across the connect itself
    rather than the whole window. That alone is the fix for the queueing.
    """
    key = _path_key(duckdb_path)
    with control_plane_lock(duckdb_path):
        with _CONTROL_PLANE_GUARD:
            con = _CONTROL_PLANE_CONNECTIONS.get(key)
        if con is not None:
            try:
                yield con
            except _CONTROL_PLANE_FATAL:
                # A pin that died has to be replaced, or the control plane
                # would stay broken until someone restarted the server --
                # which is the mitigation this exists to retire. The window
                # that found it dead still fails; every control-plane caller
                # already logs and carries on.
                _discard_control_plane_connection(key)
                raise
            return
        con = _connect_control_plane(duckdb_path)
        try:
            yield con
        finally:
            con.close()


def _discard_control_plane_connection(key: str) -> None:
    with _CONTROL_PLANE_GUARD:
        con = _CONTROL_PLANE_CONNECTIONS.pop(key, None)
    if con is None:
        return
    try:
        con.close()
    except Exception:  # noqa: BLE001 - already failing, nothing to salvage
        log.debug("failed to close the failed control-plane connection for %s", key)


def open_duckdb_connection(
    duckdb_path: Path,
    *,
    read_only: bool = False,
    role: str = "worker",
    settings_overrides: Optional[Mapping[str, str]] = None,
) -> duckdb.DuckDBPyConnection:
    """Open DuckDB and then apply role-specific connection settings.

    The connection is always opened read-write: a read-only open has a
    different connection config, which DuckDB rejects while any read-write
    connection to the same file is alive (see module docstring). Callers
    that used ``read_only`` were diagnostics running inside the server
    process beside live writers. The one read-only semantic kept is that a
    missing database file errors instead of being silently created.
    """
    if read_only and not Path(duckdb_path).exists():
        raise duckdb.IOException(
            f"Cannot open database {str(duckdb_path)!r} in read-only mode: "
            "database does not exist"
        )
    with duckdb_connect_lock(duckdb_path):
        con = duckdb.connect(str(duckdb_path))
    settings = dict(ROLE_DEFAULTS.get(role, ROLE_DEFAULTS["worker"]))
    prefix = f"DUCKDB_{role.upper()}"
    # `snapshot` deliberately does not fall back to the diagnostic or worker
    # env vars: the whole point of the role is that throttling live readers
    # must not throttle the isolated copy, and vice versa.
    fallback_prefix = prefix if role in {"diagnostic", "snapshot"} else "DUCKDB_WORKER"

    def _env_setting(suffix: str, default: str) -> str:
        for name in (
            f"DROVER_{prefix}_{suffix}",
            f"DROVER_{fallback_prefix}_{suffix}",
        ):
            value = os.environ.get(name)
            if value is not None:
                return value
        return default

    settings["memory_limit"] = _env_setting("MEMORY_LIMIT", settings["memory_limit"])
    settings["threads"] = _env_setting("THREADS", settings["threads"])
    if settings_overrides:
        settings.update({str(k): str(v) for k, v in settings_overrides.items()})

    try:
        con.execute("SET memory_limit=?", [settings["memory_limit"]])
        con.execute("SET threads=?", [int(settings["threads"])])
        con.execute(
            "SET preserve_insertion_order=?",
            [settings["preserve_insertion_order"].lower() == "true"],
        )
    except Exception:
        con.close()
        raise
    return con
