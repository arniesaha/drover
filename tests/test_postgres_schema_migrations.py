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

from drover.server.postgres_schema import (
    CONTINUITY_LEASES_MIGRATION,
    CONTINUITY_REAP_OPERATIONS_MIGRATION,
    CONTINUITY_OWNER_HANDOFF_MIGRATION,
    CONTINUITY_OWNER_HANDOFF_TOKEN_MIGRATION,
    CONTINUITY_RULES_MIGRATION,
    DAILY_DIGEST_LEASE_MIGRATION,
    DAILY_DIGEST_MIGRATION,
    EXPORTER_FRESHNESS_MIGRATION,
    EXPORTER_RESTART_GUARD_MIGRATION,
    GITHUB_ENVIRONMENT_APPROVALS_MIGRATION,
    GITHUB_WORK_ITEM_EVENTS_MIGRATION,
    HUB_WORK_ITEMS_MIGRATION,
    PROFILE_MIGRATION,
    WORK_ITEM_EVENTS_MIGRATION,
    WORKER_STATES_MIGRATION,
)

#: sha256 of each released migration's statements (see ``_statements_hash``).
RELEASED_MIGRATION_HASHES: dict[int, str] = {
    CONTINUITY_REAP_OPERATIONS_MIGRATION: "43e30bb3eeab42896842f23cf2d1655d623084e94c80149e87baa8eca5887927",
    CONTINUITY_LEASES_MIGRATION: "c5dcca12c002a87f87d5c3f8efaeca6049e15688d64f080e6ab5f4d551de4991",
    WORKER_STATES_MIGRATION: "aa9ff5f3db8d55fe6629fea8dcddc545f9c8e4e52ddb5a0ad74737d2b426f0de",
    CONTINUITY_OWNER_HANDOFF_TOKEN_MIGRATION: "30bf38eb05be55650ca75c256d7fd6f25e0c3712dc9084da22a4d2c485f8dea6",
    CONTINUITY_OWNER_HANDOFF_MIGRATION: "fbf01d89ff51d8b58df9d96833a729fa2d9c193f67eff3d896b3deea637289f4",
    DAILY_DIGEST_LEASE_MIGRATION: "f41b2434fbc8bd9eb448aef4d4dd15fd06feeb99e4ffbb247c3e1d74ed9d8b00",
    DAILY_DIGEST_MIGRATION: "11c61a9a70ead29656640c05c5750c4494f4788db787281b310afd570c0057b0",
    GITHUB_ENVIRONMENT_APPROVALS_MIGRATION: "c7807e38be214573f517f5b8818bad66e477f246ac3e3aecfec798e1d2479eea",
    CONTINUITY_RULES_MIGRATION: "c9f7d3fee1f230c5739a64bcc580ef4ca3ced6cd7075649714a381dcd327b3b9",
    GITHUB_WORK_ITEM_EVENTS_MIGRATION: "e87f807d050efffa526c1878984b5e976cca5685732e6711b30b31e1ff7f1df0",
    WORK_ITEM_EVENTS_MIGRATION: "7323ab4d004d889ba2d7856f7c9e2af86796288d6ba566deb7a1cc9e0760624e",
    HUB_WORK_ITEMS_MIGRATION: "14a41ff01c0c4c10ba305572a415c35324f28af785fa90cb773e1d09d8565d4f",
    22: "59435a76787934e90c01fe728e69ba26690410f3f8faa685e951578e657e1a5c",
    21: "ef468d3f44709b4f120177ead79dcf501eae9a1437cb5f7894d69df4b9f90088",
    20: "c95db9e518f8e9ea47ee4dc79dfbea63b48993a027b61613f0a6f88845920d13",
    EXPORTER_RESTART_GUARD_MIGRATION: "095352196aeecf2c6f86e819465f33f993e54b4bcfceda46dc65f9bb245d31b8",
    18: "6e9b1d706f78fd8674e913da95ca6bc5ceb88112b0c7c5766f434f5eceed01dc",
    EXPORTER_FRESHNESS_MIGRATION: "83b89a5b043a659612309179679d835d35a2c09c4dfd36bb801c7488de27352d",
    17: "fda9bab1f66135d72af01bbe366c9ecc7edda43161498e602a0c83dd63e5d9a0",
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
    PROFILE_MIGRATION: "939908c9cb262952f61f0feb9957f0d4c4e886a86fdd1752cd3ee20a83056ef0",
}


def _statements_hash(statements) -> str:
    """Hash the exact statement text, in order; whitespace counts."""
    return hashlib.sha256("\x00".join(statements).encode()).hexdigest()


def _current_hashes() -> dict[int, str]:
    from drover.server.postgres_schema import (
        _MIGRATIONS,
        DAILY_DIGEST_MIGRATION,
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
    assert sorted([*versions, VECTOR_MIGRATION]) == list(range(1, max(versions) + 1))


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
    from drover.server.postgres_schema import (
        _MIGRATIONS,
        DAILY_DIGEST_MIGRATION,
        VECTOR_MIGRATION,
        bootstrap_postgres_control_store,
        vector_extension_schema,
    )

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
        expected = {version for version, _ in _MIGRATIONS}
        if vector_extension_schema(con) is not None:
            expected.add(VECTOR_MIGRATION)
        assert versions == sorted(expected)


def test_event_quarantine_migration_22_forward_and_terminal_receipt(pg_control_path):
    from drover.server.control_store import postgres_control_store
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.postgres_schema import bootstrap_postgres_control_store

    store = postgres_control_store(pg_control_path)
    with store.connection() as con:
        con.execute("DROP TABLE harness_event_quarantine")
        con.execute("DELETE FROM control_schema_migrations WHERE version = 22")
    bootstrap_postgres_control_store(store)
    registry = HarnessRegistry(pg_control_path)
    with store.connection() as con:
        con.execute(
            "INSERT INTO harness_events (event_id, session_id, event_type, payload_json, "
            "normalized_source, seq) VALUES ('rejected', 'missing', 'status', '{}', 'structured', 1)"
        )
    assert len(registry.list_events_for_reconciliation()) == 1
    registry.quarantine_events(
        [{"event_id": "rejected", "session_id": "missing"}],
        "session_missing_after_registration_window",
    )
    registry.mark_events_reconciled(["rejected"])
    assert registry.list_events_for_reconciliation() == []
    assert registry.prune_reconciled_events() == 0
    with store.connection() as con:
        assert con.execute(
            "SELECT reconciled_at FROM harness_events WHERE event_id = 'rejected'"
        ).fetchone() == (None,)
        assert con.execute(
            "SELECT reason FROM harness_event_quarantine"
        ).fetchone() == ("session_missing_after_registration_window",)


def test_harness_events_reconciliation_migration_21_forward(pg_control_path):
    """An existing PostgreSQL registry receives the pending cursor additively."""
    from drover.server.control_store import postgres_control_store
    from drover.server.postgres_schema import bootstrap_postgres_control_store

    store = postgres_control_store(pg_control_path)
    with store.connection() as con:
        con.execute("ALTER TABLE harness_events DROP COLUMN reconcile_pending")
        con.execute("DELETE FROM control_schema_migrations WHERE version = 21")
        con.execute(
            "INSERT INTO harness_events "
            "(event_id, session_id, event_type, payload_json, reconciled_at) "
            "VALUES (?, ?, ?, ?, now())",
            ["previously-reconciled", "legacy-session", "status", "{}"],
        )

    bootstrap_postgres_control_store(store)
    bootstrap_postgres_control_store(store)

    with store.connection() as con:
        assert con.execute(
            "SELECT reconcile_pending FROM harness_events WHERE event_id = ?",
            ["previously-reconciled"],
        ).fetchone() == (False,)
        assert con.execute(
            "SELECT count(*) FROM control_schema_migrations WHERE version = 21"
        ).fetchone() == (1,)
        indexes = {
            row[0]
            for row in con.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = current_schema() AND tablename = 'harness_events'"
            ).fetchall()
        }
    assert {
        "harness_events_session_seq",
        "harness_events_reconciliation_pending",
    }.issubset(indexes)


def test_lifecycle_13_fresh_existing_and_rerun(pg_control_path):
    from drover.server.control_store import postgres_control_store
    from drover.server.lifecycle_schema import SESSION_COLUMNS
    from drover.server.postgres_schema import bootstrap_postgres_control_store

    store = postgres_control_store(pg_control_path)
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(pg_control_path)
    registry.register_host(
        host_id="migration-host", display_name="Migration host", kind="linux"
    )
    registry.create_session(
        session_id="existing-session",
        host_id="migration-host",
        harness="shell",
        command="sh",
        status="running",
    )
    registry.update_session_activity("existing-session", awaiting="approval")
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
    row = registry.get_session("existing-session")
    assert (
        row.status,
        row.awaiting,
        row.retention_policy,
        row.lifecycle_generation,
    ) == ("running", "approval", "auto", 1)


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


def test_freshness_migration_existing_store_and_index_plan(pg_control_path):
    """History growth must not turn the last-success lookup into a full scan."""
    import json

    from drover.server.control_store import postgres_control_store
    from drover.server.postgres_schema import bootstrap_postgres_control_store

    store = postgres_control_store(pg_control_path)
    indexes = (
        "lake_export_batches_last_success",
        "control_outbox_batches_unacknowledged",
        "control_outbox_events_pending_age",
    )
    with store.connection() as con:
        con.execute("DELETE FROM control_schema_migrations WHERE version = 16")
        for name in indexes:
            con.execute(f"DROP INDEX IF EXISTS {name}")
        con.execute(
            "INSERT INTO control_outbox_batches (batch_id, state, member_count, acknowledged_at) SELECT 'history-' || n, 'acknowledged', 1, now() - n * interval '1 second' FROM generate_series(1, 10000) n"
        )
        con.execute(
            "INSERT INTO lake_export_batches (batch_id, catalog_id, input_json, input_sha256, acknowledged_at) SELECT batch_id, 'test', '{}', 'test', acknowledged_at FROM control_outbox_batches"
        )
        con.execute(
            "INSERT INTO control_outbox_events (event_id, state, acknowledged_at) SELECT 'history-' || n, 'acknowledged', now() FROM generate_series(1, 10000) n"
        )
    bootstrap_postgres_control_store(store)
    bootstrap_postgres_control_store(store)
    with store.connection() as con:
        con.execute("ANALYZE lake_export_batches")
        con.execute("ANALYZE control_outbox_batches")
        con.execute("ANALYZE control_outbox_events")
        from drover.server.lake.freshness import FRESHNESS_SQL

        full_plan = con.execute("EXPLAIN (FORMAT JSON) " + FRESHNESS_SQL).fetchone()[0]
        encoded_plan = json.dumps(full_plan)
        assert "Seq Scan" not in encoded_plan, full_plan
        for index in indexes:
            assert index in encoded_plan, full_plan
        for query, index in [
            ("SELECT max(acknowledged_at) FROM lake_export_batches", indexes[0]),
            (
                "SELECT count(*), min(created_at) FROM control_outbox_batches WHERE acknowledged_at IS NULL",
                indexes[1],
            ),
            (
                "SELECT min(committed_at) FROM control_outbox_events WHERE state='pending'",
                indexes[2],
            ),
        ]:
            plan = con.execute("EXPLAIN (FORMAT JSON) " + query).fetchone()[0]
            assert index in json.dumps(plan), plan
        (applied,) = con.execute(
            "SELECT count(*) FROM control_schema_migrations WHERE version = 16"
        ).fetchone()
        assert applied == 1
