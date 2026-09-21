"""Pooled PostgreSQL implementation of Drover's central serving store."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from drover.config import ControlStoreConfig
from drover.server.control_store import bind_qmark_parameters

log = logging.getLogger("drover.postgres_control_store")


class ControlStoreBusy(RuntimeError):
    """The bounded PostgreSQL pool could not provide a session in time."""


class PostgresCursor:
    """Match the small DuckDB cursor surface used by existing control callers."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @property
    def description(self):
        description = self._cursor.description
        if description is None:
            return None
        return tuple((column.name,) for column in description)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size: int | None = None):
        return self._cursor.fetchmany(size)


class PostgresControlConnection:
    """qmark-compatible session wrapper with no DuckDB locking semantics."""

    dialect = "postgres"

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> PostgresCursor:
        cursor = self._connection.execute(
            bind_qmark_parameters(sql), tuple(params or ())
        )
        return PostgresCursor(cursor)

    def executemany(self, sql: str, params: Sequence[Sequence[Any]]) -> PostgresCursor:
        cursor = self._connection.cursor()
        cursor.executemany(bind_qmark_parameters(sql), params)
        return PostgresCursor(cursor)

    def interrupt(self) -> None:
        self._connection.cancel()


class PostgresControlStore:
    """Bounded pool and schema-scoped sessions for an explicit central path."""

    def __init__(self, config: ControlStoreConfig) -> None:
        if config.backend != "postgres":
            raise ValueError("PostgresControlStore requires postgres configuration")
        dsn = os.environ.get(config.dsn_env, "").strip()
        if not dsn:
            raise RuntimeError(
                f"PostgreSQL control store DSN is missing from {config.dsn_env}"
            )
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise RuntimeError(
                "PostgreSQL control store requires drover[postgres]"
            ) from exc

        self.config = config
        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=config.pool_min_size,
            max_size=config.pool_max_size,
            kwargs={"autocommit": True},
            configure=self._configure_connection,
            open=True,
        )
        # Fail during explicit bootstrap/configuration, never later in the
        # first authenticated fleet request. The DSN itself is never logged.
        try:
            self._pool.wait(timeout=config.acquire_timeout_seconds)
        except Exception as exc:
            self.close()
            raise RuntimeError(
                "PostgreSQL control store could not open its pool"
            ) from exc

    def _configure_connection(self, connection: Any) -> None:
        from psycopg import sql

        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(self.config.schema))
        )
        # Control-plane columns use TIMESTAMPTZ.  Make every pooled session
        # render and bind timestamps against a stable server-independent zone.
        connection.execute("SET TIME ZONE 'UTC'")
        connection.execute(
            "SELECT set_config('statement_timeout', %s, false)",
            [str(int(self.config.statement_timeout_seconds * 1000))],
        )

    @contextmanager
    def connection(
        self, timeout: float | None = None
    ) -> Iterator[PostgresControlConnection]:
        acquire_timeout = (
            self.config.acquire_timeout_seconds if timeout is None else timeout
        )
        try:
            with self._pool.connection(timeout=max(0.0, acquire_timeout)) as connection:
                yield PostgresControlConnection(connection)
        except Exception as exc:
            try:
                from psycopg_pool import PoolTimeout
            except ImportError:  # pragma: no cover - construction already checks
                PoolTimeout = ()  # type: ignore[assignment]
            if isinstance(exc, PoolTimeout):
                raise ControlStoreBusy(
                    f"the PostgreSQL control-store pool was busy after {acquire_timeout:.2f}s"
                ) from exc
            raise

    def bootstrap(self) -> None:
        from drover.server.postgres_schema import bootstrap_postgres_control_store

        bootstrap_postgres_control_store(self)

    def close(self) -> None:
        self._pool.close()
