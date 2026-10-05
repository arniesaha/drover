"""Control-schema drift guards: frozen sha256 table and full normalized schema equality against committed prod dump."""

from __future__ import annotations

import hashlib
import logging
from uuid import uuid4

import psycopg
import pytest
from conftest import is_pgvector_supported, restore_prod_control_schema

from drover.config import ControlStoreConfig
from drover.server.postgres_control_store import PostgresControlStore
from drover.server.postgres_schema import (
    _MIGRATIONS,
    VECTOR_MIGRATION,
    _vector_migration_statements,
    bootstrap_postgres_control_store,
)

#: Frozen table of sha256 per released migration 1..11 computed from code's migration definitions.
#: Any change to an already-released migration will fail CI.
RELEASED_MIGRATION_HASHES: dict[int, str] = {
    1: "f1982d26c425aea2c00be04ba80e393d1dce5dd27db1eeb98998d6b93d60eacf",
    2: "3228bfd9a7e26123637a9cbb2fb27bd4056a5b016c8df6f71ff12813324f46e2",
    3: "6fe7d32e0a69f2c9d3ffafb92f8be932ea3a0778052f98b407669636608dfcdd",
    4: "6ef6d3d8ab2620a9b8aa5f6838c5252442db514bc814f05ecc85dbd85d2be0af",
    5: "e92a9ed2bca50acecc2b6b9b38ceed2da96ecc566fd71c62bc8e95adf7f24250",
    6: "aecf02345db0d46371984a3374fbfae8f01181b79a61e88752a2f840dfbc24d5",
    7: "f9df7d74be5c409109ac4ab3fd5b8cfe4b12c1784d84a8e1cfd7ea1916feba78",
    8: "260d99cc374968e715b0d2982e79721611569f1420837b094a85e3d87ec5fe3a",
    9: "6e50195a7553163ee34f8aaca0ac9788c572b258af7a8726dbc4c874d65b5140",
    10: "193bed1a46e92510b7123b9e01edfdaba0fc43756698a12f42db40ff112bd4f3",
    11: "39258199d2f697391fbd70891a1b133244b8b6bb0f454e2faeac14907a9ef0b1",
}


def _statements_hash(statements: tuple[str, ...] | list[str]) -> str:
    """Hash the exact statement text, in order; whitespace counts."""
    return hashlib.sha256("\x00".join(statements).encode("utf-8")).hexdigest()


def _compute_current_hashes() -> dict[int, str]:
    hashes = {version: _statements_hash(stmts) for version, stmts in _MIGRATIONS}
    hashes[VECTOR_MIGRATION] = _statements_hash(
        [
            statement.as_string(None)
            for statement in _vector_migration_statements("public")
        ]
    )
    return hashes


def test_drift_guard_a_released_migrations_sha256_frozen():
    """Drift Guard (a): Sha256 of released migrations 1..11 must match the frozen hash table."""
    current = _compute_current_hashes()

    mismatched = {}
    for version, expected_hash in RELEASED_MIGRATION_HASHES.items():
        actual_hash = current.get(version)
        if actual_hash != expected_hash:
            mismatched[version] = (expected_hash, actual_hash)

    assert not mismatched, (
        f"Released migration hash mismatch detected: {mismatched}. "
        "Released migrations 1..11 are immutable once released (#503). "
        "Never edit an existing released migration definition."
    )


def _extract_normalized_schema(con: psycopg.Connection, schema_name: str) -> dict:
    """Extract normalized tables, columns, indexes, and constraints from PostgreSQL."""
    tables = sorted(
        [
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE'",
                [schema_name],
            ).fetchall()
        ]
    )

    columns = {}
    for t in tables:
        cols = con.execute(
            """
            SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
              AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attname
            """,
            [schema_name, t],
        ).fetchall()
        columns[t] = cols

    indexes = {}
    prefix1 = f'"{schema_name}".'
    prefix2 = f"{schema_name}."
    for t in tables:
        idxs = con.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s ORDER BY indexname",
            [schema_name, t],
        ).fetchall()
        indexes[t] = [
            (
                name,
                defn.replace(prefix1, "").replace(prefix2, "").replace("public.", ""),
            )
            for name, defn in idxs
        ]

    constraints = {}
    for t in tables:
        constrs = con.execute(
            """
            SELECT c.conname, c.contype, pg_get_constraintdef(c.oid)
            FROM pg_constraint c
            JOIN pg_namespace n ON n.oid = c.connamespace
            JOIN pg_class cl ON cl.oid = c.conrelid
            WHERE n.nspname = %s AND cl.relname = %s
            ORDER BY c.conname
            """,
            [schema_name, t],
        ).fetchall()
        constraints[t] = [
            (
                name,
                ctype,
                defn.replace(prefix1, "").replace(prefix2, "").replace("public.", ""),
            )
            for name, ctype, defn in constrs
        ]

    return {
        "tables": tables,
        "columns": columns,
        "indexes": indexes,
        "constraints": constraints,
    }


def test_drift_guard_b_migrated_schema_equals_committed_prod_dump(
    postgres_dsn, monkeypatch
):
    """Drift Guard (b): Applying code's migrations 1..11 equals the committed prod dump.

    Catches bugs like #503 where a released migration was edited and fresh migrations
    diverged from production.
    """
    from drover.server import postgres_schema

    # This guard compares the released production baseline, not future migrations.
    monkeypatch.setattr(
        postgres_schema,
        "_MIGRATIONS",
        tuple(
            (version, statements)
            for version, statements in _MIGRATIONS
            if version <= 11
        ),
    )
    has_vector = is_pgvector_supported(postgres_dsn)
    prod_schema = f"prod_dump_{uuid4().hex[:8]}"
    migrated_schema = f"migrated_{uuid4().hex[:8]}"

    dsn_env = f"DROVER_DRIFT_DSN_{uuid4().hex[:8].upper()}"
    monkeypatch.setenv(dsn_env, postgres_dsn)

    # 1. Restore committed dump into prod_schema
    restore_prod_control_schema(postgres_dsn, target_schema=prod_schema)

    # 2. Apply code's migrations 1..11 from scratch into migrated_schema
    cfg = ControlStoreConfig(
        backend="postgres",
        dsn_env=dsn_env,
        pool_min_size=1,
        pool_max_size=2,
        acquire_timeout_seconds=5.0,
        statement_timeout_seconds=5.0,
        schema=migrated_schema,
    )
    store = PostgresControlStore(cfg)
    try:
        bootstrap_postgres_control_store(store)
    finally:
        store.close()

    try:
        with psycopg.connect(postgres_dsn, autocommit=True) as con:
            prod_s = _extract_normalized_schema(con, prod_schema)
            migr_s = _extract_normalized_schema(con, migrated_schema)

            # If pgvector is unavailable on this Postgres instance, migration 8
            # (session_embeddings) is conditional in bootstrap_postgres_control_store.
            # Column TYPE is strictly verified for all tables (including vector for
            # session_embeddings when pgvector is present).
            if not has_vector:
                logging.getLogger(__name__).warning(
                    "pgvector extension is missing on this Postgres instance; "
                    "relaxing drift guard (b) for session_embeddings table"
                )
                # Compare all tables except session_embeddings
                prod_tables = [t for t in prod_s["tables"] if t != "session_embeddings"]
                migr_tables = [t for t in migr_s["tables"] if t != "session_embeddings"]
            else:
                prod_tables = prod_s["tables"]
                migr_tables = migr_s["tables"]

            assert prod_tables == migr_tables, (
                f"Table mismatch between committed prod dump and migrated schema: "
                f"prod extra={set(prod_tables) - set(migr_tables)}, "
                f"migrated extra={set(migr_tables) - set(prod_tables)}"
            )

            for table in prod_tables:
                prod_cols = prod_s["columns"][table]
                migr_cols = migr_s["columns"][table]
                assert (
                    prod_cols == migr_cols
                ), f"Column mismatch in table '{table}': prod={prod_cols}, migr={migr_cols}"

                prod_idxs = prod_s["indexes"][table]
                migr_idxs = migr_s["indexes"][table]
                assert (
                    prod_idxs == migr_idxs
                ), f"Index mismatch in table '{table}': prod={prod_idxs}, migr={migr_idxs}"

                prod_constrs = prod_s["constraints"][table]
                migr_constrs = migr_s["constraints"][table]
                assert (
                    prod_constrs == migr_constrs
                ), f"Constraint mismatch in table '{table}': prod={prod_constrs}, migr={migr_constrs}"

    finally:
        with psycopg.connect(postgres_dsn, autocommit=True) as con:
            con.execute(f'DROP SCHEMA IF EXISTS "{prod_schema}" CASCADE')
            con.execute(f'DROP SCHEMA IF EXISTS "{migrated_schema}" CASCADE')
