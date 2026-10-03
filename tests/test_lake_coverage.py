"""Authoritative source revisions and certified generations use private PG/lakes."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from test_control_outbox import postgres_control_store
from test_lake_runtime import lake_spec
from test_lake_serving import verified_lake

from drover.server.lake.serving import configure_analytics


def test_context_generation_parity_and_missing_stale_incomplete(verified_lake):
    from drover.server.db import control_plane_connection, open_duckdb_connection
    from drover.server.lake import coverage
    from drover.server.mcp import tools

    _, path, config = verified_lake
    with open_duckdb_connection(path) as con:
        con.execute(
            "INSERT INTO context_containers(context_id,container_type,label,source_harness,confidence,evidence,last_touched_at,next_action,open_loop,session_ids,task_ids,repo_owner,repo_name,summary_md,created_at,updated_at) VALUES ('ctx','research_thread','Research','codex',0.9,'curated',now(),'Continue','Question',['s'],[],'o','r','Known context',now(),now())"
        )
    expected = tools.drover_context_brief(duckdb_path=path, context_id="ctx")
    configure_analytics(path, config)
    assert (
        tools.drover_context_brief(duckdb_path=path, context_id="ctx")["status"]
        == "unavailable"
    )
    coverage.provision_coverage(path)
    coverage.publish_source(
        path,
        "contexts",
        [expected],
        publisher="curator",
        watermark="ctx-v1",
        observed_at=datetime.now(timezone.utc),
    )
    generation = coverage.certify(path, "contexts")
    actual = tools.drover_context_brief(duckdb_path=path, context_id="ctx")
    assert actual == expected
    assert (
        tools.drover_recent_contexts(
            duckdb_path=path, container_type="personal_project"
        )["contexts"]
        == []
    )
    assert tools.drover_open_loops(duckdb_path=path, project_key="o/r")[
        "open_loops"
    ] == [expected]
    assert (
        tools.drover_resume_context(duckdb_path=path, context_id="ctx")["context"]
        == expected
    )
    configure_analytics(path, replace(config, epoch="changed"))
    assert (
        tools.drover_context_brief(duckdb_path=path, context_id="ctx")["status"]
        == "unavailable"
    )
    coverage.certify(path, "contexts")
    with control_plane_connection(path) as con:
        con.execute(
            "UPDATE lake_coverage_generations SET payload='{}' WHERE generation<>?",
            [generation],
        )
    assert (
        tools.drover_context_brief(duckdb_path=path, context_id="ctx")["status"]
        == "unavailable"
    )
    coverage.publish_source(
        path,
        "contexts",
        [expected],
        publisher="curator",
        watermark="ctx-v2",
        observed_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    coverage.certify(path, "contexts")
    assert tools.drover_recent_contexts(duckdb_path=path)["status"] == "unavailable"


def test_native_inventory_certification_and_usage(verified_lake):
    import duckdb

    from drover.server.db import control_plane_connection
    from drover.server.lake import coverage
    from drover.server.lake.read_models import read_model
    from drover.server.lake.rebuild import EVENT_SCHEMA
    from drover.server.lake.runtime import LakeError

    spec, path, config = verified_lake
    # Producer inventory comes from independently frozen source files, not lake
    # serving data. Fixture winner is explicit; the loser must not be published.
    files = sorted((path.parent / "parquet" / "agent_events").rglob("*.parquet"))
    with duckdb.connect() as con:
        con.execute("SET TimeZone='UTC'")
        from drover.server.lake.runtime import literal

        con.execute(
            "CREATE VIEW incoming AS SELECT * FROM read_parquet(["
            + ",".join(literal(str(file)) for file in files)
            + "],hive_partitioning=true)"
        )
        cols = {r[0] for r in con.execute("DESCRIBE incoming").fetchall()}
        select = ",".join(
            f'CAST("{n}" AS {t}) AS "{n}"' if n in cols else f'NULL::{t} AS "{n}"'
            for n, t in EVENT_SCHEMA.items()
        )
        con.execute(
            "CREATE VIEW producer_rows AS SELECT "
            + select
            + " FROM incoming WHERE id<>'loser'"
        )
        inventory = coverage.native_inventory(con, "producer_rows")
    configure_analytics(path, config)
    coverage.provision_coverage(path)
    coverage.publish_source(
        path,
        "native",
        inventory,
        publisher="native-collector",
        watermark="native-v1",
        observed_at=datetime.now(timezone.utc),
    )
    generation = coverage.certify(path, "native")
    metadata = read_model(
        path, "cockpit", filters={"days": 30}, cursor_secret="00" * 32
    )["metadata"]
    for kind in ("native_publication", "native_usage"):
        assert metadata[kind]["freshness"] == "fresh"
        assert metadata[kind]["generation"] == generation
        assert metadata[kind]["coverage_binding"] == metadata["binding"]
        assert metadata[kind]["publication_scope"] == "canonical_native_agent_events"
        assert metadata[kind]["freshness_basis"] == "registered_source_revision"
    # An authoritative newer inventory with an extra unpublished event cannot
    # be certified, and supersedes the old generation immediately.
    coverage.publish_source(
        path,
        "native",
        {**inventory, "rows": inventory["rows"] + 1},
        publisher="native-collector",
        watermark="native-v2",
        observed_at=datetime.now(timezone.utc),
    )
    with pytest.raises(LakeError, match="native_publication_incomplete"):
        coverage.certify(path, "native")
    assert (
        read_model(path, "cockpit", filters={"days": 30}, cursor_secret="00" * 32)[
            "metadata"
        ]["native_usage"]["freshness"]
        == "unavailable"
    )


@pytest.fixture
def typed_seed(monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    import test_mcp_tools

    original = test_mcp_tools._write_agent_events

    def write(parquet, rows):
        original(parquet, rows)
        for file in (parquet / "agent_events").rglob("*.parquet"):
            table = pq.ParquetFile(file).read()
            values = {
                "older": (5, None, 1, None, None),
                "newer": (10, 2, None, 2, None),
                "loser": (500, 500, None, None, None),
                "raw-repo": (3, 1, None, None, 1),
            }
            ids = table["id"].to_pylist()
            from drover.server.lake.coverage import TOKEN_COLUMNS

            for index, name in enumerate(TOKEN_COLUMNS):
                table = table.append_column(
                    name, pa.array([values[key][index] for key in ids], type=pa.int64())
                )
            pq.write_table(table, file)

    monkeypatch.setattr(test_mcp_tools, "_write_agent_events", write)


@pytest.fixture
def typed_lake(typed_seed, verified_lake):
    return verified_lake


def _producer_inventory(path):
    import duckdb

    from drover.server.lake.coverage import native_inventory
    from drover.server.lake.rebuild import EVENT_SCHEMA
    from drover.server.lake.runtime import literal

    files = sorted((path.parent / "parquet" / "agent_events").rglob("*.parquet"))
    with duckdb.connect() as con:
        con.execute("SET TimeZone='UTC'")
        con.execute(
            "CREATE VIEW incoming AS SELECT * FROM read_parquet(["
            + ",".join(literal(str(file)) for file in files)
            + "],hive_partitioning=true)"
        )
        columns = {row[0] for row in con.execute("DESCRIBE incoming").fetchall()}
        select = ",".join(
            f'CAST("{n}" AS {t}) AS "{n}"' if n in columns else f'NULL::{t} AS "{n}"'
            for n, t in EVENT_SCHEMA.items()
        )
        con.execute(
            "CREATE VIEW producer AS SELECT "
            + select
            + " FROM incoming WHERE id<>'loser'"
        )
        return native_inventory(con, "producer")


def test_typed_native_usage_parity_and_identity_staleness(typed_lake):
    import json
    from dataclasses import asdict

    from drover.server.db import control_plane_connection, open_duckdb_connection
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.lake import coverage
    from drover.server.lake.read_models import read_model
    from drover.server.native_usage_rollup import _load_partition_totals

    _, path, config = typed_lake
    with open_duckdb_connection(path) as con:
        expected = [asdict(row) for row in _load_partition_totals(con, "2026-10-01")]
    for row in expected:
        row["source_event_count"] = row.pop("event_count")
    configure_analytics(path, config)
    coverage.provision_coverage(path)
    coverage.publish_source(
        path,
        "native",
        _producer_inventory(path),
        publisher="collector",
        watermark="typed-v1",
        observed_at=datetime.now(timezone.utc),
    )
    generation = coverage.certify(path, "native")
    with control_plane_connection(path) as pg:
        payload = json.loads(
            pg.execute(
                "SELECT payload FROM lake_coverage_generations WHERE generation=?",
                [generation],
            ).fetchone()[0]
        )
    assert payload["usage"] == expected
    assert payload["usage"][1]["input_tokens"] == 15
    metadata = read_model(
        path, "cockpit", filters={"days": 30}, cursor_secret="00" * 32
    )["metadata"]
    assert metadata["native_usage"]["freshness"] == "fresh"
    registry = HarnessRegistry(path)
    registry.register_host(host_id="control", display_name="Control", kind="test")
    registry.create_session(
        host_id="control",
        harness="codex",
        command="codex",
        session_id="control-session",
    )
    with control_plane_connection(path) as pg:
        pg.execute(
            "UPDATE harness_sessions SET native_session_id='s' WHERE session_id='control-session'"
        )
    result = read_model(path, "cockpit", filters={"days": 30}, cursor_secret="00" * 32)
    assert result["metadata"]["native_usage"]["freshness"] == "unavailable"
    coverage.certify(path, "native")
    with control_plane_connection(path) as pg:
        latest = json.loads(
            pg.execute(
                "SELECT payload FROM lake_coverage_generations WHERE kind='native' ORDER BY receipt_seq DESC LIMIT 1"
            ).fetchone()[0]
        )
    assert [r["session_id"] for r in latest["usage"]] == ["raw-only"]


def _context():
    return dict(
        context_id="ctx",
        container_type="research_thread",
        label="Curated",
        session_ids=["s"],
        task_ids=[],
        repo_owner="o",
        repo_name="r",
        summary_md="Known context",
        next_action="Continue",
    )


def test_coverage_bounds_and_receipt_atomicity(verified_lake, monkeypatch):
    from drover.server.db import control_plane_connection
    from drover.server.lake import coverage
    from drover.server.lake.runtime import LakeError
    from drover.server.mcp import tools

    _, path, config = verified_lake
    configure_analytics(path, config)
    coverage.provision_coverage(path)
    publish = lambda rows: coverage.publish_source(
        path,
        "contexts",
        rows,
        publisher="curator",
        watermark="v1",
        observed_at=datetime.now(timezone.utc),
    )
    with pytest.raises(LakeError, match="row_limit"):
        publish([dict(_context(), context_id=str(i)) for i in range(1001)])
    with pytest.raises(LakeError, match="byte_limit"):
        publish([dict(_context(), summary_md="x" * 1100000)])
    publish([_context()])

    def crash(pg):
        raise RuntimeError("crash-before-receipt")

    with monkeypatch.context() as patch:
        patch.setattr(coverage, "_before_receipt", crash)
        with pytest.raises(RuntimeError, match="crash"):
            coverage.certify(path, "contexts")
    with control_plane_connection(path) as pg:
        assert (
            pg.execute("SELECT count(*) FROM lake_coverage_generations").fetchone()[0]
            == 0
        )

    def lose_fence(pg):
        pg.execute("SELECT pg_advisory_unlock(?)", [coverage.PROJECTION_LOCK])

    with monkeypatch.context() as patch:
        patch.setattr(coverage, "_before_receipt", lose_fence)
        with pytest.raises(LakeError, match="coverage_changed"):
            coverage.certify(path, "contexts")
    generation = coverage.certify(path, "contexts")
    assert (
        tools.drover_resume_context(
            duckdb_path=path, context_id="ctx", max_summaries=1001
        )["reason"]
        == "analytics_row_limit_exceeded"
    )
    with control_plane_connection(path) as pg:
        pg.execute(
            "UPDATE lake_coverage_generations SET payload=repeat('x',1100000) WHERE generation=?",
            [generation],
        )
    assert (
        tools.drover_context_brief(duckdb_path=path, context_id="ctx")["reason"]
        == "analytics_byte_limit_exceeded"
    )


def test_new_source_head_during_read_never_returns_old_certificate(
    verified_lake, monkeypatch
):
    from drover.server.lake import coverage, read_models
    from drover.server.lake.runtime import LakeError

    _, path, config = verified_lake
    configure_analytics(path, config)
    coverage.provision_coverage(path)
    publish = lambda: coverage.publish_source(
        path,
        "contexts",
        [_context()],
        publisher="curator",
        watermark="v1",
        observed_at=datetime.now(timezone.utc),
    )
    publish()
    coverage.certify(path, "contexts")
    original = read_models.query

    def replace_head(*args, **kwargs):
        result = original(*args, **kwargs)
        publish()
        return result

    monkeypatch.setattr(read_models, "query", replace_head)
    with pytest.raises(LakeError, match="read_model_changed"):
        read_models.read_model(path, "contexts")


def test_context_bundle_summary_and_bounds(verified_lake, monkeypatch):
    from memory_helpers import put_summary

    from drover.server.lake import coverage
    from drover.server.mcp import tools
    from drover.server.recall_bundle import RecallBundleService

    _, path, config = verified_lake
    put_summary(
        path,
        "s",
        project_key="o/r",
        summary_md="Known continuation",
        next_steps_md="Continue",
    )
    configure_analytics(path, config)
    coverage.provision_coverage(path)
    coverage.publish_source(
        path,
        "contexts",
        [_context()],
        publisher="curator",
        watermark="v1",
        observed_at=datetime.now(timezone.utc),
    )
    coverage.certify(path, "contexts")
    result = tools.drover_resume_context(duckdb_path=path, context_id="ctx")
    assert result["session_summaries"][0]["summary_md"] == "Known continuation"
    bundle = RecallBundleService(duckdb_path=path).recall_bundle("remember", repo="o/r")
    assert (
        bundle["drover_context"]["repository_open_loops"][0]["source_identifiers"][
            "context_id"
        ]
        == "ctx"
    )
    assert bundle["limits"]["used_chars"] <= 24000
    assert bundle["metadata"]["contexts"]["freshness"] == "fresh"
    assert bundle["metadata"]["native_publication"]["freshness"] == "unavailable"
    put_summary(path, "s", project_key="o/r", summary_md="x" * 1100000)
    result = tools.drover_resume_context(duckdb_path=path, context_id="ctx")
    assert result["reason"] == "analytics_byte_limit_exceeded"


def test_native_stale_corrupt_and_epoch_certificates(verified_lake):
    from drover.server.db import control_plane_connection
    from drover.server.lake import coverage
    from drover.server.lake.read_models import read_model

    _, path, config = verified_lake
    configure_analytics(path, config)
    coverage.provision_coverage(path)
    payload = _producer_inventory(path)

    def publish(observed_at):
        coverage.publish_source(
            path,
            "native",
            payload,
            publisher="collector",
            watermark="v1",
            observed_at=observed_at,
        )
        return coverage.certify(path, "native")

    generation = publish(datetime.now(timezone.utc))
    configure_analytics(path, replace(config, epoch="new"))
    metadata = read_model(
        path, "cockpit", filters={"days": 30}, cursor_secret="00" * 32
    )["metadata"]
    assert metadata["native_publication"]["freshness"] == "unavailable"
    generation = coverage.certify(path, "native")
    with control_plane_connection(path) as pg:
        pg.execute(
            "UPDATE lake_coverage_generations SET payload='{}' WHERE generation=?",
            [generation],
        )
    metadata = read_model(
        path, "cockpit", filters={"days": 30}, cursor_secret="00" * 32
    )["metadata"]
    assert metadata["native_usage"]["freshness"] == "unavailable"
    assert metadata["native_usage"]["reason"] == "analytics_coverage_incomplete"
    for observed_at in (
        datetime.now(timezone.utc) - timedelta(hours=1),
        datetime.now(timezone.utc) + timedelta(hours=1),
    ):
        publish(observed_at)
        metadata = read_model(
            path, "cockpit", filters={"days": 30}, cursor_secret="00" * 32
        )["metadata"]
        assert metadata["native_usage"]["freshness"] == "unavailable"
        assert metadata["native_usage"]["reason"] == "analytics_coverage_stale"


@pytest.mark.parametrize("count", [1000, 1001])
def test_native_usage_generation_row_boundary(count):
    from drover.server.lake import coverage
    from drover.server.lake.runtime import LakeError

    rows = [
        dict(
            session_id=str(i),
            input_tokens=1,
            output_tokens=None,
            cache_read_tokens=None,
            cache_write_tokens=None,
            reasoning_tokens=None,
            turn_count=1,
            source_event_count=1,
        )
        for i in range(count)
    ]
    payload = dict(usage=rows, usage_events=count, inventory=dict(rows=count))
    if count > coverage.MAX_ROWS:
        with pytest.raises(LakeError, match="analytics_row_limit_exceeded"):
            coverage._usage(payload)
    else:
        coverage._usage(payload)
        payload["usage_events"] += 1
        with pytest.raises(LakeError, match="analytics_coverage_incomplete"):
            coverage._usage(payload)


def test_native_usage_generation_byte_and_shape_bounds():
    from drover.server.lake import coverage
    from drover.server.lake.runtime import LakeError

    with pytest.raises(LakeError, match="analytics_byte_limit_exceeded"):
        coverage._usage(dict(usage=[dict(session_id="x" * 1100000)]))
    with pytest.raises(LakeError, match="analytics_coverage_incomplete"):
        coverage._usage(dict(usage={}))


@pytest.mark.parametrize("payload", [None, [], "inventory"])
def test_native_source_shape_errors_are_explicit(tmp_path, payload):
    from drover.server.lake import coverage
    from drover.server.lake.runtime import LakeError

    with pytest.raises(LakeError, match="analytics_coverage_source_invalid"):
        coverage.publish_source(
            tmp_path / "unused.duckdb",
            "native",
            payload,
            publisher="collector",
            watermark="v1",
            observed_at=datetime.now(timezone.utc),
        )
