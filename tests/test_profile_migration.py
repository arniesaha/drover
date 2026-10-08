from drover.server.control_store import postgres_control_store
from drover.server.postgres_schema import (
    _MIGRATIONS,
    PROFILE_MIGRATION,
    bootstrap_postgres_control_store,
)


def test_profile_migration_fresh_existing_rerun(pg_control_path):
    store = postgres_control_store(pg_control_path)
    with store.connection() as con:
        assert con.execute("SELECT count(*) FROM profile_items").fetchone()[0] == 0
        con.execute(
            "INSERT INTO profile_agents (agent_id, credential_id, tier, updated_by) "
            "VALUES ('example-agent', 'example-credential', 'trusted', 'operator')"
        )
        for sql in dict(_MIGRATIONS)[PROFILE_MIGRATION]:
            con.execute(sql)
    bootstrap_postgres_control_store(store)
    with store.connection() as con:
        assert con.execute("SELECT tier FROM profile_agents").fetchone()[0] == "trusted"
        con.execute("DROP TABLE profile_proposals, profile_items, profile_agents")
        con.execute(
            "DELETE FROM control_schema_migrations WHERE version = ?",
            [PROFILE_MIGRATION],
        )
    bootstrap_postgres_control_store(store)
    bootstrap_postgres_control_store(store)
    with store.connection() as con:
        assert con.execute("SELECT count(*) FROM profile_proposals").fetchone()[0] == 0
        assert (
            con.execute(
                "SELECT count(*) FROM control_schema_migrations WHERE version = ?",
                [PROFILE_MIGRATION],
            ).fetchone()[0]
            == 1
        )
