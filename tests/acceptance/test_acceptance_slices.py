"""Executable v2 contracts. Only known product gaps are strict expected failures."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest
import requests
from click.testing import CliRunner
from lake_generator import BASE_ANCHOR_DATE, BIG_SESSION_ID

from drover.config import AnalyticsConfig, default_config
from drover.schema import bootstrap, bootstrap_control_plane_store
from drover.server.__main__ import main
from drover.server.control_store import postgres_control_store
from drover.server.harness.registry import HarnessRegistry
from drover.server.lake.serving import configure_analytics, open_history
from drover.server.ledger import SUMMARIZE_SESSION, JobLedger
from drover.server.mcp.tools import (
    drover_recall,
    drover_session_replay,
    drover_session_summary,
)
from drover.server.summarizer.jobs import enqueue_summary_generation
from drover.server.summarizer.worker import MAX_RAW_EVENTS_PER_SESSION, SummarizerWorker


def _event(index=0, session_id="acceptance-session"):
    return {
        "id": f"acceptance-event-{index}",
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": "user_message",
        "agent_id": "acceptance-host",
        "message": {"role": "user", "content": f"acceptance harness turn {index}"},
        "raw_data": {
            "harness": "claude",
            "_repo_owner": "arniesaha",
            "_repo_name": "drover",
        },
    }


def _write_events(path, events):
    from drover.models import AgentEvent

    for event in events:
        AgentEvent.model_validate(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events))


def _register(path, session_id, **session_options):
    registry = HarnessRegistry(path)
    registry.register_host(
        host_id="acceptance-host", display_name="Acceptance", kind="test"
    )
    registry.create_session(
        host_id="acceptance-host",
        harness="claude",
        command="test",
        session_id=session_id,
        repo_owner="arniesaha",
        repo_name="drover",
        branch="main",
        **session_options,
    )
    return registry


@pytest.fixture
def hub_http(prod_shaped, tmp_path):
    """Real HTTP handler, bound only to loopback, with all config paths isolated."""
    from drover.server.cockpit.service import CockpitService
    from drover.server.metrics import MetricsCollector
    from drover.server.web.app import start_metrics_server

    collector = MetricsCollector(
        duckdb_path=prod_shaped,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        config_path=tmp_path / "config.toml",
        cockpit_service=CockpitService(provider_usage=None, duckdb_path=prod_shaped),
    )
    server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


# S2: the real collector/watch lifecycle must publish to the selected lake sink.
def test_a1_collector_jsonl_in_lake_and_mcp_replay_recall(
    hub_small_lake, prod_shaped, tmp_path
):
    from drover.server.watcher import IncomingWatcher

    legacy = tmp_path / "legacy"
    bootstrap(parquet_dir=legacy, duckdb_path=prod_shaped)
    incoming = tmp_path / "incoming"
    watcher = IncomingWatcher(
        incoming_dir=incoming, parquet_dir=legacy, duckdb_path=prod_shaped
    )
    from drover.server.lake.lifecycle import selected_exporter

    # Start the same exporter lifecycle as the hub; a read-only selection
    # alone could never make the 30-second publication contract pass.
    exporter = selected_exporter(
        replace(
            default_config(),
            duckdb_path=prod_shaped,
            parquet_dir=legacy,
            incoming_dir=incoming,
            analytics=hub_small_lake.config,
        )
    )
    exporter.start(shutdown_event=threading.Event())
    watcher.start()
    event = _event(session_id=f"acceptance-{uuid4().hex}")
    event["id"] = f"acceptance-event-{uuid4().hex}"
    deadline = time.monotonic() + 30
    try:
        from drover.collect.sources import write_events_jsonl
        from drover.models import AgentEvent

        # Use the collector's fsync + atomic rename, so the watcher never
        # races a half-written JSONL file.
        write_events_jsonl(
            [AgentEvent.model_validate(event)],
            incoming,
            run_id="fixture",
            source_id="collector",
        )
        found = None
        while time.monotonic() < deadline:
            with open_history(prod_shaped) as con:
                found = con.execute(
                    "SELECT id, session_id FROM agent_events WHERE id=?", [event["id"]]
                ).fetchone()
            if found:
                break
            time.sleep(0.25)
        assert exporter.health()["last_error"] is None, exporter.health()
        assert found == (
            event["id"],
            event["session_id"],
        ), "Collector event not in lake agent_events within 30 s"
        replay = drover_session_replay(
            duckdb_path=prod_shaped, session_id=event["session_id"]
        )
        assert replay["status"] == "ok", replay
        assert any(row["id"] == event["id"] for row in replay["events"])
        # Recall indexes summaries, so drain the real summary job before asking it.
        _summarize(prod_shaped, event["session_id"])
        recall = drover_recall(
            duckdb_path=prod_shaped,
            query="acceptance harness",
            repo_owner="arniesaha",
            repo_name="drover",
        )
        assert any(
            row["session_id"] == event["session_id"] for row in recall["results"]
        ), recall
    finally:
        watcher.stop()
        exporter.stop()


# S2: all producers dedupe in the outbox, with no parquet write during ingest.
@pytest.mark.parametrize("backend", ["legacy", "ducklake"])
def test_a2_outbox_events_dedup_semantics_no_parquet_ingest(
    prod_shaped, hub_small_lake, hub_http, tmp_path, backend
):
    from drover.server.watcher import ingest_incoming_file_once

    legacy = tmp_path / "legacy"
    bootstrap(parquet_dir=legacy, duckdb_path=prod_shaped)
    if backend == "legacy":
        configure_analytics(prod_shaped, AnalyticsConfig())
    registry = _register(prod_shaped, "acceptance-session")
    collector_event = _event()
    # Real harness event recording and HTTP API, each replayed to test deduplication.
    for _ in range(2):
        registry.append_events_if_new(
            [
                dict(
                    event_id="harness-event",
                    session_id="acceptance-session",
                    event_type="user_input",
                    payload={"text": "harness"},
                    seq=1,
                )
            ]
        )
        response = requests.post(
            hub_http + "/harness/events",
            json={
                "events": [
                    dict(
                        event_id="api-event",
                        session_id="acceptance-session",
                        type="user_input",
                        seq=2,
                        text="api",
                    )
                ]
            },
            timeout=10,
        )
        assert response.status_code == 200, response.text
        source = tmp_path / "incoming" / "collector.jsonl"
        _write_events(source, [collector_event])
        ingest_incoming_file_once(source, parquet_dir=legacy, duckdb_path=prod_shaped)
    with postgres_control_store(prod_shaped).connection() as con:
        counts = dict(
            con.execute(
                "SELECT event_id, count(*) FROM control_outbox_events GROUP BY event_id"
            ).fetchall()
        )
    assert counts == {
        "harness-event": 1,
        "api-event": 1,
        "acceptance-event-0": 1,
    }, f"Outbox events missing or duplicated: {counts}"
    files = [p for p in legacy.rglob("*.parquet") if "_seed" not in str(p)]
    assert not files, f"Ingest wrote parquet in {backend} mode: {files}"


def _summarize(path, session_id):
    enqueue_summary_generation(path, session_id, "acceptance-v1")
    worker = SummarizerWorker(
        duckdb_path=path,
        api_key="test-only",
        _llm_call=lambda *a, **kw: {
            "summary_md": "acceptance harness summary",
            "next_steps_md": "Continue",
            "open_questions": [],
        },
    )
    assert worker.drain_once() == 1
    return JobLedger(path).latest(SUMMARIZE_SESSION, session_id)


@pytest.fixture
def analytical_observations(monkeypatch):
    """Observe actual query replies AND failed-child RSS samples without replacing execution."""
    from drover.server.lake import query_process, read_models, serving
    from drover.server.process_memory import process_rss

    observations = {"queries": [], "rss": [], "hub_rss": [], "hub_rss_errors": []}
    real_query, real_rss = query_process.query, query_process._rss

    def query(*args, **kwargs):
        entry = {"sql": args[1]}
        observations["queries"].append(entry)
        try:
            result = real_query(*args, **kwargs)
            entry["bytes"] = len(json.dumps(result).encode())
            entry["peak_rss_bytes"] = result["peak_rss_bytes"]
            return result
        except Exception as exc:
            entry["error"] = str(exc)
            raise

    def rss(pid):
        sample = real_rss(pid)
        observations["rss"].append(sample)
        return sample

    monkeypatch.setattr(query_process, "_rss", rss)
    monkeypatch.setattr(query_process, "query", query)
    monkeypatch.setattr(serving, "query", query)
    monkeypatch.setattr(read_models, "query", query)
    # The real HTTP hub and summarizer run in this pytest process. Sample its
    # RSS separately from disposable analytical children, throughout the
    # workload (including intervals without a child). Fixture setup/rebuild
    # happens before this monitor starts; retained coordinator memory is still
    # included, making this a conservative serving-process budget gate.
    stop = threading.Event()

    def sample_hub():
        try:
            observations["hub_rss"].append(process_rss(os.getpid()))
        except (OSError, ValueError) as exc:
            observations["hub_rss_errors"].append(str(exc))

    def monitor_hub():
        while not stop.wait(0.01):
            sample_hub()

    sample_hub()
    monitor = threading.Thread(target=monitor_hub, daemon=True)
    monitor.start()
    try:
        yield observations
    finally:
        stop.set()
        monitor.join(timeout=1)
        sample_hub()


def _assert_hub_rss_budget(observations):
    """A cutover gate, not a claim about the untouched production process."""
    samples = observations["hub_rss"]
    assert samples and not observations["hub_rss_errors"], observations
    peak = max(samples)
    budget = 4 * 1024**3
    print(f"hub_peak_rss_bytes={peak} hub_budget_bytes={budget}")
    assert peak <= budget, (
        f"Hub RSS {peak} bytes exceeds 4 GiB budget ({budget} bytes); "
        "block cutover and investigate retained hub allocations. "
        "Disposable query children are measured separately."
    )


@pytest.mark.acceptance_scale
def test_a3_summarize_40k_session_scale(
    hub_scale_lake, prod_shaped, analytical_observations, caplog
):
    with caplog.at_level("WARNING"):
        job = _summarize(prod_shaped, BIG_SESSION_ID)
    assert job.status == "succeeded", f"Summary job {job.status}: {job.last_error}"
    queries = analytical_observations["queries"]
    assert queries, "Summarizer performed no analytical queries"
    assert all(
        "analytics_byte_limit_exceeded" not in q.get("error", "") for q in queries
    ), queries
    assert all(q["bytes"] <= 1024**2 for q in queries), queries
    warnings = [
        r.message
        for r in caplog.records
        if "Truncated session" in r.message and BIG_SESSION_ID in r.message
    ]
    assert len(warnings) == int(40_000 > MAX_RAW_EVENTS_PER_SESSION), warnings
    if warnings:
        assert str(MAX_RAW_EVENTS_PER_SESSION) in warnings[0]
    # Also check a session below the cap, through the same worker.
    caplog.clear()
    with caplog.at_level("WARNING"):
        short_job = _summarize(prod_shaped, "sess-0000000")
    assert short_job.status == "succeeded", short_job
    assert not any("Truncated session" in r.message for r in caplog.records)
    assert all(
        q.get("bytes", 0) <= 1024**2 and "error" not in q for q in queries
    ), queries
    print(f"A3 child_peak_rss_bytes={max(analytical_observations['rss'])}")
    _assert_hub_rss_budget(analytical_observations)


@pytest.mark.acceptance_scale
def test_a4_cockpit_overview_5m_lake_scale(
    hub_scale_lake, prod_shaped, hub_http, analytical_observations
):
    latencies, activities = [], []
    for _ in range(5):
        started = time.perf_counter()
        response = requests.get(hub_http + "/cockpit/overview?days=30", timeout=15)
        latencies.append(time.perf_counter() - started)
        assert response.status_code == 200, response.text
        # The current HTTP API calls lake_activity 'activity' and available 'ok'.
        activities.append(response.json()["activity"])
    assert all(
        a["status"] == "ok" for a in activities
    ), f"Lake activity unavailable: {activities}"
    rss = analytical_observations["rss"]
    assert rss, "No analytical child RSS samples observed"
    assert max(rss) < 1024**3, f"Analytical child peak RSS {max(rss)} exceeds 1 GiB"
    p95 = sorted(latencies)[-1]  # nearest-rank p95 for five complete HTTP requests
    print(f"A4 child_peak_rss_bytes={max(rss)} http_p95_seconds={p95:.9f}")
    assert p95 < 2, f"Cockpit overview p95 {p95:.3f}s exceeds 2 s"
    _assert_hub_rss_budget(analytical_observations)


# S2: startup preserves released versions 1–11 and applies only migration 12.
def test_a5_hub_startup_on_prod_shaped_store(prod_shaped, served_small_lake):
    from drover.server.lake.exporter import LakeOutboxExporter

    store = postgres_control_store(prod_shaped)
    with store.connection() as con:
        before = con.execute(
            "SELECT version, applied_at FROM control_schema_migrations ORDER BY version"
        ).fetchall()
    assert [r[0] for r in before] == list(range(1, 12))
    bootstrap_control_plane_store(prod_shaped)
    with store.connection() as con:
        after = con.execute(
            "SELECT version, applied_at FROM control_schema_migrations ORDER BY version"
        ).fetchall()
    assert after[:11] == before
    assert [r[0] for r in after] == list(range(1, 13))
    bootstrap_control_plane_store(prod_shaped)
    with store.connection() as con:
        assert (
            con.execute(
                "SELECT version, applied_at FROM control_schema_migrations ORDER BY version"
            ).fetchall()
            == after
        )
    with LakeOutboxExporter(
        control_path=prod_shaped, spec=served_small_lake.spec
    ) as exporter:
        with store.connection() as con:
            assert exporter._pending(con) is None


# S2: rollback replays the retained control outbox into legacy parquet.
def test_a6_switch_ingest_rollback_outbox_replay(
    hub_small_lake, prod_shaped, hub_http, tmp_path
):
    from drover.server.ingest import ingest_file

    legacy = tmp_path / "legacy"
    bootstrap(parquet_dir=legacy, duckdb_path=prod_shaped)
    events = [_event(i, f"rollback-{i // 10}") for i in range(100)]
    source = tmp_path / "incoming" / "batch.jsonl"
    _write_events(source, events)
    stats = ingest_file(source, parquet_dir=legacy, duckdb_path=prod_shaped)
    assert stats.errors == 0, stats
    # Contract for S2 CLI: replay the durable control outbox, never a source JSONL file.
    config = tmp_path / "config.toml"
    config.write_text(
        f'[paths]\nduckdb_path = "{prod_shaped}"\nparquet_dir = "{legacy}"\nincoming_dir = "{tmp_path / "incoming"}"\n[control_store]\nbackend = "postgres"\ndsn_env = "DROVER_TEST_POSTGRES_DSN"\nschema = "drover_control"\n'
    )
    from drover.config import load_config

    loaded = load_config(config)
    assert loaded.duckdb_path == prod_shaped and loaded.parquet_dir == legacy
    result = CliRunner().invoke(
        main, ["--config", str(config), "outbox", "replay", "--sink", "legacy"]
    )
    assert result.exit_code == 0, f"outbox replay command failed: {result.output}"
    configure_analytics(prod_shaped, AnalyticsConfig())
    with duckdb.connect() as con:
        ids = {
            r[0]
            for r in con.execute(
                "SELECT id FROM read_parquet(?, hive_partitioning=true) WHERE id LIKE 'acceptance-event-%'",
                [str(legacy / "agent_events/**/*.parquet")],
            ).fetchall()
        }
    assert ids == {e["id"] for e in events}
    response = requests.get(hub_http + "/healthz", timeout=10)
    assert response.status_code == 200 and response.text.strip() == "ok\nanalytical=ok"


# fails today: a mapped historical legacy-only session resolves unavailable instead of archived.
def test_a7_replay_pre_import_watermark_archived_lake_import(
    hub_recent_lake, prod_shaped, small_lake, tmp_path
):
    session_id = "sess-0000000"  # first-day session exists in legacy, before recent lake watermark
    with duckdb.connect() as con:
        count, started_at = con.execute(
            "SELECT count(*), min(timestamp) FROM read_parquet(?, hive_partitioning=true) WHERE session_id=?",
            [str(small_lake / "agent_events/**/*.parquet"), session_id],
        ).fetchone()
    assert (
        count > 0 and started_at < BASE_ANCHOR_DATE
    ), "Historical legacy session missing"
    _register(
        prod_shaped, session_id, native_session_id=session_id, started_at=started_at
    )
    import os
    os.environ["DROVER_LAKE_EXTENSION_DIR"] = str(hub_recent_lake.spec.extension_dir)
    os.environ["DROVER_LAKE_ENGINE_SHA256"] = hub_recent_lake.spec.engine_sha256
    
    replay = drover_session_replay(duckdb_path=prod_shaped, session_id=session_id)
    assert (
        replay["status"] == "archived"
    ), f"Legacy-only session expected archived, got {replay}"
    
    result = CliRunner().invoke(
        main,
        [
            "lake",
            "import",
            "--session",
            session_id,
            "--data-root",
            str(hub_recent_lake.spec.data_root),
            "--catalog-dsn-env",
            hub_recent_lake.spec.catalog_dsn_env,
            "--legacy-root",
            str(small_lake),
        ],
    )
    assert result.exit_code == 0, result.output
    
    # Run again to test idempotency
    result2 = CliRunner().invoke(
        main,
        [
            "lake",
            "import",
            "--session",
            session_id,
            "--data-root",
            str(hub_recent_lake.spec.data_root),
            "--catalog-dsn-env",
            hub_recent_lake.spec.catalog_dsn_env,
            "--legacy-root",
            str(small_lake),
        ],
    )
    assert result2.exit_code == 0, result2.output

    # Check that rows are not duplicated
    with duckdb.connect() as con2:
        con2.execute(f"INSTALL '{hub_recent_lake.spec.extension_dir}/ducklake.duckdb_extension'")
        con2.execute("LOAD ducklake")
        con2.execute(f"ATTACH 'ducklake:postgres:{os.environ[hub_recent_lake.spec.catalog_dsn_env]}' AS lake")
        lake_count = con2.execute("SELECT count(*) FROM lake.agent_events WHERE session_id=?", [session_id]).fetchone()[0]
        assert lake_count == count, f"Idempotency failed: expected {count}, got {lake_count}"
    
    # After lake import, the verification digest changed. Reload it into the hub!
    import hashlib
    from dataclasses import replace
    proof_bytes = (hub_recent_lake.spec.data_root / "verification/serving-proof.json").read_bytes()
    new_digest = hashlib.sha256(proof_bytes).hexdigest()
    hub_recent_lake = replace(hub_recent_lake, config=replace(hub_recent_lake.config, verification_sha256=new_digest))
    from drover.server.lake.serving import configure_analytics
    configure_analytics(prod_shaped, hub_recent_lake.config)

    job = _summarize(prod_shaped, session_id)
    assert job.status == "succeeded", job
    assert (
        drover_session_summary(duckdb_path=prod_shaped, session_id=session_id)["status"]
        == "completed"
    )
