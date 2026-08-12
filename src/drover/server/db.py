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
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Mapping, Optional

import duckdb

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


def duckdb_connect_lock(duckdb_path: str | Path) -> threading.Lock:
    """Process-wide lock serializing duckdb.connect() per resolved path."""
    key = str(Path(duckdb_path).expanduser().resolve())
    with _CONNECT_LOCKS_GUARD:
        lock = _CONNECT_LOCKS.get(key)
        if lock is None:
            lock = _CONNECT_LOCKS[key] = threading.Lock()
        return lock


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
