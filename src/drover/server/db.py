"""DuckDB connection helpers for live Drover workers and diagnostics."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

import duckdb

ROLE_DEFAULTS: dict[str, dict[str, str]] = {
    "worker": {
        "memory_limit": "1GB",
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


def open_duckdb_connection(
    duckdb_path: Path,
    *,
    read_only: bool = False,
    role: str = "worker",
    settings_overrides: Optional[Mapping[str, str]] = None,
) -> duckdb.DuckDBPyConnection:
    """Open DuckDB and then apply role-specific connection settings.

    Do not pass these settings via ``duckdb.connect(config=...)``. DuckDB rejects
    opening the same database with different connection configs, which is exactly
    what live Drover workers hit when diagnostics ran beside the daemon. Applying
    settings after the connection is opened keeps connection identity compatible
    while still bounding memory/thread use.
    """
    con = duckdb.connect(str(duckdb_path), read_only=read_only)
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
