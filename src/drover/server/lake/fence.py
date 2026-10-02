"""Dedicated PostgreSQL connections for exporter and drained lifecycle fences."""

from contextlib import contextmanager

import psycopg

from .runtime import LakeError

# Keys are database-scoped; every process addressing this catalog uses them.
MUTATION_LOCK = 0x4452564C414B4501
READER_LOCK = 0x4452564C414B4502


class MutationFence:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.connection = None

    def __enter__(self):
        self.connection = psycopg.connect(self.dsn, autocommit=True, connect_timeout=2)
        try:
            row = self.connection.execute(
                "SELECT pg_try_advisory_lock(%s)", [MUTATION_LOCK]
            ).fetchone()
            if not row[0]:
                raise LakeError("lake_mutation_fenced")
        except Exception:
            self.connection.close()
            self.connection = None
            raise
        return self

    def check(self):
        # This connection is dedicated, never borrowed from a pool or reconnected.
        try:
            if self.connection is None or self.connection.closed:
                raise LakeError("lake_fence_lost")
            held = self.connection.execute(
                """SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory'
                AND pid=pg_backend_pid() AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
                AND classid=%s AND objid=%s AND objsubid=1
                AND mode='ExclusiveLock' AND granted)""",
                [MUTATION_LOCK >> 32, MUTATION_LOCK & 0xFFFFFFFF],
            ).fetchone()[0]
            if not held:
                raise LakeError("lake_fence_lost")
        except Exception:
            raise LakeError("lake_fence_lost") from None

    def __exit__(self, *exc):
        if self.connection:
            self.connection.close()
        self.connection = None


@contextmanager
def reader_fence(dsn: str):
    with psycopg.connect(dsn, autocommit=True, connect_timeout=2) as con:
        con.execute("SET statement_timeout = '5s'")
        con.execute("SELECT pg_advisory_lock_shared(%s)", [READER_LOCK])
        try:
            yield
        finally:
            con.execute("SELECT pg_advisory_unlock_shared(%s)", [READER_LOCK])


@contextmanager
def drained_mutation(dsn: str):
    with MutationFence(dsn) as fence:
        fence.connection.execute("SET statement_timeout = '5s'")
        fence.connection.execute("SELECT pg_advisory_lock(%s)", [READER_LOCK])
        yield fence
