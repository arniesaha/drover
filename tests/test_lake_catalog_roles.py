"""Real PostgreSQL privileges for a disposable initialized catalog."""

import os
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_lake_runtime import lake_spec  # noqa: F401 - shared pytest fixture

from drover.server.lake.runtime import configure_catalog, create_table, lake_connection


def test_catalog_roles_allow_read_and_export_but_no_reader_mutation(
    lake_spec, monkeypatch
):
    from drover.server.lake.catalog_roles import provision_catalog_roles

    with lake_connection(lake_spec, read_only=False, create=True) as con:
        configure_catalog(con)
        create_table(
            con,
            "agent_events",
            {"date": "VARCHAR", "dedup_key": "VARCHAR"},
            day_partition=True,
        )
        con.execute("INSERT INTO lake.agent_events VALUES ('2026-10-01','a')")
    provisioned = provision_catalog_roles(lake_spec, prefix="lake_" + uuid4().hex)
    roles = provisioned["roles"]
    try:
        for kind in ("reader", "exporter"):
            env = "DROVER_TEST_LAKE_" + kind.upper()
            monkeypatch.setenv(
                env, make_conninfo(lake_spec.dsn(), options="-c role=" + roles[kind])
            )
            spec = replace(lake_spec, catalog_dsn_env=env)
            with lake_connection(spec, read_only=kind == "reader") as con:
                assert (
                    con.execute("SELECT count(*) FROM lake.agent_events").fetchone()[0]
                    >= 1
                )
                if kind == "exporter":
                    con.execute(
                        "INSERT INTO lake.agent_events VALUES ('2026-10-01','b')"
                    )
            with psycopg.connect(os.environ[env], autocommit=True) as con:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    con.execute("CREATE TABLE forbidden (id INT)")
                if kind == "reader":
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        con.execute(
                            sql.SQL("DELETE FROM {}.ducklake_metadata").format(
                                sql.Identifier(provisioned["schema"])
                            )
                        )
        with lake_connection(lake_spec) as con:
            assert (
                con.execute("SELECT count(*) FROM lake.agent_events").fetchone()[0] == 2
            )
    finally:
        with psycopg.connect(lake_spec.dsn(), autocommit=True) as con:
            for role in roles.values():
                con.execute(
                    sql.SQL("DROP OWNED BY {} CASCADE").format(sql.Identifier(role))
                )
            for role in roles.values():
                con.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
