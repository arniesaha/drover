from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import _duckdb
import duckdb
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from drover.server.lake.fence import MutationFence, drained_mutation, reader_fence
from drover.server.lake.query_process import QueryLimits, query, run_disposable
from drover.server.lake.runtime import (
    LakeError,
    LakeSpec,
    configure_catalog,
    create_table,
    lake_connection,
    verify_runtime,
)


@pytest.fixture
def lake_spec(postgres_dsn, tmp_path, monkeypatch):
    directory = os.environ.get("DROVER_TEST_LAKE_EXTENSIONS")
    if not directory:
        pytest.skip(
            "DROVER_TEST_LAKE_EXTENSIONS must contain pinned extension artifacts"
        )
    name = "drover_lake_test_" + uuid4().hex
    with psycopg.connect(postgres_dsn, autocommit=True) as con:
        con.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(name)))
    monkeypatch.setenv(
        "DROVER_TEST_LAKE_CATALOG", make_conninfo(postgres_dsn, dbname=name)
    )
    spec = LakeSpec(
        "DROVER_TEST_LAKE_CATALOG",
        tmp_path / "lake",
        Path(directory),
        hashlib.sha256(Path(_duckdb.__file__).read_bytes()).hexdigest(),
    )
    spec.data_root.mkdir()
    try:
        yield spec
    finally:
        with psycopg.connect(postgres_dsn, autocommit=True) as con:
            con.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def test_runtime_hash_mismatch_refuses_before_attach(tmp_path):
    spec = LakeSpec("NO_SECRET", tmp_path, tmp_path, "bad")
    with pytest.raises(LakeError, match="engine_hash_mismatch"):
        verify_runtime(spec)


def test_connection_and_disposable_read(lake_spec):
    with lake_connection(lake_spec, read_only=False, create=True) as con:
        configure_catalog(con)
        create_table(
            con,
            "agent_events",
            {"dedup_key": "VARCHAR", "date": "VARCHAR", "content": "VARCHAR"},
            day_partition=True,
        )
        con.execute("INSERT INTO lake.agent_events VALUES ('a', '2026-10-01', 'hello')")
        options = con.execute("FROM lake.options()").fetchall()
        assert any("zstd" in str(row) for row in options)
        assert con.execute(
            "SELECT current_setting('ducklake_default_data_inlining_row_limit')"
        ).fetchone() == (0,)
        versions = con.execute(
            "SELECT extension_name, extension_version FROM duckdb_extensions() WHERE loaded"
        ).fetchall()
        print("extension versions", versions)
    result = query(
        lake_spec,
        "SELECT content FROM lake.agent_events WHERE date = ?",
        ["2026-10-01"],
    )
    assert result["rows"] == [["hello"]]
    assert result["peak_rss_bytes"] < 2 * 1024**3
    # A file outside the catalog cannot become serving data.
    duckdb.connect().execute(
        "COPY (SELECT 'orphan' AS content) TO ? (FORMAT PARQUET)",
        [str(lake_spec.data_root / "orphan.parquet")],
    )
    assert query(lake_spec, "SELECT count(*) FROM lake.agent_events")["rows"] == [[1]]
    with pytest.raises(LakeError, match="read_query_required"):
        query(lake_spec, "DELETE FROM lake.agent_events")


def test_extension_tampering_refuses_start(lake_spec, tmp_path):
    import shutil

    copied = tmp_path / "extensions"
    shutil.copytree(lake_spec.extension_dir, copied)
    with (copied / "ducklake.duckdb_extension").open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(LakeError, match="extension_hash_mismatch"):
        verify_runtime(replace(lake_spec, extension_dir=copied))


def test_two_exporters_and_lost_connection_are_fenced(lake_spec):
    with MutationFence(lake_spec.dsn()) as first:
        first.check()
        with pytest.raises(LakeError, match="mutation_fenced"):
            with MutationFence(lake_spec.dsn()):
                pytest.fail("second exporter entered")
        first.connection.close()
        with pytest.raises(LakeError, match="fence_lost"):
            first.check()
    with MutationFence(lake_spec.dsn()) as replacement:
        replacement.check()


def test_maintenance_refuses_open_reader(lake_spec):
    with reader_fence(lake_spec.dsn()):
        with MutationFence(lake_spec.dsn()) as fence:
            fence.connection.execute("SET statement_timeout = '50ms'")
            from drover.server.lake.fence import READER_LOCK

            with pytest.raises(psycopg.errors.QueryCanceled):
                fence.connection.execute("SELECT pg_advisory_lock(%s)", [READER_LOCK])
    with drained_mutation(lake_spec.dsn()) as fence:
        fence.check()


@pytest.mark.parametrize(
    "script,limits,error",
    [
        (
            "import time; time.sleep(10)",
            QueryLimits(deadline_seconds=0.2),
            "deadline_exceeded",
        ),
        (
            "import time; a=bytearray(80*1024**2); time.sleep(10)",
            QueryLimits(rss_bytes=40 * 1024**2),
            "rss_limit_exceeded",
        ),
        ("print('x'*10000)", QueryLimits(bytes=500), "byte_limit_exceeded"),
        ("print('{\"rows\": [[1], [2]]}')", QueryLimits(rows=1), "row_limit_exceeded"),
    ],
)
def test_process_limits_kill_and_reap(script, limits, error, tmp_path):
    with pytest.raises(LakeError, match=error):
        run_disposable(
            [sys.executable, "-c", script],
            admission_path=tmp_path / "admission",
            limits=limits,
            cwd=tmp_path,
        )
    # The same admission slot is reusable after every failure.
    assert (
        run_disposable(
            [sys.executable, "-c", "print('{\"rows\": []}')"],
            admission_path=tmp_path / "admission",
            limits=QueryLimits(),
            cwd=tmp_path,
        )["rows"]
        == []
    )


def test_query_limit_configuration_cannot_weaken_release_caps():
    for kwargs in (
        {"rss_bytes": 3 * 1024**3},
        {"deadline_seconds": 6},
        {"deadline_seconds": float("nan")},
        {"rows": 10001},
        {"bytes": 5 * 1024**2},
    ):
        with pytest.raises(ValueError):
            QueryLimits(**kwargs)
