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
3. **The control plane reads somewhere else.** ``/harness*`` state lives in
   its own database *file* (``control_plane_path``) and is reached through
   ``control_plane_connection``, which has its own lock and -- when
   ``DROVER_CONTROL_PLANE_PIN=1`` -- its own pinned connection. A separate
   file is what makes it a separate DuckDB instance, with its own scheduler,
   buffer manager and ``memory_limit``; a separate connection to the same
   file is none of those things (issue #95, 2026-08-11). Nothing analytical
   may take that lock, be handed that connection, or open that file;
   ``tests/test_control_plane_isolation.py`` and
   ``tests/test_control_plane_store.py`` enforce every direction.
4. **Handed-out connections are remembered, weakly.** ``/readyz`` has to
   prove the live DuckDB instance still answers, and the cheapest honest way
   to do that is to borrow a connection this process already holds rather
   than open one of its own (issue #175). ``remember_live_connection`` and
   ``live_connections`` are that register; the references are weak, so
   nothing here extends a connection's life or holds the file lock.
5. **Analytical readers see control-plane state through a copy.** Two live
   queries genuinely join across both worlds, and DuckDB refuses to ``ATTACH``
   a file another instance in the process already holds ("Unique file handle
   conflict"). ``attached_control_plane_snapshot`` attaches a private copy of
   the (small) control-plane store instead -- #76's pattern, at megabytes
   rather than 700MB.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import itertools
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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
    # Analytical roles sized for the post-#249/#260 loaders: 2GB was sized for
    # the pre-#249 advisory loaders (routing loader peaked at 2210 MB); #249
    # dropped those to ~300 MB, and the #260 fix (raw_data kept out of the
    # cockpit dedup window) brought the worst remaining query, the 30-day
    # cockpit activity build, to ~9-11s at ~1.6-1.7 GB RSS at both 2GB and 1GB
    # limits (measured 2026-08-31 on a production-store copy; 512MB OOMs, so
    # 1GB is the floor with headroom). DuckDB spills to <dbfile>.tmp (on the
    # external data volume via the ~/.drover symlink), so an underestimate
    # degrades to disk, not OOM-crash. Note the env override footgun in one
    # line: `DROVER_DUCKDB_WORKER_MEMORY_LIMIT` covers worker+summarizer only;
    # `diagnostic`/`snapshot` need their own `DROVER_DUCKDB_DIAGNOSTIC_*`/
    # `DROVER_DUCKDB_SNAPSHOT_*` vars (see `_apply_role_settings`).
    "worker": {
        "memory_limit": "1GB",
        "threads": "2",
        "preserve_insertion_order": "false",
    },
    "summarizer": {
        "memory_limit": "1GB",
        "threads": "1",
        "preserve_insertion_order": "false",
    },
    "diagnostic": {
        "memory_limit": "1GB",
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
        "memory_limit": "1GB",
        "threads": str(snapshot_thread_default(os.cpu_count() or 1)),
        "preserve_insertion_order": "false",
    },
    # The control-plane store, and only it. This role exists to be *small*.
    #
    # `memory_limit` is a DuckDB instance-wide setting, so before the store was
    # split every role's "2GB" was one 2GB budget shared by the control plane
    # and every analytical worker. On 2026-08-11 the hub logged
    # "failed to allocate 16.0 MiB (1.8 GiB/1.8 GiB used)" from the cockpit and
    # "failed to pin block of size 4.0 KiB (1.8 GiB/1.8 GiB used)" from the
    # advisory worker within minutes of each other. A budget the control plane
    # cannot lose is the point of the split.
    #
    # 256MB against a working set of 3 hosts, 120 sessions and 41,649 events:
    # the whole store materialized uncompressed is tens of megabytes, and the
    # widest statement here (`latest_session_previews`, a window function over
    # the events of at most ~120 sessions) touches a fraction of it. That is
    # several times the headroom it can use, and one eighth of the analytical
    # budget -- so the two instances together stay well inside the ~6GB free on
    # a 16GB host that is already paging. It must also be set explicitly: a
    # fresh instance otherwise defaults to ~80% of host RAM, which on this
    # machine would be 12.7 GiB and would trade one failure for a worse one.
    #
    # Two threads, not one: everything here is an indexed point lookup except
    # that one window function. The setting cannot leak into analytics, because
    # this role is only ever applied to a different file.
    "control_plane": {
        "memory_limit": "256MB",
        "threads": "2",
        "preserve_insertion_order": "false",
    },
}

#: Tables that belong to the control plane and live in ``control_plane_path``.
#: The recap queue and its projection are here because ``HarnessRegistry``
#: writes them in the same transaction as the event that triggers them.
CONTROL_PLANE_TABLES = (
    "harness_hosts",
    "harness_sessions",
    "harness_events",
    "live_session_recaps",
    "live_recap_jobs",
    "advisory_findings",
    "advisory_occurrences",
    "session_usage",
    "session_usage_sources",
    "native_usage_partition_totals",
    "native_usage_partition_watermarks",
)

#: Primary keys, used by the migration to copy without duplicating.
CONTROL_PLANE_PRIMARY_KEYS = {
    "harness_hosts": "host_id",
    "harness_sessions": "session_id",
    "harness_events": "event_id",
    "live_session_recaps": "session_id",
    "live_recap_jobs": "session_id",
    "advisory_findings": "finding_id",
    "advisory_occurrences": "occurrence_id",
    "session_usage": "session_id",
    "session_usage_sources": "source_usage_id",
    "native_usage_partition_totals": "native_usage_partition_id",
    "native_usage_partition_watermarks": "partition_date",
}

#: Appended to the analytical store's stem, so ``drover.duckdb`` is joined by
#: ``drover.registry.duckdb`` in the same directory. Derived from the stem
#: rather than fixed, so two lakehouses sharing a directory (which the test
#: suite does constantly) do not share one control plane.
CONTROL_PLANE_SUFFIX = ".registry.duckdb"

#: Prefix for the catalog alias a snapshot copy is attached under. Deliberately
#: unlikely to collide with a real catalog name; a per-window counter is
#: appended because ``ATTACH`` is instance-wide and readers overlap.
CONTROL_PLANE_SNAPSHOT_ALIAS = "drover_control_plane"

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


class ControlPlaneBusy(RuntimeError):
    """A control-plane window was not free inside the caller's budget.

    Issue #181. Raised only for a caller that passed ``timeout=`` to
    ``control_plane_connection``; everyone else still waits its turn, because
    for the registry and the recap worker "later" is the right answer and
    "never mind" is not. It says nothing about the store's health -- the store
    was never reached -- which is why readiness reports it as ``busy`` rather
    than as a failure.
    """


#: Analytical connections this process has handed out, held *weakly* per
#: resolved path. Readiness borrows one of these to prove the live DuckDB
#: instance still answers (issue #175); nothing else reads it, and a strong
#: reference here would keep instances -- and DuckDB's exclusive file lock --
#: alive long after their owner finished with them.
_LIVE_CONNECTIONS: dict[str, "weakref.WeakSet[duckdb.DuckDBPyConnection]"] = {}
_LIVE_GUARD = threading.Lock()

#: Empty sets are swept once the table grows past this. The sets empty
#: themselves, but their *keys* would not: every metrics refresh opens a
#: snapshot copy under a fresh temporary path, which is a new key each minute
#: for the life of the process.
_LIVE_PATHS_SOFT_MAX = 64

#: The last failed open per path, so readiness can tell a store nothing can
#: open from a store nothing happens to have open. Guarded by ``_LIVE_GUARD``.
_CONNECT_FAILURES: dict[str, tuple[float, str]] = {}

#: Well past any window a reader would count a failure inside; swept only to
#: keep the table from growing one key per temporary snapshot path.
_CONNECT_FAILURE_MAX_AGE_SECONDS = 3600.0


def _path_key(duckdb_path: str | Path) -> str:
    return str(Path(duckdb_path).expanduser().resolve())


def remember_live_connection(
    duckdb_path: str | Path, con: duckdb.DuckDBPyConnection
) -> None:
    """Record a connection so readiness can borrow the instance behind it.

    Called by ``open_duckdb_connection`` for every analytical open. The set is
    weak, so an entry disappears with the connection object and no bookkeeping
    is needed on the close path.
    """
    key = _path_key(duckdb_path)
    with _LIVE_GUARD:
        handles = _LIVE_CONNECTIONS.get(key)
        if handles is None:
            handles = _LIVE_CONNECTIONS[key] = weakref.WeakSet()
        handles.add(con)
        # An open that worked retires whatever the last one that failed said.
        _CONNECT_FAILURES.pop(key, None)
        if len(_LIVE_CONNECTIONS) > _LIVE_PATHS_SOFT_MAX:
            for stale in [
                path
                for path, entries in _LIVE_CONNECTIONS.items()
                if path != key and not entries
            ]:
                del _LIVE_CONNECTIONS[stale]


def live_connections(duckdb_path: str | Path) -> list[duckdb.DuckDBPyConnection]:
    """Connections to ``duckdb_path`` this process still holds, if any.

    An empty list means the analytical DuckDB *instance* is gone too: it is
    kept alive by its connections, and DuckDB's instance cache holds only a
    weak reference. That is why "nothing open" is not a readiness failure --
    the next connect builds a fresh instance, which cannot be an invalidated
    one.
    """
    key = _path_key(duckdb_path)
    with _LIVE_GUARD:
        handles = _LIVE_CONNECTIONS.get(key)
        return list(handles) if handles else []


def live_connection(
    duckdb_path: str | Path,
) -> duckdb.DuckDBPyConnection | None:
    """One connection to ``duckdb_path``, or None when the process holds none."""
    handles = live_connections(duckdb_path)
    return handles[0] if handles else None


def remember_connect_failure(duckdb_path: str | Path, exc: BaseException) -> None:
    """Record that a real open of ``duckdb_path`` just failed.

    A store nobody can open leaves nothing for readiness to borrow, so without
    this a lakehouse too broken to connect to would look exactly like an idle
    one (issue #175). What is kept here is a real worker's real error, which
    costs nothing to collect and is more truthful than a probe of our own.
    """
    now = time.monotonic()
    with _LIVE_GUARD:
        _CONNECT_FAILURES[_path_key(duckdb_path)] = (
            now,
            f"{type(exc).__name__}: {exc}",
        )
        if len(_CONNECT_FAILURES) > _LIVE_PATHS_SOFT_MAX:
            # Same sweep as the handle register, for the same reason: one key
            # per temporary snapshot path would otherwise be kept for the life
            # of the process. Nothing reads a failure this old.
            for stale in [
                path
                for path, (when, _) in _CONNECT_FAILURES.items()
                if now - when > _CONNECT_FAILURE_MAX_AGE_SECONDS
            ]:
                del _CONNECT_FAILURES[stale]


def last_connect_failure(duckdb_path: str | Path) -> tuple[float, str] | None:
    """The most recent failed open of ``duckdb_path``: (age in seconds, error).

    Cleared by the next open that succeeds, so a store that recovered stops
    reporting one.
    """
    with _LIVE_GUARD:
        failure = _CONNECT_FAILURES.get(_path_key(duckdb_path))
    if failure is None:
        return None
    when, message = failure
    return time.monotonic() - when, message


def control_plane_path(duckdb_path: str | Path) -> Path:
    """Where the control plane's own database file lives.

    Issue #95. ``/harness*`` state is 3 hosts, 120 sessions and 41,649 events;
    it was living inside a 640.8 MB analytical store, which meant it shared
    that store's DuckDB instance -- one scheduler, one buffer manager, one
    ``memory_limit`` -- with every parquet scan in the process. Its own file is
    the only thing that separates them; PR #104 gave it its own lock and its
    own connection and the wedge recurred on ``c7900e7`` regardless.

    Idempotent: handed the control-plane path it returns it unchanged, so a
    caller that has already resolved (or an operator who configured the store
    directly) does not end up with ``drover.registry.registry.duckdb``.

    ``DROVER_CONTROL_PLANE_DUCKDB`` overrides the location outright, for a hub
    that wants the control plane on different storage from the lakehouse.
    """
    override = os.environ.get("DROVER_CONTROL_PLANE_DUCKDB", "").strip()
    if override:
        return Path(override).expanduser()
    path = Path(duckdb_path).expanduser()
    if path.name.endswith(CONTROL_PLANE_SUFFIX):
        return path
    return path.with_name(path.stem + CONTROL_PLANE_SUFFIX)


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
    analytical reader and a fleet listing cannot queue behind each other. The
    path is resolved to the control-plane store first, so passing the
    analytical path here still locks the control plane and never the lakehouse.
    """
    key = _path_key(control_plane_path(duckdb_path))
    with _CONTROL_PLANE_GUARD:
        lock = _CONTROL_PLANE_LOCKS.get(key)
        if lock is None:
            lock = _CONTROL_PLANE_LOCKS[key] = threading.Lock()
        return lock


def pin_control_plane_connection(duckdb_path: str | Path) -> bool:
    """Open and keep the control plane's own connection to its store.

    **Off unless ``DROVER_CONTROL_PLANE_PIN=1``, and deliberately so.** A pin
    holds DuckDB's exclusive file lock for the life of the process. DuckDB
    grants one process at a time write access, so on a host where the hub
    server and ``drover-harnessd`` share ``cfg.duckdb_path`` -- the
    single-machine setup in getting-started.md -- they now also share the
    control-plane store derived from it, and a pin would take harnessd's
    registry permanently dark. That they collide today is not a guess:
    ``bootstrap_harnessd_schema`` catches "Could not set lock" and carries on
    "best-effort", which works only because the server's windows are short.
    Enable it on a hub whose control-plane store no process else opens.

    The lock split in ``control_plane_connection`` is on unconditionally and
    is the part that fixes #95's queueing; this is the further latency win,
    staged behind a flag because its blast radius is another daemon.

    Returns False when declined, and also when the connection could not be
    opened -- a database another process already owns, or a path that does not
    exist. Neither is fatal: ``control_plane_connection`` falls back to the
    pre-#95 connect-per-window path, still under the control-plane lock.
    ``bootstrap_harnessd_schema`` sets the same precedent.

    Only the process that owns the database file should ever call this;
    harnessd and the CLI never do. ``duckdb_path`` may be either the analytical
    path or the control-plane path; it is resolved either way.
    """
    duckdb_path = control_plane_path(duckdb_path)
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


#: A control-plane connect collides only with another process's *window*, and
#: those are milliseconds long (the pin that would remove them is off by
#: default, see `pin_control_plane_connection`). Retrying briefly turns a
#: dropped heartbeat into a slightly slower one. Measured live: 48 collisions,
#: each surfacing as an HTTP 500 from `/harness/hosts` and losing that poll.
_CONTROL_PLANE_LOCK_ATTEMPTS = 4
_CONTROL_PLANE_LOCK_BACKOFF_SECONDS = 0.05


def _is_lock_conflict(exc: BaseException) -> bool:
    """Whether this is another process holding the file, not a broken store.

    Only the collision is worth retrying. A missing or corrupt database is a
    condition the caller needs reported now, not after a delay.
    """

    return "could not set lock" in str(exc).lower()


def _connect_control_plane(duckdb_path: str | Path) -> duckdb.DuckDBPyConnection:
    """Open one control-plane connection, serialized against other connects.

    This is the only moment the control plane touches ``duckdb_connect_lock``:
    two threads racing ``duckdb.connect()`` on one file still lose to a
    "Unique file handle conflict" (see the module docstring), and a pin is
    opened once per process. The lock is keyed on the control-plane path, so
    it is a different lock from the analytical store's and no analytical
    connect can be waiting on it.

    Role settings *are* applied, and that is new with the store split. They are
    DuckDB instance settings; while the control plane shared the analytical
    file, writing them here would have reconfigured every analytical reader on
    that instance, so #104 deliberately left them alone. Now the file is its
    own instance, an unset ``memory_limit`` would default to ~80% of host RAM,
    and setting it is the only way the control plane gets a budget an
    analytical scan cannot spend.
    """
    duckdb_path = control_plane_path(duckdb_path)
    with duckdb_connect_lock(duckdb_path):
        for attempt in range(_CONTROL_PLANE_LOCK_ATTEMPTS):
            try:
                con = duckdb.connect(str(duckdb_path))
                break
            except duckdb.IOException as exc:
                last = attempt == _CONTROL_PLANE_LOCK_ATTEMPTS - 1
                if last or not _is_lock_conflict(exc):
                    raise
                time.sleep(_CONTROL_PLANE_LOCK_BACKOFF_SECONDS * (attempt + 1))
    try:
        _apply_role_settings(con, "control_plane")
    except Exception:
        con.close()
        raise
    return con


@contextmanager
def _held_control_plane_lock(
    duckdb_path: str | Path, timeout: float | None
) -> Iterator[None]:
    """Hold the control-plane lock, optionally giving up rather than waiting.

    ``timeout=None`` is the historical behaviour and stays the default: a
    control-plane caller that gives up has nothing useful to do instead. A
    caller that must answer on a deadline -- ``/readyz``, and so far only
    ``/readyz`` -- passes a budget and gets ``ControlPlaneBusy`` when the
    window in front of it has not finished (#181).
    """
    lock = control_plane_lock(duckdb_path)
    if timeout is None:
        lock.acquire()
    elif not lock.acquire(timeout=max(0.0, timeout)):
        raise ControlPlaneBusy(
            f"the control-plane window was still busy after {timeout:.2f}s"
        )
    try:
        yield
    finally:
        lock.release()


@contextmanager
def control_plane_connection(
    duckdb_path: str | Path,
    *,
    timeout: float | None = None,
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

    * **Its own database file**, which is what makes it a private DuckDB
      instance. One file is one instance in one process, so while the registry
      tables lived in ``drover.duckdb`` every ``/harness*`` read shared that
      store's scheduler, buffer manager and ``memory_limit`` with every
      parquet scan -- whichever lock it held, pinned or not. The lock split
      shipped in #104 and the wedge recurred on ``c7900e7`` for exactly this
      reason. ``duckdb_path`` is resolved through ``control_plane_path``, so
      this function can never open the analytical store.

    Without a pin -- the default, and always for harnessd and the CLI -- the
    window opens a connection of its own, still under the control-plane lock,
    and still holds the shared connect lock only across the connect itself
    rather than the whole window.

    ``timeout`` bounds the wait for the window itself and raises
    ``ControlPlaneBusy`` instead of queueing. It is off by default: waiting is
    right for every caller that has work to do and wrong only for one that has
    to answer a monitor now (#181).
    """
    key = _path_key(control_plane_path(duckdb_path))
    with _held_control_plane_lock(duckdb_path, timeout):
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
    try:
        with duckdb_connect_lock(duckdb_path):
            con = duckdb.connect(str(duckdb_path))
        try:
            _apply_role_settings(con, role, settings_overrides=settings_overrides)
        except Exception:
            con.close()
            raise
    except Exception as exc:
        # Remembered, then re-raised unchanged: callers keep their error, and
        # readiness gains the one piece of evidence a borrowed handle cannot
        # give it -- that this store cannot be opened at all (#175).
        remember_connect_failure(duckdb_path, exc)
        raise
    remember_live_connection(duckdb_path, con)
    return con


def _apply_role_settings(
    con: duckdb.DuckDBPyConnection,
    role: str,
    *,
    settings_overrides: Optional[Mapping[str, str]] = None,
) -> None:
    """Apply one role's settings to an already-open connection.

    Every one of these is a DuckDB *instance* setting, not a connection
    setting, so which file the connection is on decides who else they land on.
    """
    settings = dict(ROLE_DEFAULTS.get(role, ROLE_DEFAULTS["worker"]))
    prefix = f"DUCKDB_{role.upper()}"
    # `snapshot` and `control_plane` deliberately do not fall back to the
    # diagnostic or worker env vars: the whole point of both roles is that
    # throttling live analytical readers must not throttle them, or vice versa.
    fallback_prefix = (
        prefix
        if role in {"diagnostic", "snapshot", "control_plane"}
        else "DUCKDB_WORKER"
    )

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

    con.execute("SET memory_limit=?", [settings["memory_limit"]])
    con.execute("SET threads=?", [int(settings["threads"])])
    con.execute(
        "SET preserve_insertion_order=?",
        [settings["preserve_insertion_order"].lower() == "true"],
    )


def sql_path_literal(value: str | Path) -> str:
    """Quote a path for ``ATTACH``, which takes no bind parameters."""
    return "'" + str(value).replace("'", "''") + "'"


@contextmanager
def control_plane_snapshot(duckdb_path: str | Path) -> Iterator[Path | None]:
    """Yield a private copy of the control-plane store, or None if it is absent.

    The copy is #76's pattern applied to a much smaller file. It is isolation,
    not caching: DuckDB refuses to ``ATTACH`` a database another instance in
    the same process already holds ("Unique file handle conflict"), so an
    analytical reader in the hub *cannot* attach the live control-plane store
    even if it wanted to, and attaching it from the analytical instance would
    put the two back on one scheduler anyway.

    What made this unaffordable in #76 was size -- 700MB per read, growing.
    The control-plane store is the 3 hosts, 120 sessions and 41,649 events
    that used to sit inside it, so the same objection does not apply.

    One copy is nonetheless reused until the store changes, keyed on its mtime
    and size. These readers are not occasional: ``AdvisoryScheduler`` asks each
    of six analyzers for a source version on the advisory worker's 5s poll, so
    a copy per window would be a permanent write load on a host that is already
    paging. A generation a reader still holds is never overwritten or deleted --
    an attached DuckDB file has to stay exactly as it was attached -- so a
    refresh writes a new file and the old one goes when its last reader leaves.
    """
    source = control_plane_path(duckdb_path)
    if not source.exists():
        log.debug("no control-plane store at %s; skipping snapshot", source)
        yield None
        return
    entry = _acquire_control_plane_snapshot(source)
    try:
        yield entry.path
    finally:
        _release_control_plane_snapshot(source, entry)


@dataclass
class _CachedSnapshot:
    """One copy of the control-plane store, and whether anyone holds it."""

    path: Path
    signature: tuple[int, int, int, int]
    busy: bool = False


#: At most one *idle* copy is kept per store. Concurrent readers each get their
#: own file, because DuckDB refuses to attach one file twice in a process
#: ("Unique file handle conflict") and the cockpit and advisory workers are
#: separate threads on the same analytical instance. Sequential readers -- the
#: common case, and the frequent one -- reuse the idle copy instead of writing
#: a fresh one every few hundred milliseconds.
_SNAPSHOT_IDLE: dict[str, _CachedSnapshot] = {}
_SNAPSHOT_DIRS: dict[str, tempfile.TemporaryDirectory] = {}
_SNAPSHOT_SEQUENCE = itertools.count()
_SNAPSHOT_GUARD = threading.Lock()


def _write_ahead_log(source: Path) -> Path:
    """DuckDB's WAL for ``source``, whether or not it currently exists."""
    return source.with_name(source.name + ".wal")


def _snapshot_signature(source: Path) -> tuple[int, int, int, int]:
    """Identify the store's contents, including anything only in the WAL.

    Both files count. DuckDB checkpoints on last close, so an unpinned hub
    usually has an empty WAL -- but a pinned one, or a co-resident harnessd
    holding the store, does not, and a signature blind to the WAL would serve a
    cached copy that is missing every row written since the last checkpoint.
    """
    stat = source.stat()
    wal = _write_ahead_log(source)
    wal_stat = wal.stat() if wal.exists() else None
    return (
        stat.st_mtime_ns,
        stat.st_size,
        wal_stat.st_mtime_ns if wal_stat else 0,
        wal_stat.st_size if wal_stat else 0,
    )


SNAPSHOT_SCRATCH_DIRNAME = ".drover-snapshots"


def snapshot_scratch_root(source: Path | str) -> Path:
    """Where snapshot copies of ``source`` are written: beside ``source``.

    Not the system temp directory, for two reasons that happen to be the same
    reason. ``clonefile`` only works within a volume, so a snapshot written to
    a temp dir on a different filesystem silently falls back to the chunked
    read the clone exists to replace. And these copies are hundreds of
    megabytes arriving every few minutes -- pointed at the boot volume they
    filled it and took the hub down, while the store itself sits on a volume
    with room to spare (#171).

    Landing them beside the store fixes both: the clone engages, which also
    makes the copy nearly free, because copy-on-write extents cost no space
    until they diverge.
    """
    root = Path(source).parent / SNAPSHOT_SCRATCH_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def sweep_orphaned_snapshot_scratch(
    source: Path | str, *, older_than_seconds: float = 3600.0
) -> int:
    """Delete abandoned snapshot directories beside ``source``; return the count.

    ``TemporaryDirectory`` only cleans up when the process exits gracefully, and
    a hub that is killed -- or restarted by launchd -- does not. Copies from
    every previous process therefore accumulated indefinitely: 145 directories
    and 27 GB by the time the volume ran out.

    The age cutoff is what keeps this safe to call while other work is in
    flight: a directory another process is actively filling has a recent mtime,
    so only genuinely abandoned ones are swept.
    """
    root = Path(source).parent / SNAPSHOT_SCRATCH_DIRNAME
    if not root.is_dir():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for child in root.iterdir():
        try:
            if not child.is_dir() or child.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    if removed:
        log.info("swept %d orphaned snapshot director(ies) under %s", removed, root)
    return removed


def _clone_file(source: Path, destination: Path) -> bool:
    """APFS copy-on-write clone of ``source``, or False if unavailable.

    The point is atomicity, not speed -- though it is also O(1). ``clonefile``
    captures the file's extents as one operation, so a writer working on the
    store cannot be observed half way, which a chunked read can and did.
    """
    if sys.platform != "darwin":
        return False
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        clonefile = libc.clonefile
    except (OSError, AttributeError):
        return False
    clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    clonefile.restype = ctypes.c_int
    if clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0:
        return True
    # Cross-volume and non-APFS both land here (EXDEV / ENOTSUP), and both are
    # ordinary rather than exceptional -- the caller falls back.
    log.debug(
        "clonefile(%s -> %s) unavailable: %s",
        source,
        destination,
        os.strerror(ctypes.get_errno()),
    )
    return False


def copy_duckdb_store(source: Path, destination: Path) -> None:
    """Capture a DuckDB store as a snapshot that always opens.

    Two properties, learned the hard way when a snapshot invalidated the live
    handle and the hub served nothing while looking healthy (#171):

    **The store is captured atomically.** ``shutil.copy2`` reads in chunks, so
    copying a store DuckDB is writing into yields a torn file -- the source of
    "Invalid bitmask for FixedSizeAllocator" and "Could not find node in column
    segment tree!". An APFS clone takes the extents in one operation instead.

    **The WAL is not carried.** The store and its log are two files captured at
    two instants, so a copy taking both can only vouch for the pair by luck,
    and the existence check ahead of it races a checkpoint deleting the file.
    A store on its own is a valid database as of its last checkpoint, so the
    cost is staleness rather than corruption -- and a checkpoint first keeps
    even that small. Stale is a trade a snapshot can make; unopenable is not.
    """
    _checkpoint_before_snapshot(source)
    if not _clone_file(source, destination):
        # Off-APFS or across volumes. Still no WAL, so the pairing hazard is
        # gone, but a chunked read can tear on its own -- callers treat a
        # failed snapshot as a skipped cycle.
        shutil.copy2(source, destination)


def _checkpoint_before_snapshot(source: Path) -> None:
    """Fold the WAL into the store so the snapshot is not needlessly stale.

    Best effort on purpose. ``CHECKPOINT`` fails while another transaction is
    open, and that is a normal state for a live hub, not an error worth failing
    a snapshot over: without it the capture is simply as of the previous
    checkpoint, which is the trade this function is already making.
    """
    if not source.exists():
        return
    try:
        with duckdb_connect_lock(source):
            con = duckdb.connect(str(source))
            try:
                con.execute("CHECKPOINT")
            finally:
                con.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("checkpoint before snapshot of %s skipped: %s", source, exc)


def _acquire_control_plane_snapshot(source: Path) -> _CachedSnapshot:
    """Return a copy of ``source`` as it is now, marked as in use.

    Reuses the idle copy when the store has not changed since it was taken.
    Otherwise writes a new one, under the guard on purpose: copying outside it
    and publishing afterwards would let every concurrent reader copy the same
    file at once, which is the pile-up this exists to prevent.
    """
    key = _path_key(source)
    signature = _snapshot_signature(source)
    with _SNAPSHOT_GUARD:
        idle = _SNAPSHOT_IDLE.get(key)
        if idle is not None and idle.signature == signature:
            del _SNAPSHOT_IDLE[key]
            idle.busy = True
            return idle
        if idle is not None:
            del _SNAPSHOT_IDLE[key]
            _remove_snapshot(idle)

        directory = _SNAPSHOT_DIRS.get(key)
        if directory is None:
            directory = _SNAPSHOT_DIRS[key] = tempfile.TemporaryDirectory(
                prefix="drover-control-plane-", dir=snapshot_scratch_root(source)
            )
        fresh = Path(directory.name) / (
            f"{source.stem}-{next(_SNAPSHOT_SEQUENCE)}{source.suffix}"
        )
        copy_duckdb_store(source, fresh)
        # Stamped with the signature read *before* the copy, not after. The
        # bytes describe the store as it was going in, so claiming they match
        # what it became on the way out is how a copy taken across a change got
        # served again and again to later readers (#171).
        return _CachedSnapshot(path=fresh, signature=signature, busy=True)


def _release_control_plane_snapshot(source: Path, entry: _CachedSnapshot) -> None:
    """Park the copy for the next reader, or delete it if one is already parked."""
    key = _path_key(source)
    entry.busy = False
    with _SNAPSHOT_GUARD:
        if key in _SNAPSHOT_IDLE:
            _remove_snapshot(entry)
            return
        _SNAPSHOT_IDLE[key] = entry


def _remove_snapshot(entry: _CachedSnapshot) -> None:
    try:
        entry.path.unlink(missing_ok=True)
        _write_ahead_log(entry.path).unlink(missing_ok=True)
    except OSError:
        log.debug("failed to remove the control-plane snapshot %s", entry.path)


@contextmanager
def attached_control_plane_snapshot(
    con: duckdb.DuckDBPyConnection,
    duckdb_path: str | Path,
) -> Iterator[None]:
    """Let one analytical connection read control-plane tables, from a copy.

    Two live queries genuinely join across both worlds and must keep working
    after the split:

    * ``advisory/worker.py`` -- ``spans_enriched`` JOIN ``bounded_sessions``,
      which derives from ``harness_sessions``.
    * ``cockpit/analytics.py`` -- ``harness_sessions`` correlated with span
      sessions via ``EXISTS (SELECT 1 FROM span_sessions ...)``.

    Their SQL is left exactly as it was. Each control-plane table is exposed as
    a ``TEMP VIEW`` over the attached copy, and DuckDB resolves the temp
    catalog first, so ``FROM harness_sessions`` reads the copy. That also
    shadows the pre-split tables still sitting in ``drover.duckdb``: the
    migration leaves them in place so a rollback is a restart rather than a
    data-recovery exercise, and shadowing is what stops a reader silently
    answering from that frozen copy.

    Temp views belong to one connection, so nothing leaks to another reader,
    and both they and the attachment are released on the way out.

    The catalog alias is unique per window. ``ATTACH`` is instance-wide rather
    than per connection, and the cockpit and the advisory worker are two threads
    on the same analytical instance -- a shared alias would fail the second one
    with "database with name ... already exists" and then detach the first one's
    snapshot out from under its query.
    """
    with control_plane_snapshot(duckdb_path) as snapshot:
        if snapshot is None:
            yield
            return
        alias = f"{CONTROL_PLANE_SNAPSHOT_ALIAS}_{next(_SNAPSHOT_SEQUENCE)}"
        con.execute(f"ATTACH {sql_path_literal(snapshot)} AS {alias} (READ_ONLY)")
        try:
            for table in CONTROL_PLANE_TABLES:
                con.execute(
                    f"CREATE OR REPLACE TEMP VIEW {table} AS "
                    f"SELECT * FROM {alias}.{table}"
                )
            yield
        finally:
            for table in CONTROL_PLANE_TABLES:
                try:
                    con.execute(f"DROP VIEW IF EXISTS temp.{table}")
                except Exception:  # noqa: BLE001 - teardown is best effort
                    log.debug("failed to drop the temp view for %s", table)
            try:
                con.execute(f"DETACH {alias}")
            except Exception:  # noqa: BLE001 - an interrupted query may have
                # left the connection unable to run anything; the temp copy is
                # removed by the TemporaryDirectory either way.
                log.debug("failed to detach the control-plane snapshot")
