"""Released control-schema migrations are immutable.

``bootstrap_postgres_control_store`` records each applied version in
``control_schema_migrations`` and never re-runs it. Editing a migration that a
store has already applied therefore changes nothing on that store: fresh
stores get the new DDL, existing ones silently do not. #503 did exactly that
(``lake_export_batches`` inside migration 2) and the production DuckLake
exporter failed with UndefinedTable while every scratch rehearsal passed.

A migration counts as released once it merges to ``main``. To change the
schema, append a new version to ``_MIGRATIONS`` with idempotent DDL
(``IF NOT EXISTS`` and friends) and pin its hash below in the same PR. Never
edit a pinned hash to make this test pass: that is the bug this test exists
to catch.
"""

from __future__ import annotations

import hashlib

#: sha256 of each released migration's statements (see ``_statements_hash``).
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
    14: "436cfca2e4ddfce2827210f4437208664da062a000f9aeb470805a74b2b29a64",
    13: "480f8a74a4a43dae2115df2e789c727d23efaf18ec8947e39997c01ffc26503b",
    12: "835df35a9e5043c218fcfcd792a1fd84386cdb31443b25058e54b2fff9f2f30d",
}


def _statements_hash(statements) -> str:
    """Hash the exact statement text, in order; whitespace counts."""
    return hashlib.sha256("\x00".join(statements).encode()).hexdigest()


def _current_hashes() -> dict[int, str]:
    from drover.server.postgres_schema import (
        _MIGRATIONS,
        VECTOR_MIGRATION,
        _vector_migration_statements,
    )

    hashes = {version: _statements_hash(stmts) for version, stmts in _MIGRATIONS}
    # The conditional pgvector migration composes SQL around the extension
    # schema; hash its rendering for the default ``public`` schema.
    hashes[VECTOR_MIGRATION] = _statements_hash(
        statement.as_string(None)
        for statement in _vector_migration_statements("public")
    )
    return hashes


def test_released_migrations_are_unchanged():
    current = _current_hashes()
    changed = sorted(
        version
        for version, pinned in RELEASED_MIGRATION_HASHES.items()
        if current.get(version) != pinned
    )
    assert not changed, (
        f"released control-schema migration(s) {changed} changed or were removed. "
        "Existing stores never re-run an applied version: revert the edit and "
        "add a new migration version instead (see this module's docstring)."
    )


def test_every_migration_has_a_pinned_hash():
    unpinned = sorted(set(_current_hashes()) - set(RELEASED_MIGRATION_HASHES))
    assert not unpinned, (
        f"pin migration(s) {unpinned} in RELEASED_MIGRATION_HASHES; once merged "
        "they are immutable."
    )


def test_migration_versions_are_unique_and_ascending():
    from drover.server.postgres_schema import _MIGRATIONS, VECTOR_MIGRATION

    versions = [version for version, _ in _MIGRATIONS]
    assert versions == sorted(set(versions))
    assert VECTOR_MIGRATION not in versions


def test_lake_export_batches_backfill_matches_migration_2():
    """Migration 11 recreates exactly the DDL #503 put into migration 2."""
    from drover.server.postgres_schema import _MIGRATIONS

    by_version = dict(_MIGRATIONS)

    def ddl(version: int) -> list[str]:
        return [s for s in by_version[version] if "lake_export_batches" in s]

    assert len(ddl(2)) == 1
    assert ddl(11) == ddl(2)
    assert "IF NOT EXISTS" in ddl(11)[0]


def test_lake_export_batches_migration_11_forward(pg_control_path):
    from drover.server.control_store import postgres_control_store
    from drover.server.postgres_schema import bootstrap_postgres_control_store

    store = postgres_control_store(pg_control_path)

    with store.connection() as con:
        con.execute("DROP TABLE lake_export_batches")
        con.execute("DELETE FROM control_schema_migrations WHERE version = 11")

    bootstrap_postgres_control_store(store)

    with store.connection() as con:
        columns = [
            row[0]
            for row in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'lake_export_batches' AND table_schema = current_schema()"
            ).fetchall()
        ]
        assert len(columns) == 6
        assert set(columns) == {
            "batch_id",
            "catalog_id",
            "input_json",
            "input_sha256",
            "receipt_sha256",
            "acknowledged_at",
        }

        versions = [
            v
            for (v,) in con.execute(
                "SELECT version FROM control_schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert versions == list(range(1, 15))


def test_lifecycle_13_fresh_existing_and_rerun(pg_control_path):
    from drover.server.control_store import postgres_control_store
    from drover.server.lifecycle_schema import SESSION_COLUMNS
    from drover.server.postgres_schema import bootstrap_postgres_control_store

    store = postgres_control_store(pg_control_path)
    with store.connection() as con:
        assert (
            con.execute("SELECT 1 FROM session_lifecycle_operations LIMIT 1").fetchone()
            is None
        )
        # Recreate the version-12 table shape to exercise the additive upgrade.
        con.execute("DROP TABLE session_lifecycle_operations")
        for column in SESSION_COLUMNS:
            con.execute(f"ALTER TABLE harness_sessions DROP COLUMN {column}")
        con.execute("DELETE FROM control_schema_migrations WHERE version = 13")
    bootstrap_postgres_control_store(store)
    bootstrap_postgres_control_store(store)
    with store.connection() as con:
        assert (
            con.execute(
                "SELECT count(*) FROM control_schema_migrations WHERE version = 13"
            ).fetchone()[0]
            == 1
        )
        columns = {
            r[0]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'harness_sessions'"
            ).fetchall()
        }
        assert set(SESSION_COLUMNS) <= columns


def test_lifecycle_14_fresh_existing_and_rerun(pg_control_path):
    from drover.server.control_store import postgres_control_store
    from drover.server.postgres_schema import bootstrap_postgres_control_store

    store = postgres_control_store(pg_control_path)
    with store.connection() as con:
        for table in ("session_publications", "session_worktrees"):
            assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            con.execute(f"DROP TABLE {table}")
        con.execute("DELETE FROM control_schema_migrations WHERE version = 14")
    bootstrap_postgres_control_store(store)
    bootstrap_postgres_control_store(store)
    with store.connection() as con:
        assert (
            con.execute(
                "SELECT count(*) FROM control_schema_migrations WHERE version = 14"
            ).fetchone()[0]
            == 1
        )
        assert con.execute("SELECT count(*) FROM session_worktrees").fetchone()[0] == 0
