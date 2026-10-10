"""Task projection generations use only private catalogs and PG fixtures."""

from dataclasses import replace

import pytest
from test_control_outbox import postgres_control_store
from test_lake_runtime import lake_spec
from test_lake_serving import verified_lake

from drover.config import AnalyticsConfig
from drover.server.lake.runtime import LakeError
from drover.server.lake.serving import configure_analytics


@pytest.fixture
def task_seed(monkeypatch):
    import test_mcp_tools

    original = test_mcp_tools._write_agent_events

    def write(parquet, rows):
        for row in rows:
            row["task_id"] = "task-s" if row["session_id"] == "s" else "task-other"
        original(parquet, rows)

    monkeypatch.setattr(test_mcp_tools, "_write_agent_events", write)


@pytest.fixture
def task_lake(task_seed, verified_lake):
    return verified_lake


def test_task_projection_parity_selection_and_missing_generation(
    task_lake, monkeypatch
):
    from drover.server.db import open_duckdb_connection
    from drover.server.lake.task_projection import (
        provision_task_projections,
        refresh_task_projections,
    )
    from drover.server.mcp import tools

    spec, path, config = task_lake
    with open_duckdb_connection(path) as con:
        con.execute(
            "INSERT INTO tasks(task_id,repo_owner,repo_name,branch,status,created_at,total_cost_usd) VALUES ('task-s','o','r','main','active','2026-10-01',NULL)"
        )
    from memory_helpers import put_summary

    put_summary(path, "s", task_id="task-s", summary_md="verified task summary")
    legacy = tools.drover_task_status(duckdb_path=path, task_id="task-s")
    configure_analytics(path, config)
    assert (
        tools.drover_task_status(duckdb_path=path, task_id="task-s")["status"]
        == "unavailable"
    )
    provision_task_projections(path)
    assert (
        tools.drover_task_status(duckdb_path=path, task_id="task-s")["status"]
        == "unavailable"
    )
    refresh_task_projections(path)
    original_connect = tools._connect
    monkeypatch.setattr(
        tools, "_connect", lambda *a, **kw: pytest.fail("legacy task read")
    )
    lake = tools.drover_task_status(duckdb_path=path, task_id="task-s")
    for key in [
        "task_id",
        "repo_owner",
        "repo_name",
        "branch",
        "principal_id",
        "session_count",
        "agent_count",
        "latest_summary",
    ]:
        assert lake[key] == legacy[key]
    from datetime import datetime

    assert datetime.fromisoformat(lake["last_activity_at"]) == datetime.fromisoformat(
        legacy["last_activity_at"]
    )
    assert lake["total_cost_usd"] is None
    assert (
        tools.drover_task_status(duckdb_path=path, session_id="s")["task_id"]
        == "task-s"
    )
    assert (
        tools.drover_task_status(duckdb_path=path, task_id="absent")["status"]
        == "unknown"
    )
    configure_analytics(path, AnalyticsConfig())
    monkeypatch.setattr(tools, "_connect", original_connect)
    assert (
        tools.drover_task_status(duckdb_path=path, task_id="task-s")["session_count"]
        == legacy["session_count"]
    )


def test_projection_failed_publish_epoch_change_and_incomplete_rows(
    task_lake, monkeypatch
):
    from drover.server.db import control_plane_connection
    from drover.server.lake import task_projection as projection
    from drover.server.mcp import tools

    spec, path, config = task_lake
    configure_analytics(path, config)
    projection.provision_task_projections(path)
    projection.refresh_task_projections(path)
    configure_analytics(path, replace(config, epoch="renewed"))
    assert (
        tools.drover_task_status(duckdb_path=path, task_id="task-s")["status"]
        == "unavailable"
    )

    def fail(*args):
        raise LakeError("projection_interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(projection, "_before_receipt", fail)
        with pytest.raises(LakeError, match="interrupted"):
            projection.refresh_task_projections(path)
    assert (
        tools.drover_task_status(duckdb_path=path, task_id="task-s")["status"]
        == "unavailable"
    )
    projection.refresh_task_projections(path)
    with control_plane_connection(path) as con:
        con.execute("UPDATE lake_task_rows SET payload='{}' WHERE task_id='task-s'")
    assert (
        tools.drover_task_status(duckdb_path=path, task_id="task-s")["status"]
        == "unavailable"
    )
    # Corruption of a different task also invalidates the whole generation.
    assert (
        tools.drover_task_status(duckdb_path=path, task_id="task-other")["status"]
        == "unavailable"
    )


def test_projection_fence_and_unverified_catalog(task_lake):
    from drover.server.lake import task_projection as projection
    from drover.server.mcp import tools

    spec, path, config = task_lake
    configure_analytics(path, config)
    projection.provision_task_projections(path)
    with projection.projection_fence(path):
        with pytest.raises(LakeError, match="fenced"):
            projection.refresh_task_projections(path)
    projection.refresh_task_projections(path)
    (spec.data_root / "verification/serving-proof.json").write_text("{}")
    assert (
        tools.drover_task_status(duckdb_path=path, task_id="task-s")["status"]
        == "unavailable"
    )
    with pytest.raises(LakeError, match="verification"):
        projection.refresh_task_projections(path)


def test_lost_projection_fence_cannot_publish_receipt(task_lake, monkeypatch):
    from drover.server.db import control_plane_connection
    from drover.server.lake import task_projection as projection
    from drover.server.mcp import tools

    spec, path, config = task_lake
    configure_analytics(path, config)
    projection.provision_task_projections(path)

    def release(con):
        con.execute("SELECT pg_advisory_unlock(?)", [projection.PROJECTION_LOCK])

    monkeypatch.setattr(projection, "_before_receipt", release)
    with pytest.raises(LakeError, match="fence_lost"):
        projection.refresh_task_projections(path)
    with control_plane_connection(path) as con:
        assert (
            con.execute("SELECT count(*) FROM lake_task_generations").fetchone()[0] == 0
        )
        assert con.execute("SELECT count(*) FROM lake_task_rows").fetchone()[0] == 0
    assert (
        tools.drover_task_status(duckdb_path=path, task_id="task-s")["status"]
        == "unavailable"
    )


def test_receipted_export_invalidates_old_tasks_and_refreshes_atomically(
    task_lake, monkeypatch
):
    import hashlib
    import threading

    from test_lake_exporter import seed

    from drover.config import default_config
    from drover.server.lake import task_projection as projection
    from drover.server.lake.exporter import LakeOutboxExporter, provision_exporter
    from drover.server.lake.lifecycle import selected_exporter
    from drover.server.lake.rebuild import verify
    from drover.server.mcp import tools

    spec, path, config = task_lake
    provision_exporter(spec)
    verify(spec)
    config = replace(
        config,
        verification_sha256=hashlib.sha256(
            (spec.data_root / "verification/serving-proof.json").read_bytes()
        ).hexdigest(),
    )
    configure_analytics(path, config)
    projection.provision_task_projections(path)
    projection.refresh_task_projections(path)
    seed(path, count=1)
    with LakeOutboxExporter(control_path=path, spec=spec) as exporter:
        exporter.run_once()
    # The export itself is verified, but an older PG generation is incomplete.
    assert (
        tools.drover_task_status(duckdb_path=path, task_id="task-s")["status"]
        == "unavailable"
    )
    monkeypatch.setenv("DROVER_TASK_EXPORT_DSN", spec.dsn())
    enabled = replace(
        config, exporter_enabled=True, exporter_dsn_env="DROVER_TASK_EXPORT_DSN"
    )
    configure_analytics(path, enabled)
    from drover.server.harness.registry import HarnessRegistry

    HarnessRegistry(path).append_event(
        session_id="export-session",
        event_id="event-new",
        seq=2,
        event_type="assistant_output",
        payload={"text": "new task generation"},
    )
    lifecycle = selected_exporter(
        replace(default_config(), duckdb_path=path, analytics=enabled)
    )
    try:
        lifecycle.start(shutdown_event=threading.Event())
        import time

        from drover.server.db import control_plane_connection

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with control_plane_connection(path) as con:
                completed = con.execute(
                    "SELECT count(*) FROM lake_task_generations"
                ).fetchone()[0]
            if completed >= 3:
                break
            time.sleep(0.05)
        assert completed >= 3
        assert (
            tools.drover_task_status(duckdb_path=path, session_id="export-session")[
                "status"
            ]
            == "observed"
        )
        assert (
            tools.drover_task_status(duckdb_path=path, task_id="task-s")[
                "session_count"
            ]
            == 1
        )
    finally:
        lifecycle.stop()


def test_changed_identity_rejects_old_generation(task_lake):
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.lake import task_projection as projection
    from drover.server.mcp import tools

    spec, path, config = task_lake
    configure_analytics(path, config)
    projection.provision_task_projections(path)
    projection.refresh_task_projections(path)
    registry = HarnessRegistry(path)
    registry.register_host(host_id="new-host", display_name="Fixture", kind="test")
    registry.create_session(
        host_id="new-host",
        harness="claude-code",
        session_id="new-session",
        command="claude",
    )
    assert (
        tools.drover_task_status(duckdb_path=path, task_id="task-s")["status"]
        == "unavailable"
    )
    projection.refresh_task_projections(path)
    assert (
        tools.drover_task_status(duckdb_path=path, task_id="task-s")["session_count"]
        == 1
    )


def test_ambiguous_identity_and_failed_lifecycle_refresh_stay_closed(
    task_lake, monkeypatch
):
    import threading

    from drover.config import default_config
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.lake import lifecycle
    from drover.server.lake import task_projection as projection
    from drover.server.mcp import tools

    spec, path, config = task_lake
    configure_analytics(path, config)
    registry = HarnessRegistry(path)
    registry.register_host(
        host_id="ambiguous-host", display_name="Fixture", kind="test"
    )
    for sid in ("alias-one", "alias-two"):
        registry.create_session(
            host_id="ambiguous-host", session_id=sid, harness="codex", command="codex"
        )
    with control_plane_connection(path) as con:
        con.execute(
            "UPDATE harness_sessions SET native_session_id='alias' WHERE host_id='ambiguous-host'"
        )
    projection.provision_task_projections(path)
    projection.refresh_task_projections(path)
    assert (
        tools.drover_task_status(duckdb_path=path, session_id="alias")["reason"]
        == "analytics_task_identity_ambiguous"
    )
    released = threading.Event()

    class Exporter:
        def __init__(self, *, spec, **kwargs):
            self.spec = spec

        def __enter__(self):
            return self

        def __exit__(self, *args):
            released.set()

        def run_once(self):
            pytest.fail("claim after failed task refresh")

    def fail(*args, **kwargs):
        raise LakeError("analytics_task_projection_fenced")

    monkeypatch.setattr(lifecycle, "LakeOutboxExporter", Exporter)
    monkeypatch.setattr(projection, "refresh_if_provisioned", fail)
    monkeypatch.setenv("DROVER_TASK_EXPORT_DSN", spec.dsn())
    enabled = replace(
        config, exporter_enabled=True, exporter_dsn_env="DROVER_TASK_EXPORT_DSN"
    )
    configure_analytics(path, enabled)
    worker = lifecycle.selected_exporter(
        replace(default_config(), duckdb_path=path, analytics=enabled)
    )
    with pytest.raises(LakeError, match="projection_fenced"):
        worker.start(shutdown_event=threading.Event())
    assert released.is_set() and not worker.health()["enabled"]


def test_zero_winner_ack_still_refreshes_and_refresh_error_releases_lifecycle(
    tmp_path, monkeypatch
):
    import threading

    from drover.config import default_config
    from drover.server.lake import lifecycle
    from drover.server.lake import task_projection as projection

    path = tmp_path / "unused"
    config = AnalyticsConfig(
        backend="ducklake",
        exporter_enabled=True,
        catalog_dsn_env="UNUSED_READER",
        exporter_dsn_env="UNUSED_EXPORTER",
        data_root=str(tmp_path / "lake"),
        extension_dir=str(tmp_path / "ext"),
        engine_sha256="0" * 64,
        verification_sha256="0" * 64,
        epoch="test",
    )
    configure_analytics(path, config)
    calls, released = [], threading.Event()

    class Exporter:
        def __init__(self, *, spec, **kwargs):
            self.spec = spec

        def __enter__(self):
            return self

        def __exit__(self, *args):
            released.set()

        def run_once(self):
            return {"exported": 0, "acknowledged": 1}

    class History:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, *args):
            pass

    def refresh(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise LakeError("analytics_task_projection_changed")

    monkeypatch.setattr(lifecycle, "LakeOutboxExporter", Exporter)
    monkeypatch.setattr(lifecycle, "HistoryConnection", History)
    monkeypatch.setattr(lifecycle, "catalog_identity", lambda spec: "same")
    monkeypatch.setattr(projection, "refresh_if_provisioned", refresh)
    worker = lifecycle.selected_exporter(
        replace(default_config(), duckdb_path=path, analytics=config)
    )
    worker._run(threading.Event())
    assert calls == [1, 1] and released.is_set()
    assert {key: worker.health()[key] for key in ("enabled", "last_error")} == {
        "enabled": False,
        "last_error": "analytics_task_projection_changed",
    }


def test_corrupt_task_key_is_bounded_before_pg_text_fetch(task_lake, monkeypatch):
    from contextlib import contextmanager

    from drover.server.db import control_plane_connection
    from drover.server.lake import task_projection as projection
    from drover.server.mcp import tools

    spec, path, config = task_lake
    configure_analytics(path, config)
    projection.provision_task_projections(path)
    projection.refresh_task_projections(path)
    with control_plane_connection(path) as con:
        con.execute(
            "UPDATE lake_task_rows SET task_id=repeat('x',5000) WHERE task_id='task-s'"
        )
    monkeypatch.setattr(projection, "MAX_BYTES", 2048)

    @contextmanager
    def guarded(path):
        with control_plane_connection(path) as con:

            class Guard:
                def execute(self, sql, *args):
                    if sql.startswith("SELECT task_id,encode"):
                        pytest.fail("unbounded task key reached parent fetch")
                    return con.execute(sql, *args)

            yield Guard()

    monkeypatch.setattr(projection, "control_plane_connection", guarded)
    assert (
        tools.drover_task_status(duckdb_path=path, task_id="task-other")["reason"]
        == "analytics_task_projection_incomplete"
    )


def test_task_summary_pg_error_is_explicit_unavailable(task_lake, monkeypatch):
    import psycopg

    from drover.server.lake import task_projection as projection
    from drover.server.mcp import tools

    spec, path, config = task_lake
    configure_analytics(path, config)
    projection.provision_task_projections(path)
    projection.refresh_task_projections(path)

    class UnavailableMemory:
        def recent_summaries(self, **kwargs):
            raise psycopg.OperationalError("fixture summary read failure")

    monkeypatch.setattr(tools, "_memory", lambda path: UnavailableMemory())
    result = tools.drover_task_status(duckdb_path=path, task_id="task-s")
    assert result["status"] == "unavailable"
    assert result["reason"] == "analytics_task_summary_unavailable"


def test_paged_publication_above_combined_limit_and_atomic_failure(
    task_lake, monkeypatch
):
    from drover.server.db import control_plane_connection
    from drover.server.lake import task_projection as projection

    _, path, config = task_lake
    configure_analytics(path, config)
    projection.provision_task_projections(path)
    original_capture = projection._capture
    rows = [dict(task_id=f"task-{i:05}", status="observed") for i in range(1001)]
    sessions = [(f"session-{i:05}", row["task_id"]) for i, row in enumerate(rows)]
    calls = []

    def capture(path, *, build=False, kind=None, after=None):
        binding, result = original_capture(path)
        if build:
            calls.append((kind, after))
            # Compatible with the old unpaged call so RED exercises its cap.
            values = rows if kind == "tasks" else sessions
            if kind is None:
                return binding, {**result, "tasks": rows, "sessions": sessions}
            page = [
                r
                for r in values
                if after is None or (r["task_id"] if kind == "tasks" else r[0]) > after
            ][: projection.PAGE_ROWS]
            return binding, {**result, kind: page}
        return binding, result

    monkeypatch.setattr(projection, "_capture", capture)
    generation = projection.refresh_task_projections(path)
    assert (
        projection.task_status(path, session_id="session-01000")["task_id"]
        == "task-01000"
    )
    assert len(calls) >= 6
    with control_plane_connection(path) as con:
        assert con.execute(
            "SELECT task_count,session_count FROM lake_task_generations WHERE generation=?",
            [generation],
        ).fetchone() == (1001, 1001)
    monkeypatch.setattr(
        projection,
        "_before_receipt",
        lambda con: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError, match="crash"):
        projection.refresh_task_projections(path)

    def changed_capture(path, **options):
        binding, result = capture(path, **options)
        if options.get("after") is not None:
            binding = {**binding, "identities": "changed-between-pages"}
        return binding, result

    monkeypatch.setattr(projection, "_capture", changed_capture)
    with pytest.raises(LakeError, match="analytics_task_projection_changed"):
        projection.refresh_task_projections(path)
    monkeypatch.setattr(projection, "_capture", capture)
    with control_plane_connection(path) as con:
        assert (
            con.execute("SELECT count(*) FROM lake_task_generations").fetchone()[0] == 1
        )
        assert con.execute("SELECT count(*) FROM lake_task_rows").fetchone()[0] == 1001
        con.execute("DELETE FROM lake_task_sessions WHERE session_id='session-01000'")
    with pytest.raises(LakeError, match="analytics_task_projection_incomplete"):
        projection.task_status(path, task_id="task-00000")


def test_native_child_keyset_pages_are_complete_and_deterministic():
    import duckdb

    from drover.server.lake.task_projection import PAGE_ROWS, build_in_child

    with duckdb.connect() as con:
        con.execute("CREATE SCHEMA lake")
        con.execute("CREATE MACRO lake.snapshots() AS TABLE SELECT 1 snapshot_id")
        con.execute("""CREATE TABLE agent_events AS SELECT
            printf('%05d',i) task_id, printf('%05d',i) session_id,
            i id, 'a' agent_id, 'o' repo_owner, 'r' repo_name,
            'main' branch, 'p' principal_id, TIMESTAMPTZ '2026-10-01' AS timestamp
            FROM range(1201) t(i)""")
        con.execute(
            "INSERT INTO agent_events SELECT '', '', id, agent_id, repo_owner, repo_name, branch, principal_id, timestamp FROM agent_events LIMIT 1"
        )
        for kind in ("tasks", "sessions"):
            after = None
            keys = []
            while True:
                result = build_in_child(con, build=True, kind=kind, after=after)
                page = result[kind]
                assert len(page) <= PAGE_ROWS
                keys.extend(
                    row["task_id"] if kind == "tasks" else row[0] for row in page
                )
                if len(page) < PAGE_ROWS:
                    break
                after = keys[-1]
            assert keys == [""] + [f"{i:05d}" for i in range(1201)]


def test_publication_accounts_for_pg_page_bytes_before_receipt(task_lake, monkeypatch):
    from drover.server.db import control_plane_connection
    from drover.server.lake import task_projection as projection

    _, path, config = task_lake
    configure_analytics(path, config)
    projection.provision_task_projections(path)
    capture = projection._capture
    binding, result = capture(path)
    rows = [dict(task_id=f"{i:05}" + "x" * 800, status="observed") for i in range(500)]

    def pages(path, *, build=False, kind=None, after=None):
        if not build:
            return binding, result
        page = (
            [r for r in rows if after is None or r["task_id"] > after]
            if kind == "tasks"
            else []
        )
        return binding, {**result, kind: page[: projection.PAGE_ROWS]}

    monkeypatch.setattr(projection, "_capture", pages)
    projection.refresh_task_projections(path)
    assert (
        projection.task_status(path, task_id=rows[-1]["task_id"])["status"]
        == "observed"
    )
    # Fits the child JSON limit, but exceeds the reader's key + payload bound.
    for row in rows:
        row["task_id"] += "x" * 300
        row["padding"] = "x" * 500
    with pytest.raises(LakeError, match="analytics_task_projection_page_limit"):
        projection.refresh_task_projections(path)
    with control_plane_connection(path) as con:
        assert (
            con.execute("SELECT count(*) FROM lake_task_generations").fetchone()[0] == 1
        )

    # A malformed newest receipt must never be filtered away in favor of an
    # older valid generation bound to the same snapshot.
    from uuid import uuid4

    for manifest in ("x" * (1024 * 1024 + 1), '{"tasks":{},"sessions":{}}'):
        with control_plane_connection(path) as con:
            con.execute(
                "INSERT INTO lake_task_generations(generation,binding,manifest,task_count,session_count) SELECT ?,binding,?,task_count,session_count FROM lake_task_generations ORDER BY receipt_seq DESC LIMIT 1",
                [str(uuid4()), manifest],
            )
        with pytest.raises(LakeError, match="analytics_task_projection_incomplete"):
            projection.task_status(path, task_id=rows[0]["task_id"])
