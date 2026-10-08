"""Local, realistic control/native fixtures for the Phase 2 memory contract."""

import importlib.util
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.db import control_plane_connection
from drover.server.embeddings.client import EmbeddingBackendConfig, OllamaEmbedder
from drover.server.ledger import SUMMARIZE_SESSION, JobLedger
from drover.server.mcp import tools
from drover.server.memory_identity import apply_memory_links, read_memory_sessions
from drover.server.memory_identity import refresh_memory_projection as project_memory
from drover.server.memory_store import EMBEDDING_DIM, MemoryRepository
from drover.server.summarizer.jobs import source_version_for_session
from drover.server.summarizer.worker import SummarizerWorker

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/memory/control_sessions.json").read_text()
)


@pytest.mark.parametrize(
    ("watermark", "earliest_event", "status"),
    [
        ("2026-09-01", "2026-07-01", "archived"),
        (None, "2026-09-01", "archived"),
        (None, None, "unavailable"),
    ],
)
def test_archived_identity_uses_import_or_rebuild_boundary(
    monkeypatch, tmp_path, watermark, earliest_event, status
):
    from types import SimpleNamespace

    from drover.server.lake import serving
    from drover.server.memory_identity import resolve_session

    monkeypatch.setattr(
        serving, "selected_config", lambda _: SimpleNamespace(backend="ducklake")
    )
    monkeypatch.setattr("drover.server.ledger.memory_store_available", lambda _: False)
    with duckdb.connect() as con:
        con.execute("ATTACH ':memory:' AS lake")
        con.execute("CREATE TABLE lake.agent_events(timestamp TIMESTAMPTZ)")
        if earliest_event:
            con.execute("INSERT INTO lake.agent_events VALUES (?)", [earliest_event])
        if watermark:
            con.execute("CREATE TABLE lake.import_watermark(partition_date VARCHAR)")
            con.execute("INSERT INTO lake.import_watermark VALUES (?)", [watermark])
        con.execute(
            "CREATE TABLE memory_session_identity(harness_session_id VARCHAR, "
            "native_session_id VARCHAR, summary_session_id VARCHAR, started_at TIMESTAMPTZ)"
        )
        con.execute(
            "INSERT INTO memory_session_identity VALUES ('old', 'native', NULL, '2026-08-01')"
        )
        con.execute("CREATE TABLE control_memory_events(session_id VARCHAR)")
        result = resolve_session(con, "old", store_path=tmp_path / "control.duckdb")
        assert result["status"] == status


def refresh_memory_projection(analytics, control, path):
    apply_memory_links(
        control,
        project_memory(analytics, read_memory_sessions(control), store_path=path),
    )


@pytest.fixture
def stores(tmp_path, pg_control_path):
    path = pg_control_path
    parquet = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet, duckdb_path=path)
    return path, parquet


def seed_control(con, harness="codex", sid="harness-fixture", native="native-fixture"):
    con.execute(
        """INSERT INTO harness_sessions
        (session_id,host_id,harness,command,status,repo_owner,repo_name,branch,native_session_id)
        VALUES (?, 'fixture-host', ?, 'agent', 'exited', 'arniesaha', 'drover', 'main', ?)""",
        [sid, harness, native],
    )


def exported_events(analytics, sid="harness-fixture", metadata_only=False):
    turns = [] if metadata_only else FIXTURE["turns"]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    rows = []
    for index, turn in enumerate(
        turns
        + [
            {"event_type": "status", "payload": {"status": "idle", "seq": n}}
            for n in range(FIXTURE["metadata_tail_count"])
        ]
    ):
        rows.append(
            {
                "event_id": f"{sid}-{index}",
                "session_id": sid,
                "created_at": now + timedelta(seconds=index),
                "event_type": turn["event_type"],
                "normalized_type": turn["event_type"],
                "normalized_source": "structured",
                "payload_json": json.dumps(turn["payload"]),
            }
        )
    import pyarrow as pa

    analytics.register("fixture_export", pa.Table.from_pylist(rows))
    analytics.execute(
        "CREATE TABLE harness_exported_events AS SELECT * FROM fixture_export"
    )


@pytest.mark.parametrize("harness", FIXTURE["harnesses"])
def test_control_stream_summarizes_every_harness_with_metadata_tail(stores, harness):
    path, _ = stores
    with control_plane_connection(path) as control:
        seed_control(control, harness)
        analytics = duckdb.connect(str(path))
        exported_events(analytics)
        refresh_memory_projection(analytics, control, path)
        version = source_version_for_session(analytics, "harness-fixture")
        assert analytics.execute(
            "SELECT count(*), min(source) FROM agent_events"
        ).fetchone() == (40, "control")
        analytics.execute(
            "INSERT INTO harness_exported_events SELECT 'later', session_id, created_at + INTERVAL '1 day', 'status', 'status', normalized_source, payload_json FROM harness_exported_events LIMIT 1"
        )
        refresh_memory_projection(analytics, control, path)
        assert source_version_for_session(analytics, "harness-fixture") == version
        analytics.close()
    prompts = []

    def llm(prompt, **kwargs):
        prompts.append(prompt)
        return {"summary_md": "Implemented memory integrity.", "next_steps_md": ""}

    worker = SummarizerWorker(duckdb_path=path, _llm_call=llm, api_key="fixture")
    assert worker.drain_once() == 1
    assert FIXTURE["final_output"] in prompts[0]
    with control_plane_connection(path) as control:
        analytics = duckdb.connect(str(path))
        refresh_memory_projection(analytics, control, path)
        assert (
            control.execute(
                "SELECT summary_session_id FROM harness_sessions"
            ).fetchone()[0]
            == "harness-fixture"
        )
        analytics.close()
    for identifier in ["harness-fixture", "native-fixture"]:
        assert (
            tools.drover_session_close(duckdb_path=path, session_id=identifier)[
                "session_id"
            ]
            == "harness-fixture"
        )
        summary = tools.drover_session_summary(duckdb_path=path, session_id=identifier)
        assert summary["last_assistant"] == FIXTURE["final_output"]
        for ref in ["abc123def456789", "#479", "#468", "#469", "#470"]:
            assert ref in summary["summary_md"]
        assert summary["files_touched"] == [
            "src/drover/server/memory_identity.py",
            "tests/test_memory_integrity.py",
        ]
        assert summary["tools_used"] == {"Edit": 1, "shell": 1}
        assert (
            len(
                tools.drover_session_replay(duckdb_path=path, session_id=identifier)[
                    "events"
                ]
            )
            == 5
        )
        assert tools.drover_handoff(duckdb_path=path, session_id=identifier)[
            "summaries"
        ]
        assert tools.drover_search(
            duckdb_path=path, session_id=identifier, query="#479"
        )["results"]
        assert tools.drover_files_touched(duckdb_path=path, session_id=identifier)[
            "files"
        ]
        assert (
            tools.drover_task_status(duckdb_path=path, session_id=identifier)[
                "session_count"
            ]
            == 1
        )
        assert (
            tools.drover_recall(duckdb_path=path, session_id=identifier, query="#479")[
                "mode"
            ]
            == "keyword"
        )
    spec = importlib.util.spec_from_file_location(
        "memory_acceptance", Path(__file__).parents[1] / "scripts/memory_acceptance.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    con = duckdb.connect(str(path), read_only=True)
    report = module.audit_session(con, "harness-fixture", store_path=path)
    assert report["canonical_event_count"] == 41
    assert (
        report["mapping_present"]
        and report["summary_contains_final_references"]
        and report["files_touched_non_empty"]
    )
    con.close()
    remote_report = tools.drover_memory_acceptance(
        duckdb_path=path, harness_ids=["harness-fixture"]
    )["sessions"][0]
    assert remote_report == {
        **report,
        "store": "hub",
        "store_authoritative": True,
        "host": socket.gethostname(),
        "data_watermark": {"timestamp": None, "basis": "unknown"},
    }


def test_metadata_only_session_is_insufficient(stores):
    path, _ = stores
    with control_plane_connection(path) as control:
        seed_control(control, native=None)
        con = duckdb.connect(str(path))
        exported_events(con, metadata_only=True)
        refresh_memory_projection(con, control, path)
        con.close()
    llm = Mock()
    SummarizerWorker(duckdb_path=path, _llm_call=llm, api_key="fixture").drain_once()
    llm.assert_not_called()
    job = JobLedger(path).latest(SUMMARIZE_SESSION, "harness-fixture")
    assert job.status == "quarantined" and job.error_category == "no_events"
    assert MemoryRepository(path).summary("harness-fixture") is None
    assert JobLedger(path).stats()["embed_session"]["pending"] == 0
    assert (
        tools.drover_session_summary(duckdb_path=path, session_id="harness-fixture")[
            "status"
        ]
        == "insufficient_input"
    )


@pytest.mark.parametrize(
    "tool,kwargs",
    [
        (tools.drover_session_summary, {}),
        (tools.drover_session_replay, {}),
        (tools.drover_handoff, {}),
        (tools.drover_search, {"query": "memory"}),
        (tools.drover_files_touched, {}),
        (tools.drover_task_status, {}),
        (tools.drover_recall, {"query_embedding": [1.0] * EMBEDDING_DIM}),
        (tools.drover_active_handoff, {}),
    ],
)
def test_mcp_unknown_and_unmapped(stores, tool, kwargs):
    path, _ = stores
    assert (
        tool(duckdb_path=path, session_id="unknown-id", **kwargs)["status"] == "unknown"
    )
    with control_plane_connection(path) as control:
        seed_control(control, native=None)
        con = duckdb.connect(str(path))
        refresh_memory_projection(con, control, path)
        con.close()
    assert (
        tool(duckdb_path=path, session_id="harness-fixture", **kwargs)["status"]
        == "unmapped"
    )


def test_ollama_probe_gates_explicit_launchd(monkeypatch):
    import drover.server.embeddings.client as client

    kick = Mock()
    wait = Mock()
    monkeypatch.setattr(client, "_launchctl_kickstart", kick)
    monkeypatch.setattr(client, "wait_for_ollama", wait)
    monkeypatch.setattr(client, "_ollama_healthy", lambda url: True)
    OllamaEmbedder(ollama_url="http://fixture", launchd_label="explicit").ensure_ready()
    kick.assert_not_called()
    monkeypatch.setattr(client, "_ollama_healthy", lambda url: False)
    OllamaEmbedder(ollama_url="http://fixture").ensure_ready()
    kick.assert_not_called()
    OllamaEmbedder(ollama_url="http://fixture", launchd_label="explicit").ensure_ready()
    kick.assert_called_once()
    wait.assert_called_once()
    assert (
        EmbeddingBackendConfig.from_runtime(
            mac_ollama_url="http://fixture"
        ).mac_ollama_launchd_label
        is None
    )


def test_native_history_attaches_without_duplicate_memory_and_backfills(stores):
    from drover.server.ingest import ingest_file

    path, parquet = stores
    incoming = path.parent / "native.jsonl"

    def native_event(sid):
        return {
            "id": sid + "-event",
            "session_id": sid,
            "timestamp": "2026-10-01T12:00:00Z",
            "agent_id": "claude-code",
            "event_type": "assistant_message",
            "message": {"role": "assistant", "content": "Native final output"},
            "tool_calls": [
                {"tool_name": "Write", "input": {"file_path": "external.py"}}
            ],
        }

    incoming.write_text(
        "\n".join(
            json.dumps(native_event(sid))
            for sid in ["native-fixture", "external-native"]
        )
        + "\n"
    )
    assert ingest_file(incoming, parquet_dir=parquet, duckdb_path=path).inserted == 2
    from drover.server.control_exporter import ControlOutboxExporter

    ControlOutboxExporter(
        control_path=path, analytical_path=path, parquet_dir=parquet, batch_size=1
    ).run_once()
    with duckdb.connect(str(path)) as analytics:
        analytics.execute("DROP VIEW harness_exported_events")
    with control_plane_connection(path) as control:
        seed_control(control, native=None)
        con = duckdb.connect(str(path))
        exported_events(con)
        # A first native event often reports its ID inside the structured envelope.
        con.execute(
            "UPDATE harness_exported_events SET payload_json=? WHERE event_id='harness-fixture-0'",
            [
                json.dumps(
                    {
                        "text": "implement",
                        "payload": {"native_session_id": "native-fixture"},
                    }
                )
            ],
        )
        refresh_memory_projection(con, control, path)
        assert (
            control.execute(
                "SELECT native_session_id FROM harness_sessions WHERE session_id='harness-fixture'"
            ).fetchone()[0]
            == "native-fixture"
        )
        assert con.execute(
            "SELECT DISTINCT session_id FROM agent_events ORDER BY session_id"
        ).fetchall() == [("external-native",), ("harness-fixture",)]
        assert (
            con.execute(
                "SELECT count(*) FROM agent_events_for_date('2026-10-01') WHERE session_id='native-fixture'"
            ).fetchone()[0]
            == 0
        )
        assert (
            con.execute(
                "SELECT source FROM agent_events WHERE session_id='external-native'"
            ).fetchone()[0]
            == "native"
        )
        con.close()
    # Physical collector dedupe must still see excluded native rows on retry.
    assert ingest_file(incoming, parquet_dir=parquet, duckdb_path=path).inserted == 0
    assert (
        tools.drover_session_replay(duckdb_path=path, session_id="native-fixture")[
            "session_id"
        ]
        == "harness-fixture"
    )
    assert tools.drover_files_touched(duckdb_path=path, session_id="external-native")[
        "files"
    ] == ["external.py"]


def test_projection_repairs_enqueue_and_control_link_after_failure(stores, monkeypatch):
    import drover.server.summarizer.jobs as jobs

    path, _ = stores
    with control_plane_connection(path) as control:
        seed_control(control, native=None)
        con = duckdb.connect(str(path))
        exported_events(con)
        con.execute(
            "UPDATE harness_exported_events SET payload_json=? WHERE event_id='harness-fixture-0'",
            [json.dumps({"text": "implement", "native_session_id": "native-fixture"})],
        )
        original = jobs.enqueue_summary_generation
        monkeypatch.setattr(
            jobs,
            "enqueue_summary_generation",
            Mock(side_effect=RuntimeError("fixture enqueue outage")),
        )
        with pytest.raises(RuntimeError):
            refresh_memory_projection(con, control, path)
        assert (
            con.execute("SELECT count(*) FROM memory_projection_pending").fetchone()[0]
            == 1
        )
        monkeypatch.setattr(jobs, "enqueue_summary_generation", original)
        refresh_memory_projection(con, control, path)
        assert (
            con.execute("SELECT count(*) FROM memory_projection_pending").fetchone()[0]
            == 0
        )
        assert (
            JobLedger(path).latest(SUMMARIZE_SESSION, "harness-fixture").status
            == "pending"
        )
        assert (
            control.execute(
                "SELECT native_session_id FROM harness_sessions WHERE session_id='harness-fixture'"
            ).fetchone()[0]
            == "native-fixture"
        )
        con.close()


def test_forced_final_assistant_survives_later_tool_turns(stores):
    from drover.server.summarizer.derive import select_substantive_window
    from drover.server.summarizer.worker import _session_agent_events_ctes

    path, _ = stores
    with control_plane_connection(path) as control:
        seed_control(control)
        con = duckdb.connect(str(path))
        exported_events(con)
        for n in range(40):
            con.execute(
                "INSERT INTO harness_exported_events SELECT ?, session_id, created_at+INTERVAL '1 day', 'tool_result', 'tool_result', normalized_source, ? FROM harness_exported_events LIMIT 1",
                [f"tool-{n}", json.dumps({"text": "test completed"})],
            )
        refresh_memory_projection(con, control, path)
        window = select_substantive_window(
            con, _session_agent_events_ctes(), "harness-fixture"
        )
        assert len(window) <= 31
        assert any(ev["content"] == FIXTURE["final_output"] for ev in window)
        con.close()
    SummarizerWorker(
        duckdb_path=path,
        _llm_call=lambda *args, **kw: {"summary_md": "done", "next_steps_md": ""},
        api_key="fixture",
    ).drain_once()
    assert (
        tools.drover_session_summary(duckdb_path=path, session_id="harness-fixture")[
            "last_assistant"
        ]
        == FIXTURE["final_output"]
    )


def test_exporter_rebuilds_canonical_memory_from_acknowledged_manifest(
    stores, monkeypatch
):
    import hashlib

    import pyarrow as pa
    import pyarrow.parquet as pq

    from drover.server import control_exporter, control_outbox

    path, parquet = stores
    with control_plane_connection(path) as control:
        seed_control(control)
    con = duckdb.connect(str(path))
    exported_events(con)
    cur = con.execute("SELECT * FROM harness_exported_events")
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, row)) for row in cur.fetchall()]
    con.execute("DROP TABLE harness_exported_events")
    con.close()
    archive = parquet / "fixture-batch.parquet"
    pq.write_table(pa.Table.from_pylist(rows), archive)
    receipt = control_outbox.PublishedBatch(
        "fixture-batch",
        str(archive),
        hashlib.sha256(archive.read_bytes()).hexdigest(),
        len(rows),
        datetime.now(timezone.utc),
    )
    monkeypatch.setattr(control_outbox, "published_batches", lambda con: [receipt])
    monkeypatch.setattr(
        control_exporter, "is_postgres_control_store", lambda path: True
    )
    monkeypatch.setattr(
        control_exporter, "outbox_status", lambda con: {"pending": 0, "claimed": 0}
    )
    exporter = control_exporter.ControlOutboxExporter(
        control_path=path, analytical_path=path, parquet_dir=parquet
    )
    monkeypatch.setattr(
        exporter, "_rebuild_relation_and_acknowledge", lambda **kwargs: 0
    )
    monkeypatch.setattr(
        exporter,
        "_prune_verified_payloads",
        lambda: {"pruned": 0, "verification_failed": 0},
    )
    exporter.run_once()
    exporter.run_once()
    con = duckdb.connect(str(path))
    assert con.execute("SELECT count(*) FROM control_memory_events").fetchone()[0] == 40
    assert JobLedger(path).stats()[SUMMARIZE_SESSION]["pending"] == 1
    con.close()


def test_missing_read_store_is_explicitly_unavailable(tmp_path):
    # Directory is not a database and cannot be silently treated as an unknown ID.
    assert (
        tools.drover_session_summary(duckdb_path=tmp_path, session_id="harness-id")[
            "status"
        ]
        == "unavailable"
    )


def test_acceptance_cli_never_creates_a_missing_store(tmp_path, monkeypatch, capsys):
    import runpy
    import sys

    missing = tmp_path / "missing.duckdb"
    monkeypatch.setattr(
        sys,
        "argv",
        ["memory_acceptance.py", "--duckdb-path", str(missing), "harness-missing"],
    )
    runpy.run_path(
        str(Path(__file__).parents[1] / "scripts/memory_acceptance.py"),
        run_name="__main__",
    )
    assert not missing.exists()
    assert json.loads(capsys.readouterr().out)[0]["status"] == "unavailable"


def test_legacy_file_changes_are_substantive_and_url_refs_are_preserved():
    from drover.server.memory_identity import project_control_event
    from drover.server.summarizer.derive import compute_files_touched, final_references

    event = {
        "event_id": "change",
        "session_id": "harness-fixture",
        "event_type": "status",
        "normalized_type": "status",
        "created_at": datetime.now(timezone.utc),
        "payload_json": json.dumps(
            {
                "text": "file_change",
                "payload": {
                    "item": {
                        "type": "file_change",
                        "changes": [{"path": "src/foo.py", "kind": "update"}],
                    }
                },
            }
        ),
    }
    row = project_control_event(event, {"harness": "codex"})
    assert row["event_type"] == "file_change" and row["role"] == "tool"
    assert compute_files_touched([row]) == ["src/foo.py"]
    assert final_references(
        "Committed abc123def456789; https://github.com/arniesaha/drover/issues/479"
    ) == ["abc123def456789", "#479"]


def test_acceptance_cli_uses_only_live_audit_read(monkeypatch, capsys):
    import runpy
    import sys

    from drover.server.mcp import client

    call = Mock(
        return_value={
            "structuredContent": {
                "sessions": [{"harness_id": "harness-fixture", "status": "ok"}]
            }
        }
    )
    monkeypatch.setattr(client, "call_tool", call)
    monkeypatch.setattr(
        sys,
        "argv",
        ["memory_acceptance.py", "--mcp-url", "http://fixture/mcp", "harness-fixture"],
    )
    runpy.run_path(
        str(Path(__file__).parents[1] / "scripts/memory_acceptance.py"),
        run_name="__main__",
    )
    call.assert_called_once_with(
        "http://fixture/mcp",
        "drover_memory_acceptance",
        {"harness_ids": ["harness-fixture"]},
    )
    assert json.loads(capsys.readouterr().out)[0]["status"] == "ok"
