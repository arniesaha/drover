"""Least-privilege group roles in an already initialized, separate catalog DB."""

import re

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from .runtime import LakeError, LakeSpec


def provision_catalog_roles(spec: LakeSpec, *, prefix: str) -> dict:
    """Explicit admin operation; no passwords, login users or control DB grants.

    NOLOGIN roles are granted to independently managed login credentials. The
    exporter gets catalog DML and sequence usage, never schema/table ownership.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,49}", prefix):
        raise ValueError("invalid catalog role prefix")
    names = {kind: f"{prefix}_{kind}" for kind in ("reader", "exporter", "admin")}
    with psycopg.connect(spec.dsn()) as con:
        schemas = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT schemaname FROM pg_tables WHERE tablename LIKE 'ducklake_%%' AND schemaname NOT IN ('pg_catalog','information_schema')"
            ).fetchall()
        ]
        if len(schemas) != 1:
            raise LakeError("lake_initialized_catalog_required")
        schema = schemas[0]
        database = conninfo_to_dict(spec.dsn())["dbname"]
        for role in names.values():
            con.execute(
                sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(role))
            )
        con.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(database)
            )
        )
        for role in names.values():
            con.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
        con.execute(
            sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                sql.Identifier(database), sql.Identifier(names["admin"])
            )
        )
        con.execute(
            sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(
                sql.Identifier(schema)
            )
        )
        con.execute(
            sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(
                sql.Identifier(schema), sql.Identifier(names["admin"])
            )
        )
        for kind in ("reader", "exporter"):
            con.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(names[kind])
                )
            )
        tables = con.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=%s", [schema]
        ).fetchall()
        for (table,) in tables:
            con.execute(
                sql.SQL("ALTER TABLE {}.{} OWNER TO {}").format(
                    sql.Identifier(schema),
                    sql.Identifier(table),
                    sql.Identifier(names["admin"]),
                )
            )
        for (sequence,) in con.execute(
            "SELECT sequencename FROM pg_sequences WHERE schemaname=%s", [schema]
        ).fetchall():
            con.execute(
                sql.SQL("ALTER SEQUENCE {}.{} OWNER TO {}").format(
                    sql.Identifier(schema),
                    sql.Identifier(sequence),
                    sql.Identifier(names["admin"]),
                )
            )
        con.execute(
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(names["reader"])
            )
        )
        con.execute(
            sql.SQL(
                "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {} TO {}"
            ).format(sql.Identifier(schema), sql.Identifier(names["exporter"]))
        )
        con.execute(
            sql.SQL("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(names["exporter"])
            )
        )
        for kind, privileges in (
            ("reader", "SELECT"),
            ("exporter", "SELECT,INSERT,UPDATE,DELETE"),
        ):
            con.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT "
                    + privileges
                    + " ON TABLES TO {}"
                ).format(
                    sql.Identifier(names["admin"]),
                    sql.Identifier(schema),
                    sql.Identifier(names[kind]),
                )
            )
        con.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT USAGE,SELECT ON SEQUENCES TO {}"
            ).format(
                sql.Identifier(names["admin"]),
                sql.Identifier(schema),
                sql.Identifier(names["exporter"]),
            )
        )
    return {"database": database, "schema": schema, "roles": names}
