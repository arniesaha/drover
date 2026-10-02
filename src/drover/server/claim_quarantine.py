"""Keep a single DuckDB index constraint failure from blocking a job queue."""

from __future__ import annotations

import logging

import duckdb

CONSTRAINT_ERROR = "constraint: index ghost; requeued"


class ClaimQuarantine:
    def __init__(self, table: str, key_column: str, logger: logging.Logger) -> None:
        # Identifiers are supplied only by worker code, never by job payloads.
        self.table = table
        self.key_column = key_column
        self.log = logger
        self.skipped: set[str] = set()

    def exclusion(self, column: str) -> tuple[str, list[str]]:
        keys = sorted(self.skipped)
        if not keys:
            return "", []
        return f" AND {column} NOT IN ({', '.join('?' for _ in keys)})", keys

    def quarantine(self, con, key: str, error: duckdb.ConstraintException) -> None:
        if key in self.skipped:
            return
        self.skipped.add(key)
        outcome = "memory_skip"
        quarantine_error = None
        try:
            cur = con.execute(
                f"SELECT * FROM {self.table} WHERE {self.key_column}=?", [key]
            )
            columns = [d[0] for d in cur.description]
            row = cur.fetchone()
            if row is not None:
                values = dict(zip(columns, row))
                values.update(status="errored", last_error=CONSTRAINT_ERROR)
                # Separate autocommit statements: do not UPDATE the indexed row,
                # or retain the deleted key in the INSERT's transaction.
                con.execute(
                    f"DELETE FROM {self.table} WHERE {self.key_column}=?", [key]
                )
                con.execute(
                    f"INSERT INTO {self.table} ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    [values[c] for c in columns],
                )
                outcome = "errored"
        except duckdb.Error as exc:
            # A broken DELETE/INSERT must not undo isolation of the claim.
            quarantine_error = str(exc)
        self.log.warning(
            "job claim constraint: table=%s key=%s quarantine=%s error=%s quarantine_error=%s",
            self.table,
            key,
            outcome,
            error,
            quarantine_error,
            extra={
                "job_table": self.table,
                "job_key": key,
                "quarantine": outcome,
                "quarantine_error": quarantine_error,
            },
        )

    def execute(self, con, query: str, parameters: list, key: str):
        if key in self.skipped:
            return None
        try:
            return con.execute(query, parameters).fetchone()
        except duckdb.ConstraintException as exc:
            self.quarantine(con, key, exc)
            return None
