"""Catalog-side fencing at DuckLake's atomic snapshot commit boundary."""

from uuid import uuid4

import psycopg
from psycopg import sql

from .fence import MUTATION_LOCK, MutationFence
from .runtime import LakeError, LakeSpec

GUARD_SCHEMA = "drover_lake_export"
APPLICATION_PREFIX = "drover-export:"


def install_commit_guard(spec: LakeSpec, *, exporter_role: str | None = None):
    """Explicit catalog-admin provisioning, never exporter startup DDL.

    A snapshot trigger holds a shared row lock until catalog COMMIT. Replacement
    ownership updates that row exclusively, draining any already-validated old
    commit before issuing a new token. A stale engine cannot publish afterwards.
    """
    with MutationFence(spec.dsn()), psycopg.connect(spec.dsn(), autocommit=True) as con:
        schemas = con.execute(
            "SELECT schemaname FROM pg_tables WHERE tablename='ducklake_snapshot'"
        ).fetchall()
        if len(schemas) != 1:
            raise LakeError("lake_snapshot_catalog_missing")
        metadata_schema = schemas[0][0]
        require_token = ""
        if exporter_role:
            role_literal = sql.Literal(exporter_role).as_string(con)
            admin_literal = sql.Literal(
                con.execute("SELECT current_user").fetchone()[0]
            ).as_string(con)
            require_token = f"""IF app NOT LIKE '{APPLICATION_PREFIX}%' AND
                (pg_catalog.current_setting('role')={role_literal} OR
                 (pg_catalog.pg_has_role(session_user,{role_literal},'MEMBER') AND
                  NOT pg_catalog.pg_has_role(session_user,{admin_literal},'MEMBER'))) THEN
                  RAISE EXCEPTION 'lake exporter token required';
                END IF;"""
        with con.transaction():
            con.execute(
                sql.SQL("CREATE SCHEMA {} ").format(sql.Identifier(GUARD_SCHEMA))
            )
            con.execute(
                sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(
                    sql.Identifier(GUARD_SCHEMA)
                )
            )
            con.execute(f"""CREATE TABLE {GUARD_SCHEMA}.ownership (
                singleton boolean PRIMARY KEY CHECK (singleton), catalog_id text NOT NULL,
                token text, backend_pid integer)""")
            con.execute(
                f"INSERT INTO {GUARD_SCHEMA}.ownership VALUES (true,%s,NULL,NULL)",
                [uuid4().hex],
            )
            # SQL identifiers are fixed, and SECURITY DEFINER has no ambient search path.
            con.execute(
                f"""CREATE FUNCTION {GUARD_SCHEMA}.activate(new_token text) RETURNS text
                LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
                DECLARE identity text;
                BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_locks
                     WHERE locktype='advisory' AND pid=pg_catalog.pg_backend_pid()
                     AND database=(SELECT oid FROM pg_catalog.pg_database WHERE datname=pg_catalog.current_database())
                     AND classid={MUTATION_LOCK >> 32} AND objid={MUTATION_LOCK & 0xffffffff}
                     AND objsubid=1 AND mode='ExclusiveLock' AND granted) THEN
                    RAISE EXCEPTION 'lake mutation fence required';
                  END IF;
                  UPDATE {GUARD_SCHEMA}.ownership SET token=new_token,backend_pid=pg_catalog.pg_backend_pid()
                    WHERE singleton RETURNING catalog_id INTO identity;
                  RETURN identity;
                END $$"""
            )
            con.execute(
                f"""CREATE FUNCTION {GUARD_SCHEMA}.check_snapshot() RETURNS trigger
                LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
                DECLARE owner_token text; owner_pid integer; app text;
                BEGIN
                  app := pg_catalog.current_setting('application_name');
                  {require_token}
                  IF app LIKE '{APPLICATION_PREFIX}%' THEN
                    SELECT token,backend_pid INTO owner_token,owner_pid
                      FROM {GUARD_SCHEMA}.ownership WHERE singleton FOR SHARE;
                    IF app != '{APPLICATION_PREFIX}' || owner_token OR owner_token IS NULL
                       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_locks
                         WHERE locktype='advisory' AND pid=owner_pid
                         AND database=(SELECT oid FROM pg_catalog.pg_database WHERE datname=pg_catalog.current_database())
                         AND classid={MUTATION_LOCK >> 32} AND objid={MUTATION_LOCK & 0xffffffff}
                         AND objsubid=1 AND mode='ExclusiveLock' AND granted) THEN
                      RAISE EXCEPTION 'lake exporter fence lost';
                    END IF;
                  END IF;
                  IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
                END $$"""
            )
            con.execute(
                f"REVOKE ALL ON ALL TABLES IN SCHEMA {GUARD_SCHEMA} FROM PUBLIC"
            )
            con.execute(
                f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {GUARD_SCHEMA} FROM PUBLIC"
            )
            con.execute(
                sql.SQL(
                    "CREATE TRIGGER drover_export_fence BEFORE INSERT OR UPDATE OR DELETE ON {}.ducklake_snapshot FOR EACH ROW EXECUTE FUNCTION {}.check_snapshot()"
                ).format(sql.Identifier(metadata_schema), sql.Identifier(GUARD_SCHEMA))
            )
            if exporter_role:
                con.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        sql.Identifier(GUARD_SCHEMA), sql.Identifier(exporter_role)
                    )
                )
                con.execute(
                    sql.SQL("GRANT EXECUTE ON FUNCTION {}.activate(text) TO {}").format(
                        sql.Identifier(GUARD_SCHEMA), sql.Identifier(exporter_role)
                    )
                )


def activate(fence: MutationFence) -> tuple[str, str]:
    fence.check()
    token = uuid4().hex
    # Ownership transfer waits for any old snapshot transaction to finish.
    fence.connection.execute("SET statement_timeout = '5s'")
    row = fence.connection.execute(
        f"SELECT {GUARD_SCHEMA}.activate(%s)", [token]
    ).fetchone()
    return row[0], token
