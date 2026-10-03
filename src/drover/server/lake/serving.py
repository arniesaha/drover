"""Explicit path-scoped selection for canonical memory reads only.

No file discovery, implicit configuration load, or legacy retry on lake errors.
The catalog proof is checked in the SAME disposable child as each SELECT.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from threading import RLock

from drover.config import AnalyticsConfig

from .query_process import query
from .runtime import LakeError, LakeSpec

_CONFIGS: dict[Path, AnalyticsConfig] = {}
_SELECTION_LOCK = RLock()


def configure_analytics(path: Path, config: AnalyticsConfig) -> None:
    with _SELECTION_LOCK:
        _CONFIGS[Path(path).resolve()] = config


def selected_config(path: Path) -> AnalyticsConfig:
    return _CONFIGS.get(Path(path).resolve(), AnalyticsConfig())


def lake_spec(config: AnalyticsConfig, *, exporter=False) -> LakeSpec:
    return LakeSpec(
        config.exporter_dsn_env if exporter else config.catalog_dsn_env,
        Path(config.data_root),
        Path(config.extension_dir),
        config.engine_sha256,
    )


class HistoryConnection:
    """Small read-only cursor facade; every execution has independent hard caps."""

    def __init__(self, config, *, identities=()):
        self.config = config
        self.identities = list(identities)
        self.description = []
        self.rows = []

    def execute(self, sql, params=None):
        result = query(
            lake_spec(self.config),
            sql,
            [
                value.isoformat() if isinstance(value, (date, datetime)) else value
                for value in (params or [])
            ],
            serving={
                "verification_sha256": self.config.verification_sha256,
                "identities": self.identities,
            },
        )
        self.description = list(zip(result["columns"], result["types"]))
        self.rows = [
            tuple(
                (
                    datetime.fromisoformat(value)
                    if value is not None and typ.startswith("TIMESTAMP")
                    else value
                )
                for value, typ in zip(row, result["types"])
            )
            for row in result["rows"]
        ]
        return self

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def open_history(path: Path):
    config = selected_config(path)
    if config.backend == "legacy":
        from drover.server.db import open_duckdb_connection

        return open_duckdb_connection(path, role="diagnostic")
    # PG is authoritative for identity. Never read the old analytical mapping.
    from drover.server.control_store import is_postgres_control_store
    from drover.server.db import control_plane_connection

    if not is_postgres_control_store(path):
        raise LakeError("lake_serving_requires_postgres")
    try:
        with control_plane_connection(path) as con:
            rows = con.execute(
                """SELECT session_id, native_session_id, summary_session_id
                FROM harness_sessions ORDER BY session_id LIMIT 10001"""
            ).fetchall()
    except Exception:
        raise LakeError("analytics_identity_unavailable") from None
    if len(rows) > 10000:
        raise LakeError("analytics_identity_limit_exceeded")
    return HistoryConnection(config, identities=rows)


def check_selected(path: Path):
    if selected_config(path).backend == "ducklake":
        with open_history(path) as con:
            con.execute("SELECT 1")
