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
    fallback_prefix = "DUCKDB_DIAGNOSTIC" if role == "diagnostic" else "DUCKDB_WORKER"

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
