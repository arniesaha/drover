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


def test_query_child_engine_budget_leaves_half_the_rss_cap():
    """The A3 fix: the child's engine budget sits at most at half the kill cap."""
    from drover.server.lake.cutover import memory_limit_bytes
    from drover.server.lake.runtime import QUERY_CHILD_SETTINGS

    cap = QueryLimits().rss_bytes
    assert memory_limit_bytes(QUERY_CHILD_SETTINGS["memory_limit"]) <= cap // 2
    assert QUERY_CHILD_SETTINGS["threads"] == 1
    assert QUERY_CHILD_SETTINGS["preserve_insertion_order"] is False


def test_query_child_runs_with_bounded_settings_and_private_spill(lake_spec):
    with lake_connection(lake_spec, read_only=False, create=True) as con:
        configure_catalog(con)
    result = query(
        lake_spec,
        "SELECT current_setting('threads'), current_setting('memory_limit'),"
        " current_setting('preserve_insertion_order'),"
        " current_setting('temp_directory')",
    )
    threads, memory_limit, preserve, spill = result["rows"][0]
    assert threads == 1 and preserve is False
    assert memory_limit.startswith("732")  # 768MB, reported in MiB
    assert Path(spill).name == "spill"
    assert Path(spill).parent.name.startswith("drover-lake-query-")


def _fixture_tar(tmp_path):
    import tarfile
    from datetime import datetime, timezone

    import pyarrow as pa
    import pyarrow.parquet as pq

    source = tmp_path / "source"
    # Exact ties use row payload hash/file lineage; all null keys are retained.
    rows = [
        {
            "id": "old",
            "session_id": "s",
            "timestamp": "2026-10-01T01:00:00Z",
            "dedup_key": "a",
            "repo_owner": None,
            "repo_name": None,
            "content": "old",
        },
        {
            "id": "winner",
            "session_id": "s",
            "timestamp": "2026-10-01T00:00:00Z",
            "dedup_key": "a",
            "repo_owner": "o",
            "repo_name": "r",
            "content": "attributed",
        },
        {
            "id": "n",
            "session_id": "s",
            "timestamp": "2026-10-01T02:00:00Z",
            "dedup_key": None,
            "repo_owner": None,
            "repo_name": None,
            "content": "null",
        },
        {
            "id": "n",
            "session_id": "s",
            "timestamp": "2026-10-01T02:00:00Z",
            "dedup_key": None,
            "repo_owner": None,
            "repo_name": None,
            "content": "null",
        },
    ]
    path = source / "parquet/agent_events/date=2026-10-01/agent_id=test/part.parquet"
    path.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows), path)
    # A mixed-era typed timestamp must bind in the same unified scan.
    path = path.with_name("typed.parquet")
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "id": "typed",
                    "session_id": "s",
                    "timestamp": datetime(2026, 10, 1, tzinfo=timezone.utc),
                    "dedup_key": "b",
                }
            ]
        ),
        path,
    )
    for table, row in (
        (
            "provider_usage_snapshots",
            {
                "snapshot_id": "p",
                "observed_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
            },
        ),
        (
            "control_outbox_batches",
            {"event_id": "e", "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc)},
        ),
    ):
        path = source / "parquet" / table / "part.parquet"
        path.parent.mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist([row]), path)
    # Never import spans or AppleDouble resource forks.
    (source / "parquet/spans").mkdir()
    (source / "parquet/spans/part.parquet").write_bytes(b"not parquet")
    (source / "parquet/agent_events/._part.parquet").write_bytes(b"not parquet")
    archive = tmp_path / "frozen.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(source / "parquet", arcname="parquet")
    return archive


def test_rebuild_accounting_and_fresh_catalog_roundtrip(lake_spec, tmp_path):
    from drover.server.lake.rebuild import rebuild, verify

    spec = replace(lake_spec, data_root=tmp_path / "rebuilt")
    report = rebuild(_fixture_tar(tmp_path), spec)
    assert report["raw"]["agent_events"]["rows"] == 5
    assert report["canonical_agent_events"]["rows"] == 3
    assert report["null_key_rows_retained"] == 0
    assert report["losers"] == 2
    assert report["original_key_losers"] == 1
    assert report["baseline_canonical_agent_events"]["rows"] == 4
    assert report["buckets"]["rebuild_backfill"] == 2
    actual = verify(spec)
    assert actual["agent_events"] == report["canonical_agent_events"]
    assert (
        actual["provider_usage_snapshots"] == report["raw"]["provider_usage_snapshots"]
    )
    assert query(spec, "SELECT content FROM lake.agent_events WHERE dedup_key='a'")[
        "rows"
    ] == [["attributed"]]
    with lake_connection(spec) as con:
        assert (
            con.execute(
                "SELECT count(DISTINCT (_import_file, _import_row)) FROM lake.agent_events WHERE dedup_key_source = 'rebuild_backfill'"
            ).fetchone()[0]
            == 1
        )
    import pyarrow.parquet as pq

    mapping = pq.read_table(
        spec.data_root / "partitions/2026-10-01/losers.parquet"
    ).to_pylist()
    assert len(mapping) == 2
    assert any(row["dedup_key"] == "a" for row in mapping)
    with pytest.raises(LakeError, match="requires_new_data_root"):
        rebuild(tmp_path / "frozen.tar", spec)


def test_rebuild_dry_run_never_contacts_postgres(lake_spec, tmp_path, monkeypatch):
    from drover.server.lake.rebuild import rebuild

    monkeypatch.delenv(lake_spec.catalog_dsn_env)
    report = rebuild(
        _fixture_tar(tmp_path),
        replace(lake_spec, data_root=tmp_path / "dry"),
        dry_run=True,
    )
    assert report["dry_run"]
    assert report["canonical_agent_events"]["rows"] == 3


def test_rebuild_rejects_unsafe_tar(tmp_path):
    import io
    import tarfile

    from drover.server.lake.rebuild import extract_frozen

    path = tmp_path / "unsafe.tar"
    with tarfile.open(path, "w") as tar:
        member = tarfile.TarInfo("../agent_events/evil.parquet")
        member.size = 1
        tar.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(LakeError, match="unsafe_tar_path"):
        extract_frozen(path, tmp_path / "output")


def test_rebuild_refuses_nonempty_catalog(lake_spec, tmp_path):
    from drover.server.lake.rebuild import rebuild

    with psycopg.connect(lake_spec.dsn(), autocommit=True) as con:
        con.execute("CREATE TABLE must_preserve (id INT)")
    with pytest.raises(LakeError, match="requires_fresh_catalog"):
        rebuild(
            _fixture_tar(tmp_path), replace(lake_spec, data_root=tmp_path / "fresh")
        )
    with psycopg.connect(lake_spec.dsn(), autocommit=True) as con:
        assert con.execute("SELECT count(*) FROM must_preserve").fetchone() == (0,)


def test_verify_recomputes_payload_hashes(lake_spec, tmp_path):
    from drover.server.lake.rebuild import rebuild, verify

    spec = replace(lake_spec, data_root=tmp_path / "verify-changed")
    rebuild(_fixture_tar(tmp_path), spec)
    with lake_connection(spec, read_only=False) as con:
        # Keeping the recorded row digest must not conceal altered contents.
        con.execute(
            "UPDATE lake.agent_events SET content='changed' WHERE dedup_key='a'"
        )
    with pytest.raises(LakeError, match="verification_mismatch"):
        verify(spec)


@pytest.mark.skipif(
    not os.environ.get("DROVER_PHASE4_REHEARSAL_TAR"),
    reason="explicit local backup rehearsal only",
)
def test_local_backup_rehearsal(lake_spec):
    import json

    from drover.server.lake.rebuild import rebuild, verify

    if os.environ.get("DROVER_TEST_POSTGRES_DSN"):
        pytest.fail("large rehearsal requires an initdb-created private cluster")
    root = Path(os.environ["DROVER_PHASE4_REHEARSAL_ROOT"]).resolve()
    assert root.is_relative_to(Path("/tmp").resolve())
    spec = replace(lake_spec, data_root=root)
    report = rebuild(Path(os.environ["DROVER_PHASE4_REHEARSAL_TAR"]), spec)
    assert verify(spec)["agent_events"] == report["canonical_agent_events"]
    print(json.dumps(report, indent=2))


def test_rebuild_cli_does_not_resolve_live_config(lake_spec, tmp_path, monkeypatch):
    import json

    from click.testing import CliRunner

    from drover.server import __main__ as cli

    def forbidden(*args, **kwargs):
        pytest.fail("offline lake tooling must never load the live hub config")

    monkeypatch.setattr(cli, "_resolve_config", forbidden)
    monkeypatch.delenv(lake_spec.catalog_dsn_env)
    monkeypatch.setenv("DROVER_LAKE_EXTENSION_DIR", str(lake_spec.extension_dir))
    monkeypatch.setenv("DROVER_LAKE_ENGINE_SHA256", lake_spec.engine_sha256)
    monkeypatch.setenv("DROVER_LAKE_REBUILD_RSS_CEILING_BYTES", str(3 * 1024**3))
    result = CliRunner().invoke(
        cli.main,
        [
            "--config",
            str(tmp_path / "does-not-exist.toml"),
            "lake",
            "rebuild",
            "--from-tar",
            str(_fixture_tar(tmp_path)),
            "--data-root",
            str(tmp_path / "cli-dry"),
            "--catalog-dsn-env",
            "UNSET_SCRATCH_DSN",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["dry_run"]
    assert report["rss_ceiling_bytes"] == 3 * 1024**3


def test_rebuild_slices_keep_one_global_timestamp_binding(tmp_path):
    from datetime import datetime, timezone

    import pyarrow as pa
    import pyarrow.parquet as pq

    from drover.server.lake.rebuild import stage_relation

    extracted = tmp_path / "input"
    inventory = []
    for index in range(21):
        path = (
            extracted
            / f"parquet/agent_events/date=2026-10-01/agent_id=test/{index:02}.parquet"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = (
            datetime(2026, 10, 1, tzinfo=timezone.utc)
            if index == 20
            else "2026-10-01T00:00:00Z"
        )
        pq.write_table(
            pa.Table.from_pylist([{"id": f"{index:02}", "timestamp": timestamp}]), path
        )
        inventory.append({"path": str(path.relative_to(extracted))})
    with duckdb.connect() as con:
        stage_relation(con, "agent_events", inventory, extracted)
        expected = con.execute(
            "SELECT CAST(timestamp AS VARCHAR) FROM input_agent_events ORDER BY id"
        ).fetchall()
        actual = con.execute(
            "SELECT timestamp FROM raw_agent_events ORDER BY id"
        ).fetchall()
        assert actual == expected


def test_admission_wait_has_same_deadline_and_never_starts_second_child(tmp_path):
    import fcntl

    path = tmp_path / "admission"
    sentinel = tmp_path / "started"
    with path.open("a+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(LakeError, match="admission_deadline"):
            run_disposable(
                [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(sentinel)!r}).touch()",
                ],
                admission_path=path,
                limits=QueryLimits(deadline_seconds=0.1),
                cwd=tmp_path,
            )
    assert not sentinel.exists()


def test_decided_null_key_buckets(lake_spec, tmp_path):
    import tarfile

    import pyarrow as pa
    import pyarrow.parquet as pq

    from drover.dedup import make_dedup_key
    from drover.server.lake.rebuild import rebuild, verify

    original = _fixture_tar(tmp_path)
    path = (
        tmp_path
        / "source/parquet/agent_events/date=2026-10-01/agent_id=test/policy.parquet"
    )
    rows = [
        {
            "id": "metadata",
            "session_id": "s",
            "timestamp": "2026-10-01T03:00:00Z",
            "dedup_key": None,
            "content": "",
            "role": None,
        },
        {
            "id": "empty-role",
            "session_id": "s",
            "timestamp": "2026-10-01T03:00:00Z",
            "dedup_key": None,
            "content": "",
            "role": "tool",
        },
        {
            "id": "unicode",
            "session_id": "s",
            "timestamp": "2026-10-01T03:00:00Z",
            "dedup_key": None,
            "content": "🙂" * 205,
            "role": "assistant",
        },
        {
            "id": "keyed-status",
            "session_id": "s",
            "timestamp": "2026-10-01T03:00:00Z",
            "dedup_key": "status",
            "content": "",
            "role": None,
        },
    ]
    pq.write_table(pa.Table.from_pylist(rows), path)
    original.unlink()
    with tarfile.open(original, "w") as tar:
        tar.add(tmp_path / "source/parquet", arcname="parquet")
    spec = replace(lake_spec, data_root=tmp_path / "policy")
    report = rebuild(original, spec)
    assert report["buckets"] == {
        "original": 4,
        "rebuild_backfill": 3,
        "legacy_metadata": 1,
        "legacy_null": 1,
    }
    assert report["raw"]["agent_events"]["rows"] == 9
    assert report["canonical_agent_events"]["rows"] == 6
    assert report["legacy_metadata"]["rows"] == 1
    assert report["losers"] == 2
    assert (
        9
        == report["canonical_agent_events"]["rows"]
        + report["legacy_metadata"]["rows"]
        + report["losers"]
    )
    import duckdb

    with duckdb.connect() as con:
        counts = con.execute(
            "SELECT sum(raw_rows),sum(serving_rows),sum(archive_rows),sum(loser_rows) FROM read_parquet(?)",
            [str(spec.data_root / "partitions/2026-10-01/session-counts.parquet")],
        ).fetchone()
    assert counts == (9, 6, 1, 2)
    expected = make_dedup_key("2026-10-01T03:00:00Z", "test", "s", None, "🙂" * 205)
    assert query(
        spec,
        "SELECT dedup_key,dedup_key_source FROM lake.agent_events WHERE id='unicode'",
    )["rows"] == [[expected, "rebuild_backfill"]]
    assert query(spec, "SELECT id FROM lake.agent_events_legacy_metadata")["rows"] == [
        ["metadata"]
    ]
    assert query(spec, "SELECT count(*) FROM lake.agent_events WHERE id='metadata'")[
        "rows"
    ] == [[0]]
    assert verify(spec)["agent_events_legacy_metadata"]["rows"] == 1


def test_cross_partition_keys_are_checked_before_publication(lake_spec, tmp_path):
    import tarfile

    import pyarrow as pa
    import pyarrow.parquet as pq

    from drover.server.lake.rebuild import rebuild

    archive = _fixture_tar(tmp_path)
    path = (
        tmp_path
        / "source/parquet/agent_events/date=2026-10-02/agent_id=test/other.parquet"
    )
    path.parent.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [{"id": "bad-day", "timestamp": "2026-10-01T01:00:00Z", "dedup_key": "a"}]
        ),
        path,
    )
    archive.unlink()
    with tarfile.open(archive, "w") as tar:
        tar.add(tmp_path / "source/parquet", arcname="parquet")
    with pytest.raises(LakeError, match="cross_partition_dedup_key"):
        rebuild(archive, replace(lake_spec, data_root=tmp_path / "cross"))
    with psycopg.connect(lake_spec.dsn(), autocommit=True) as con:
        assert (
            con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')"
            ).fetchone()[0]
            == 0
        )


def test_sanitize_detail_is_bounded_and_credential_free():
    from drover.server.lake.runtime import sanitize_detail

    dsn = "host=db.internal user=svc password=s3cr3t-pw dbname=lake"
    exc = RuntimeError(
        "connection to server failed: host=db.internal password=s3cr3t-pw "
        "postgresql://svc:s3cr3t-pw@db.internal/lake\nsecond line " + "x" * 500
    )
    detail = sanitize_detail(exc, dsn)
    assert detail.startswith("RuntimeError: connection to server failed")
    assert "s3cr3t-pw" not in detail
    assert "db.internal" not in detail
    assert "second line" not in detail
    assert len(sanitize_detail(RuntimeError("y" * 1000))) == 300


def test_attach_failure_keeps_code_and_carries_clean_detail(monkeypatch, tmp_path):
    from drover.server.lake import runtime

    monkeypatch.setattr(runtime, "verify_runtime", lambda spec: {})
    monkeypatch.setenv("SCRATCH_DSN", "host=h user=u password=hunter22 dbname=lake")

    class Con:
        def execute(self, statement):
            if statement.startswith("ATTACH"):
                raise RuntimeError(
                    "permission denied for schema public (password=hunter22)"
                )

    spec = LakeSpec("SCRATCH_DSN", tmp_path, tmp_path, "0")
    with pytest.raises(LakeError) as caught:
        runtime.attach_lake(Con(), spec, read_only=False)
    assert caught.value.code == "analytics_unavailable"
    assert str(caught.value) == "analytics_unavailable"
    assert "permission denied for schema public" in caught.value.detail
    assert "hunter22" not in caught.value.detail


def test_cli_prints_detail_and_writes_admin_error_log(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from drover.server.lake import cli as lake_cli
    from drover.server.lake.runtime import LakeError as Err

    root = tmp_path / "root"
    root.mkdir()

    def boom(spec):
        raise Err("analytics_unavailable", "Error: permission denied for schema public")

    monkeypatch.setattr(lake_cli, "verify", boom)
    monkeypatch.setenv("DROVER_LAKE_EXTENSION_DIR", str(tmp_path))
    monkeypatch.setenv("DROVER_LAKE_ENGINE_SHA256", "0")
    result = CliRunner().invoke(
        lake_cli.lake_cmd,
        ["verify", "--data-root", str(root), "--catalog-dsn-env", "X"],
    )
    assert result.exit_code != 0
    assert "analytics_unavailable: Error: permission denied" in result.output
    assert (
        "permission denied for schema public" in (root / "admin-error.log").read_text()
    )


@pytest.fixture
def scratch_catalog(postgres_dsn):
    """A throwaway database in the private test server plus a CREATE-less role."""
    name = "drover_preflight_" + uuid4().hex[:12]
    role = "drover_nocreate_" + uuid4().hex[:12]
    with psycopg.connect(postgres_dsn, autocommit=True) as con:
        con.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        con.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'pw-nocreate'").format(
                sql.Identifier(role)
            )
        )
    with psycopg.connect(
        make_conninfo(postgres_dsn, dbname=name), autocommit=True
    ) as con:
        con.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    try:
        yield make_conninfo(postgres_dsn, dbname=name), role
    finally:
        with psycopg.connect(postgres_dsn, autocommit=True) as con:
            con.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
            con.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_catalog_preflight_denies_missing_create_and_allows_owner(
    scratch_catalog, tmp_path, monkeypatch
):
    from drover.server.lake.runtime import catalog_permission_preflight

    dsn, role = scratch_catalog
    spec = LakeSpec("SCRATCH_CATALOG", tmp_path / "root", tmp_path, "0")
    monkeypatch.setenv("SCRATCH_CATALOG", dsn)
    catalog_permission_preflight(spec)
    monkeypatch.setenv(
        "SCRATCH_CATALOG", make_conninfo(dsn, user=role, password="pw-nocreate")
    )
    with pytest.raises(LakeError) as caught:
        catalog_permission_preflight(spec)
    assert caught.value.code == "lake_catalog_permission_denied"
    assert "CREATE" in caught.value.detail
    assert "pw-nocreate" not in caught.value.detail


def test_rebuild_runs_preflight_before_any_partition_work(
    scratch_catalog, tmp_path, monkeypatch
):
    from drover.server.lake import partition_rebuild

    dsn, role = scratch_catalog
    monkeypatch.setenv(
        "SCRATCH_CATALOG", make_conninfo(dsn, user=role, password="pw-nocreate")
    )
    monkeypatch.setattr(partition_rebuild, "verify_runtime", lambda spec: {})
    monkeypatch.setattr(
        partition_rebuild,
        "extract_frozen",
        lambda *a, **k: pytest.fail("preflight must run before extraction"),
    )
    spec = LakeSpec("SCRATCH_CATALOG", tmp_path / "pre", tmp_path, "0")
    with pytest.raises(LakeError) as caught:
        partition_rebuild.rebuild_partitioned(tmp_path / "unused.tar", spec)
    assert caught.value.code == "lake_catalog_permission_denied"
    assert not spec.data_root.exists()


def test_preflight_skipped_for_dry_run(lake_spec, tmp_path, monkeypatch):
    from drover.server.lake import partition_rebuild
    from drover.server.lake.rebuild import rebuild

    monkeypatch.setattr(
        partition_rebuild,
        "catalog_permission_preflight",
        lambda spec: pytest.fail("dry run must not contact PostgreSQL"),
    )
    monkeypatch.delenv(lake_spec.catalog_dsn_env)
    report = rebuild(
        _fixture_tar(tmp_path),
        replace(lake_spec, data_root=tmp_path / "dry"),
        dry_run=True,
    )
    assert report["dry_run"]
