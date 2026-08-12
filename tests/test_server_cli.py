"""Tests for src/drover/server/__main__.py CLI."""

import json
import subprocess
import sys
import textwrap
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from drover.config import default_config
from drover.schema import bootstrap
from drover.server import __main__ as server_main
from drover.server.__main__ import (
    _bootstrap_harnessd_schema,
    _build_redis_job_streams,
    _seed_redis_job_streams,
    _summarizer_backend_available,
    main,
)
from drover.server.db import control_plane_path
from drover.server.harness import cli as harness_cli
from drover.server.harness.recap_jobs import enqueue_live_recap
from drover.server.ledger import ArtifactSpec, Ledger
from drover.server.summarizer.backends import SummarizerBackendConfig
from drover.server.wol import GpuRig

_SPAN_SCHEMA = pa.schema(
    [
        ("trace_id", pa.string()),
        ("span_id", pa.string()),
        ("parent_span_id", pa.string()),
        ("name", pa.string()),
        ("service_name", pa.string()),
        ("start_time", pa.timestamp("us", tz="UTC")),
        ("end_time", pa.timestamp("us", tz="UTC")),
        ("duration_ms", pa.float64()),
        ("session_id", pa.string()),
        ("task_id", pa.string()),
        ("agent_id", pa.string()),
        ("cost_usd", pa.float64()),
        ("dedup_key", pa.string()),
    ]
)


def _make_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(textwrap.dedent(f"""\
        [paths]
        incoming_dir = "{tmp_path / 'incoming'}"
        parquet_dir  = "{tmp_path / 'parquet'}"
        duckdb_path  = "{tmp_path / 'drover.duckdb'}"
        processed_retention_days = 7

        [server]
        otlp_grpc_port = 14317
        mcp_http_port  = 17077

        [agent]
        agent_id     = "test"
        principal_id = "test"
    """))
    return cfg


def seeded_server_db(tmp_path: Path) -> Path:
    """Create the bootstrapped server database used by Redis seeding tests."""
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    return db


class RecordingJobStream:
    """Capture the payloads mirrored into a startup job stream."""

    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, fields: dict[str, str]) -> str:
        self.items.append(fields)
        return f"{len(self.items)}-0"


def _write_spans(parquet_dir: Path, rows: list[dict]) -> None:
    by_date: dict[str, list[dict]] = {}
    for row in rows:
        by_date.setdefault(row["start_time"].date().isoformat(), []).append(row)
    for date, date_rows in by_date.items():
        out = parquet_dir / "spans" / f"date={date}"
        out.mkdir(parents=True, exist_ok=True)
        cols = {
            field.name: pa.array(
                [row.get(field.name) for row in date_rows], type=field.type
            )
            for field in _SPAN_SCHEMA
        }
        pq.write_table(pa.table(cols, schema=_SPAN_SCHEMA), out / "part.parquet")


def _insert_legacy_sequence_events(db: Path, *, mixed: bool = False) -> None:
    # `harness_events` lives in the control-plane store since #95.
    with duckdb.connect(str(control_plane_path(db))) as con:
        con.executemany(
            "INSERT INTO harness_events "
            "(event_id, session_id, event_type, payload_json, created_at, seq) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "legacy-event-2",
                    "private-legacy-session",
                    "assistant_output",
                    '{"message":"must-not-leak"}',
                    "2026-08-06 10:00:00",
                    None,
                ),
                (
                    "legacy-event-1",
                    "private-legacy-session",
                    "user_input",
                    '{"token":"must-not-leak"}',
                    "2026-08-06 10:00:00",
                    None,
                ),
            ],
        )
        if mixed:
            con.executemany(
                "INSERT INTO harness_events "
                "(event_id, session_id, event_type, payload_json, created_at, seq) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        "mixed-event-1",
                        "private-mixed-session",
                        "user_input",
                        "{}",
                        "2026-08-06 11:00:00",
                        1,
                    ),
                    (
                        "mixed-event-2",
                        "private-mixed-session",
                        "assistant_output",
                        "{}",
                        "2026-08-06 11:01:00",
                        None,
                    ),
                ],
            )


def test_harness_migrate_sequences_dry_run_reports_without_mutating(tmp_path):
    db = tmp_path / "override.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    _insert_legacy_sequence_events(db)

    result = CliRunner().invoke(main, ["harness", "migrate-sequences", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "all_null_sessions": 1,
        "applied": False,
        "database": str(db),
        "mixed_sessions": 0,
        "null_event_count": 2,
    }
    with duckdb.connect(str(control_plane_path(db)), read_only=True) as con:
        assert con.execute(
            "SELECT count(*) FROM harness_events WHERE seq IS NULL"
        ).fetchone() == (2,)


def test_harness_migrate_sequences_apply_reports_exact_counts(tmp_path):
    db = tmp_path / "override.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    _insert_legacy_sequence_events(db)

    result = CliRunner().invoke(
        main, ["harness", "migrate-sequences", "--db", str(db), "--apply"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "all_null_sessions": 1,
        "applied": True,
        "database": str(db),
        "mixed_sessions": 0,
        "null_event_count": 2,
    }
    with duckdb.connect(str(control_plane_path(db)), read_only=True) as con:
        assert con.execute(
            "SELECT event_id, seq FROM harness_events "
            "WHERE session_id = 'private-legacy-session' ORDER BY seq"
        ).fetchall() == [("legacy-event-1", 1), ("legacy-event-2", 2)]


def test_harness_audit_sequences_mixed_session_is_safe_nonzero_json(tmp_path):
    db = tmp_path / "override.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    _insert_legacy_sequence_events(db, mixed=True)

    result = CliRunner().invoke(
        main, ["harness", "audit-sequences", "--db", str(db), "--json"]
    )

    assert result.exit_code != 0
    assert json.loads(result.output) == {
        "all_null_sessions": 1,
        "applied": False,
        "database": str(db),
        "mixed_sessions": 1,
        "null_event_count": 3,
    }
    assert "private-legacy-session" not in result.output
    assert "private-mixed-session" not in result.output
    assert "must-not-leak" not in result.output
    with duckdb.connect(str(control_plane_path(db)), read_only=True) as con:
        assert con.execute(
            "SELECT count(*) FROM harness_events WHERE seq IS NULL"
        ).fetchone() == (3,)


def test_summarizer_backend_available_requires_api_or_claude_code(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "DROVER_CLAUDE_CREDENTIALS_PATH", str(tmp_path / "no-such-creds.json")
    )
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DROVER_LOCAL_OLLAMA_URL", raising=False)
    monkeypatch.delenv("NEXUS_LOCAL_OLLAMA_URL", raising=False)

    with patch(
        "drover.server.harness.structured.claude.resolve_binary",
        return_value="/usr/local/bin/claude",
    ):
        assert _summarizer_backend_available(SummarizerBackendConfig())
        assert _summarizer_backend_available(SummarizerBackendConfig(api_key="sk-test"))
        # An Ollama host no longer makes the summarizer runnable on its own.
        assert not _summarizer_backend_available(
            SummarizerBackendConfig(
                backend_policy="cloud",
                gpu_rig=GpuRig(relay_url="http://relay", ollama_url="http://ollama"),
            )
        )

    with patch(
        "drover.server.harness.structured.claude.resolve_binary", return_value=None
    ):
        assert not _summarizer_backend_available(SummarizerBackendConfig())
        assert not _summarizer_backend_available(
            SummarizerBackendConfig.from_runtime(
                local_ollama_url="http://127.0.0.1:11435"
            )
        )
        assert _summarizer_backend_available(
            SummarizerBackendConfig(backend_policy="hybrid", api_key="sk-test")
        )


def test_build_redis_job_streams_includes_live_recap(monkeypatch) -> None:
    """A Redis-enabled server gives live recaps their own consumer stream."""
    captured_suffixes: list[str] = []

    def fake_from_url(_url, config):
        captured_suffixes.append(config.stream.rsplit(":", maxsplit=1)[-1])
        return RecordingJobStream()

    monkeypatch.setattr(
        server_main.RedisJobStream, "from_url", staticmethod(fake_from_url)
    )
    cfg = replace(default_config(), redis_jobs_enabled=True)

    streams = _build_redis_job_streams(cfg)

    assert "live_recap" in streams
    assert "summarize_live_session" in captured_suffixes


def test_seed_redis_streams_publishes_live_recap_source_seq(tmp_path):
    db = seeded_server_db(tmp_path)
    # The recap queue moved to the control-plane store in #95; seeding has to
    # read it where the control plane keeps it.
    with duckdb.connect(str(control_plane_path(db))) as con:
        enqueue_live_recap(con, "s1", 12)
    stream = RecordingJobStream()
    counts = _seed_redis_job_streams(duckdb_path=db, streams={"live_recap": stream})
    assert counts == {"live_recap": 1}
    assert stream.items == [{"session_id": "s1", "source_seq": "12"}]


@pytest.mark.parametrize(
    "summarizer_start_error",
    [False, True],
    ids=["summarizer-starts", "summarizer-start-fails"],
)
def test_run_starts_and_stops_live_recap_worker_with_summarizer_backend(
    tmp_path, monkeypatch, summarizer_start_error
) -> None:
    """The foreground server owns recap worker lifecycle beside summarization."""
    events: list[tuple[str, str]] = []
    worker_configs: dict[str, object] = {}

    class ImmediateStopEvent:
        def set(self) -> None:
            events.append(("event", "set"))

        def wait(self, _timeout=None) -> bool:
            return True

    class RecordingWatcher:
        def __init__(self, **_kwargs) -> None:
            pass

        def start(self) -> None:
            events.append(("watcher", "start"))

        def stop(self) -> None:
            events.append(("watcher", "stop"))

    class RecordingAdvisoryWorker:
        def __init__(self, **_kwargs) -> None:
            pass

        def start(self, **_kwargs) -> None:
            events.append(("advisory", "start"))

        def join(self, **_kwargs) -> None:
            events.append(("advisory", "join"))

    class RecordingContentWorker:
        def start(self, **_kwargs) -> None:
            events.append(("content", "start"))

        def join(self, **_kwargs) -> None:
            events.append(("content", "join"))

    class RecordingSummarizerWorker:
        backend_config = None

        def __init__(self, **kwargs) -> None:
            self.backend_config = kwargs["backend_config"]
            worker_configs["summarizer"] = self.backend_config
            events.append(("summarizer", "constructed"))

        def start(self) -> None:
            events.append(("summarizer", "start"))
            if summarizer_start_error:
                raise RuntimeError("summarizer startup failed")

        def stop(self) -> None:
            events.append(("summarizer", "stop"))

    class RecordingLiveRecapWorker:
        backend_config = None

        def __init__(self, **kwargs) -> None:
            self.backend_config = kwargs["backend_config"]
            worker_configs["live_recap"] = self.backend_config
            events.append(("live_recap", "constructed"))

        def start(self) -> None:
            events.append(("live_recap", "start"))

        def stop(self) -> None:
            events.append(("live_recap", "stop"))

    monkeypatch.setattr(server_main.threading, "Event", ImmediateStopEvent)
    monkeypatch.setattr(server_main, "IncomingWatcher", RecordingWatcher)
    monkeypatch.setattr(server_main, "operational_analyzers", lambda: ())
    monkeypatch.setattr(server_main, "AdvisoryWorker", RecordingAdvisoryWorker)
    monkeypatch.setattr(
        server_main,
        "_create_content_analysis_worker",
        lambda **_kwargs: RecordingContentWorker(),
    )
    monkeypatch.setattr(server_main, "SummarizerWorker", RecordingSummarizerWorker)
    monkeypatch.setattr(server_main, "LiveRecapWorker", RecordingLiveRecapWorker)
    monkeypatch.setattr(server_main, "_summarizer_backend_available", lambda _cfg: True)
    monkeypatch.setattr(server_main.signal, "signal", lambda *_args: None)

    result = CliRunner().invoke(
        main,
        [
            "--config",
            str(_make_config(tmp_path)),
            "run",
            "--no-otlp",
            "--no-mcp",
            "--no-metrics",
            "--no-embeddings",
            "--no-briefs",
        ],
    )

    assert result.exit_code == 0, result.output
    assert ("live_recap", "constructed") in events
    assert ("live_recap", "start") in events
    assert ("live_recap", "stop") in events
    assert worker_configs["live_recap"] is worker_configs["summarizer"]


def test_cli_init_writes_default_config(tmp_path):
    runner = CliRunner()
    target = tmp_path / "myconf.toml"
    res = runner.invoke(main, ["--config", str(target), "init"])
    assert res.exit_code == 0, res.output
    assert target.exists()
    text = target.read_text()
    assert "[paths]" in text
    assert 'principal_id = "unknown"' in text
    assert 'principal_id = "arnab"' not in text
    assert "metrics_http_port = 7080" in text
    assert '-agent"' in text
    assert '-claude"' not in text
    assert "192.168." not in text
    assert "10.10." not in text


def test_run_help_uses_loopback_bind_defaults():
    result = CliRunner().invoke(main, ["run", "--help"])
    assert result.exit_code == 0, result.output
    assert result.output.count("[default: 127.0.0.1]") == 3


def test_cli_init_does_not_overwrite_existing(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    res = runner.invoke(main, ["--config", str(cfg), "init"])
    assert res.exit_code != 0
    assert "already exists" in res.output.lower()


def test_harnessd_schema_bootstrap_tolerates_live_duckdb_lock(monkeypatch, tmp_path):
    cfg = _make_config(tmp_path)
    loaded = server_main.load_config(cfg)

    def locked_bootstrap(**kwargs):
        raise duckdb.IOException("IO Error: Could not set lock: Conflicting lock")

    monkeypatch.setattr(harness_cli, "bootstrap", locked_bootstrap)

    assert _bootstrap_harnessd_schema(loaded) is False


def test_harnessd_schema_bootstrap_reraises_other_duckdb_io(monkeypatch, tmp_path):
    cfg = _make_config(tmp_path)
    loaded = server_main.load_config(cfg)

    def failing_bootstrap(**kwargs):
        raise duckdb.IOException("IO Error: corrupted database")

    monkeypatch.setattr(harness_cli, "bootstrap", failing_bootstrap)

    try:
        _bootstrap_harnessd_schema(loaded)
    except duckdb.IOException as exc:
        assert "corrupted database" in str(exc)
    else:
        raise AssertionError("non-lock DuckDB failures should still fail startup")


def test_cli_status_shows_config_and_counts(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    res = runner.invoke(main, ["--config", str(cfg), "status"])
    assert res.exit_code == 0, res.output
    assert "incoming_dir" in res.output
    assert "tasks" in res.output
    assert "session_summaries" in res.output


def test_cli_status_uses_read_only_connection_when_db_exists(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    writer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import duckdb, sys, time; duckdb.connect(sys.argv[1]); print('ready', flush=True); time.sleep(10)",
            str(duckdb_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert writer.stdout is not None
        assert writer.stdout.readline().strip() == "ready"
        res = runner.invoke(main, ["--config", str(cfg), "status"])
    finally:
        writer.terminate()
        writer.wait(timeout=5)
    assert res.exit_code == 0, res.output
    assert "tasks" in res.output


def test_cli_doctor_uses_read_only_connection_when_db_exists(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    writer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import duckdb, sys, time; duckdb.connect(sys.argv[1]); print('ready', flush=True); time.sleep(10)",
            str(duckdb_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert writer.stdout is not None
        assert writer.stdout.readline().strip() == "ready"
        res = runner.invoke(main, ["--config", str(cfg), "doctor"])
    finally:
        writer.terminate()
        writer.wait(timeout=5)
    assert res.exit_code == 0, res.output
    assert "drover-server doctor" in res.output


def test_cli_ledger_reconcile_dry_run_uses_snapshot_when_db_locked(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, updated_at) "
            "VALUES ('sess-locked', 'running', now())"
        )
    finally:
        con.close()

    writer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import duckdb, sys, time; duckdb.connect(sys.argv[1]); print('ready', flush=True); time.sleep(10)",
            str(duckdb_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert writer.stdout is not None
        assert writer.stdout.readline().strip() == "ready"
        res = runner.invoke(
            main,
            [
                "--config",
                str(cfg),
                "ledger",
                "reconcile",
                "--job-kind",
                "summarize_session",
            ],
        )
    finally:
        writer.terminate()
        writer.wait(timeout=5)
    assert res.exit_code == 0, res.output
    assert "ledger reconcile (dry-run)" in res.output
    assert "serving_running=1" in res.output


def test_cli_ledger_replay_dry_run_uses_snapshot_when_db_locked(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        ledger = Ledger(con)
        job = ledger.open_job(
            job_kind="summarize_session", subject_key="sess-replay"
        ).job
        ledger.lease_job(job.job_id, worker_id="test")
        ledger.succeed_job(
            job.job_id,
            artifact=ArtifactSpec(
                artifact_kind="session_summary", subject_key="sess-replay"
            ),
        )
    finally:
        con.close()

    writer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import duckdb, sys, time; duckdb.connect(sys.argv[1]); print('ready', flush=True); time.sleep(10)",
            str(duckdb_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert writer.stdout is not None
        assert writer.stdout.readline().strip() == "ready"
        res = runner.invoke(
            main,
            [
                "--config",
                str(cfg),
                "ledger",
                "replay",
                "--job-kind",
                "summarize_session",
                "--subject",
                "sess-replay",
            ],
        )
    finally:
        writer.terminate()
        writer.wait(timeout=5)
    assert res.exit_code == 0, res.output
    assert "ledger replay (dry-run) summarize_session/sess-replay" in res.output
    assert "ledger_status=succeeded" in res.output
    assert "eligible=True" in res.output


def test_cli_help_lists_subcommands():
    runner = CliRunner()
    res = runner.invoke(main, ["--help"])
    assert res.exit_code == 0
    for sub in (
        "init",
        "run",
        "status",
        "doctor",
        "compact",
        "session",
        "embeddings",
        "context",
    ):
        assert sub in res.output


def test_cli_export_bundle_requires_selector(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    res = runner.invoke(main, ["--config", str(cfg), "export-bundle"])
    assert res.exit_code != 0
    assert "--session-id" in res.output
    assert "repo-owner" in res.output


def test_cli_export_bundle_task_outputs_yaml(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    db = tmp_path / "drover.duckdb"
    parquet = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet, duckdb_path=db)
    now = datetime.now(timezone.utc)
    con = duckdb.connect(str(db))
    try:
        con.execute(
            """
            INSERT INTO tasks (
              task_id, repo_owner, repo_name, branch, explicit_task_id, principal_id,
              status, title, created_at, last_activity_at, session_count, total_cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "task-42",
                "acme",
                "nexus",
                "main",
                None,
                "agent-a",
                "active",
                "context bundle export test",
                now,
                now,
                1,
                0.0,
            ],
        )
        con.execute(
            """
            INSERT INTO session_summaries (
              session_id, task_id, agent_id, ended_at, summary_md, last_user_prompt,
              last_assistant, next_steps_md, status, generator_model, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "session-export",
                "task-42",
                "agent-a",
                now,
                "Added context bundle export command",
                "Build the context bundle",
                "Done",
                "Wire command into handoff review flow",
                "complete",
                "test-model",
                now,
            ],
        )
    finally:
        con.close()

    res = runner.invoke(
        main,
        [
            "--config",
            str(cfg),
            "export-bundle",
            "--repo-owner",
            "acme",
            "--repo-name",
            "nexus",
            "--branch",
            "main",
            "--task-id",
            "task-42",
            "--format",
            "yaml",
        ],
    )
    assert res.exit_code == 0, res.output
    assert 'kind: "project"' in res.output
    assert "session-export" in res.output
    assert "Added context bundle export command" in res.output


def test_cli_export_bundle_session_outputs_markdown(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    db = tmp_path / "drover.duckdb"
    parquet = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet, duckdb_path=db)
    now = datetime.now(timezone.utc)
    con = duckdb.connect(str(db))
    try:
        con.execute(
            """
            INSERT INTO session_summaries (
              session_id, task_id, agent_id, ended_at, summary_md, last_user_prompt,
              last_assistant, next_steps_md, open_questions, status, generator_model,
              generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "session-export",
                None,
                "agent-a",
                now,
                "Implemented the session bundle path",
                "Export this session",
                "Done",
                "Hand off to reviewer",
                ["Is the markdown shape right?"],
                "complete",
                "test-model",
                now,
            ],
        )
    finally:
        con.close()

    res = runner.invoke(
        main,
        [
            "--config",
            str(cfg),
            "export-bundle",
            "--session-id",
            "session-export",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "Kind: `session`" in res.output
    assert "session-export" in res.output
    assert "Implemented the session bundle path" in res.output
    assert "Hand off to reviewer" in res.output
    assert "Is the markdown shape right?" in res.output


def test_cli_embeddings_enqueue_spans_dry_run_and_apply(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    parquet_dir = tmp_path / "parquet"
    now = datetime.now(timezone.utc)
    _write_spans(
        parquet_dir,
        [
            {
                "trace_id": "trace-1",
                "span_id": "span-cli",
                "parent_span_id": None,
                "name": "llm_call",
                "service_name": "agentweave",
                "start_time": now,
                "end_time": now + timedelta(seconds=1),
                "duration_ms": 1000.0,
                "session_id": "session-cli",
                "task_id": "task-cli",
                "agent_id": "agent-cli",
                "cost_usd": 0.01,
                "dedup_key": "dedup-cli",
            }
        ],
    )

    dry = runner.invoke(
        main,
        ["--config", str(cfg), "embeddings", "enqueue-spans", "--limit", "10"],
    )
    assert dry.exit_code == 0, dry.output
    assert "candidate_count=1" in dry.output
    assert "enqueued=0" in dry.output

    applied = runner.invoke(
        main,
        [
            "--config",
            str(cfg),
            "embeddings",
            "enqueue-spans",
            "--limit",
            "10",
            "--apply",
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert "candidate_count=1" in applied.output
    assert "enqueued=1" in applied.output

    con = duckdb.connect(str(tmp_path / "drover.duckdb"))
    try:
        assert con.execute("SELECT count(*) FROM span_embed_jobs").fetchone()[0] == 1
    finally:
        con.close()


def test_cli_embeddings_reset_stale_spans_dry_run_and_apply(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "INSERT INTO span_embed_jobs (span_id, status, attempts, last_error, updated_at) VALUES ('stale', 'running', 2, 'worker died', now() - INTERVAL '2 days')"
        )
        con.execute(
            "INSERT INTO span_embed_jobs (span_id, status, attempts, last_error, updated_at) VALUES ('fresh', 'running', 1, NULL, now())"
        )
    finally:
        con.close()

    dry = runner.invoke(main, ["--config", str(cfg), "embeddings", "reset-stale-spans"])
    assert dry.exit_code == 0, dry.output
    assert "mode=dry-run" in dry.output
    assert "matched=1" in dry.output
    assert "reset=0" in dry.output

    applied = runner.invoke(
        main,
        ["--config", str(cfg), "embeddings", "reset-stale-spans", "--apply"],
    )
    assert applied.exit_code == 0, applied.output
    assert "mode=apply" in applied.output
    assert "matched=1" in applied.output
    assert "reset=1" in applied.output

    con = duckdb.connect(str(db))
    try:
        rows = dict(
            con.execute("SELECT span_id, status FROM span_embed_jobs").fetchall()
        )
    finally:
        con.close()
    assert rows == {"stale": "pending", "fresh": "running"}


def test_cli_embeddings_reset_stale_sessions_dry_run_and_apply(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "INSERT INTO embed_jobs (session_id, status, attempts, last_error, updated_at) VALUES ('stale', 'running', 2, 'worker died', now() - INTERVAL '2 days')"
        )
        con.execute(
            "INSERT INTO embed_jobs (session_id, status, attempts, last_error, updated_at) VALUES ('fresh', 'running', 1, NULL, now())"
        )
    finally:
        con.close()

    dry = runner.invoke(
        main, ["--config", str(cfg), "embeddings", "reset-stale-sessions"]
    )
    assert dry.exit_code == 0, dry.output
    assert "mode=dry-run" in dry.output
    assert "matched=1" in dry.output
    assert "reset=0" in dry.output

    applied = runner.invoke(
        main,
        ["--config", str(cfg), "embeddings", "reset-stale-sessions", "--apply"],
    )
    assert applied.exit_code == 0, applied.output
    assert "mode=apply" in applied.output
    assert "matched=1" in applied.output
    assert "reset=1" in applied.output

    con = duckdb.connect(str(db))
    try:
        rows = dict(con.execute("SELECT session_id, status FROM embed_jobs").fetchall())
    finally:
        con.close()
    assert rows == {"stale": "pending", "fresh": "running"}


def test_cli_embeddings_prune_orphan_spans_dry_run_and_apply(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "INSERT INTO span_embed_jobs (span_id, status, attempts, last_error, updated_at) VALUES ('inf', 'errored', 142, 'span row missing', now())"
        )
        con.execute(
            "INSERT INTO span_embed_jobs (span_id, status, attempts, last_error, updated_at) VALUES ('keep', 'errored', 1, 'other error', now())"
        )
    finally:
        con.close()

    dry = runner.invoke(
        main, ["--config", str(cfg), "embeddings", "prune-orphan-spans"]
    )
    assert dry.exit_code == 0, dry.output
    assert "mode=dry-run" in dry.output
    assert "matched=1" in dry.output
    assert "deleted=0" in dry.output

    applied = runner.invoke(
        main,
        ["--config", str(cfg), "embeddings", "prune-orphan-spans", "--apply"],
    )
    assert applied.exit_code == 0, applied.output
    assert "mode=apply" in applied.output
    assert "matched=1" in applied.output
    assert "deleted=1" in applied.output

    con = duckdb.connect(str(db))
    try:
        rows = dict(
            con.execute("SELECT span_id, status FROM span_embed_jobs").fetchall()
        )
    finally:
        con.close()
    assert rows == {"keep": "errored"}


def test_cli_embeddings_drain_once_dry_run_reports_pending_counts(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "INSERT INTO embed_jobs (session_id, status) VALUES ('s1', 'pending')"
        )
        con.execute(
            "INSERT INTO span_embed_jobs (span_id, status) VALUES ('sp1', 'pending')"
        )
    finally:
        con.close()

    dry = runner.invoke(main, ["--config", str(cfg), "embeddings", "drain-once"])
    assert dry.exit_code == 0, dry.output
    assert "mode=dry-run" in dry.output
    assert "pending_sessions=1" in dry.output
    assert "pending_spans=1" in dry.output


def test_cli_incoming_ingest_once_dry_run_and_apply(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    host_dir = tmp_path / "incoming" / "macmini"
    host_dir.mkdir(parents=True)
    jsonl_path = host_dir / "batch.jsonl"
    jsonl_path.write_text(
        json.dumps(
            {
                "id": "incoming-cli-001",
                "session_id": "sess-incoming-cli",
                "timestamp": "2026-05-08T10:00:00Z",
                "agent_id": "test-agent",
                "event_type": "user_message",
                "message": {"role": "user", "content": "hi"},
                "raw_data": {
                    "_repo_owner": "arniesaha",
                    "_repo_name": "nexus",
                    "gitBranch": "main",
                },
            }
        )
        + "\n"
    )

    dry = runner.invoke(
        main, ["--config", str(cfg), "incoming", "ingest-once", str(jsonl_path)]
    )
    assert dry.exit_code == 0, dry.output
    assert "mode=dry-run" in dry.output
    assert jsonl_path.exists()

    applied = runner.invoke(
        main,
        ["--config", str(cfg), "incoming", "ingest-once", str(jsonl_path), "--apply"],
    )
    assert applied.exit_code == 0, applied.output
    assert "mode=apply" in applied.output
    assert not jsonl_path.exists()
    assert (host_dir / ".processed" / "batch.jsonl").exists()

    con = duckdb.connect(str(tmp_path / "drover.duckdb"))
    try:
        assert (
            con.execute(
                "SELECT count(*) FROM agent_events WHERE id='incoming-cli-001'"
            ).fetchone()[0]
            == 1
        )
        assert (
            con.execute(
                "SELECT status FROM summarize_jobs WHERE session_id='sess-incoming-cli'"
            ).fetchone()[0]
            == "pending"
        )
    finally:
        con.close()


def test_cli_embeddings_reset_stale_spans_dry_run_does_not_bootstrap_missing_db(
    tmp_path,
):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    db = tmp_path / "drover.duckdb"
    parquet_dir = tmp_path / "parquet"

    dry = runner.invoke(main, ["--config", str(cfg), "embeddings", "reset-stale-spans"])

    assert dry.exit_code != 0
    assert not db.exists()
    assert not parquet_dir.exists()


def test_cli_session_graph_help_lists_formats():
    runner = CliRunner()
    res = runner.invoke(main, ["session", "graph", "--help"])
    assert res.exit_code == 0
    assert "--format" in res.output
    assert "ascii" in res.output
    assert "json" in res.output
    assert "dot" in res.output


def test_cli_doctor_runs_on_empty_lakehouse(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    res = runner.invoke(main, ["--config", str(cfg), "doctor"])
    assert res.exit_code == 0, res.output
    assert "agent_events" in res.output
    assert "no warnings" in res.output


def test_cli_compact_runs_on_empty_lakehouse(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    res = runner.invoke(main, ["--config", str(cfg), "compact"])
    assert res.exit_code == 0, res.output
    assert "compacted" in res.output


def test_cli_trace_tail_shows_recent_spans_with_filters(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    now = datetime.now(timezone.utc)
    _write_spans(
        parquet_dir,
        [
            {
                "trace_id": "trace-keep",
                "span_id": "span-keep",
                "parent_span_id": None,
                "name": "llm_call",
                "service_name": "claude-code",
                "start_time": now - timedelta(minutes=2),
                "end_time": now - timedelta(minutes=2, seconds=-1),
                "duration_ms": 1000.0,
                "session_id": "sess-keep",
                "task_id": "task-keep",
                "agent_id": "agent-a",
                "cost_usd": 0.01,
                "dedup_key": "keep",
            },
            {
                "trace_id": "trace-drop",
                "span_id": "span-drop",
                "parent_span_id": None,
                "name": "tool_call",
                "service_name": "other-service",
                "start_time": now - timedelta(minutes=1),
                "end_time": now - timedelta(minutes=1, seconds=-1),
                "duration_ms": 1000.0,
                "session_id": "sess-drop",
                "task_id": "task-drop",
                "agent_id": "agent-b",
                "cost_usd": 0.0,
                "dedup_key": "drop",
            },
        ],
    )

    res = runner.invoke(
        main,
        [
            "--config",
            str(cfg),
            "trace-tail",
            "--since-minutes",
            "10",
            "--service",
            "claude-code",
            "--limit",
            "5",
        ],
    )

    assert res.exit_code == 0, res.output
    assert "trace-keep" in res.output
    assert "span-keep" in res.output
    assert "llm_call" in res.output
    assert "trace-drop" not in res.output


def test_cli_recent_traces_alias_supports_json_output(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    now = datetime.now(timezone.utc)
    _write_spans(
        parquet_dir,
        [
            {
                "trace_id": "trace-json",
                "span_id": "span-json",
                "parent_span_id": None,
                "name": "agent.step",
                "service_name": "agentweave-proxy",
                "start_time": now,
                "end_time": now + timedelta(milliseconds=42),
                "duration_ms": 42.0,
                "session_id": "sess-json",
                "task_id": "task-json",
                "agent_id": "agent-json",
                "cost_usd": 0.0,
                "dedup_key": "json",
            }
        ],
    )

    res = runner.invoke(
        main,
        ["--config", str(cfg), "recent-traces", "--json", "--trace-id", "trace-json"],
    )

    assert res.exit_code == 0, res.output
    rows = [json.loads(line) for line in res.output.splitlines()]
    assert rows[0]["trace_id"] == "trace-json"
    assert rows[0]["span_id"] == "span-json"


def test_cli_trace_tail_agent_filter_accepts_raw_alias(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    now = datetime.now(timezone.utc)
    _write_spans(
        parquet_dir,
        [
            {
                "trace_id": "trace-alias",
                "span_id": "span-alias",
                "parent_span_id": None,
                "name": "agent.step",
                "service_name": "agentweave-proxy",
                "start_time": now,
                "end_time": now + timedelta(milliseconds=42),
                "duration_ms": 42.0,
                "session_id": "sess-alias",
                "task_id": "task-alias",
                "agent_id": "claude-code-mac",
                "cost_usd": 0.0,
                "dedup_key": "alias",
            }
        ],
    )

    res = runner.invoke(
        main,
        ["--config", str(cfg), "trace-tail", "--agent", "claude-code-mac", "--json"],
    )

    assert res.exit_code == 0, res.output
    rows = [json.loads(line) for line in res.output.splitlines()]
    assert rows[0]["trace_id"] == "trace-alias"
    assert rows[0]["agent_id"] == "macmini-claude"


def test_cli_trace_tail_skips_missing_recent_partitions(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    _write_spans(
        parquet_dir,
        [
            {
                "trace_id": "old-trace",
                "span_id": "old-span",
                "parent_span_id": None,
                "name": "old_call",
                "service_name": "claude-code",
                "start_time": old,
                "end_time": old + timedelta(milliseconds=1),
                "duration_ms": 1.0,
                "session_id": "old-session",
                "task_id": "old-task",
                "agent_id": "agent-old",
                "cost_usd": 0.0,
                "dedup_key": "old",
            }
        ],
    )

    res = runner.invoke(
        main,
        ["--config", str(cfg), "trace-tail", "--since-minutes", "10", "--limit", "5"],
    )

    assert res.exit_code == 0, res.output
    assert res.output == ""


def test_cli_run_help_exposes_otlp_flags():
    runner = CliRunner()
    res = runner.invoke(main, ["run", "--help"])
    assert res.exit_code == 0
    assert "--no-otlp" in res.output
    assert "--otlp-host" in res.output


def test_cli_run_help_exposes_mcp_flags():
    runner = CliRunner()
    res = runner.invoke(main, ["run", "--help"])
    assert res.exit_code == 0
    assert "--no-mcp" in res.output
    assert "--mcp-host" in res.output


def test_cli_run_help_exposes_summarizer_flag():
    runner = CliRunner()
    res = runner.invoke(main, ["run", "--help"])
    assert res.exit_code == 0
    assert "--no-summarizer" in res.output


def test_cli_summarizer_doctor_reports_auth_without_network(tmp_path, monkeypatch):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    creds = tmp_path / ".credentials.json"
    creds.write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "tok-live", "expiresAt": 4102444800000}}
        )
    )
    monkeypatch.setenv("DROVER_CLAUDE_CREDENTIALS_PATH", str(creds))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)

    res = runner.invoke(main, ["--config", str(cfg), "summarizer-doctor"])

    assert res.exit_code == 0, res.output
    # The default policy is the claude-code harness, so Anthropic credentials
    # are reported as configured but not the selected backend.
    assert "Policy          : harness" in res.output
    assert "Anthropic ready : no" in res.output
    assert "Effective auth  : claude_credentials" in res.output
    assert "tok-live" not in res.output


def test_cli_retry_summarize_jobs_is_dry_run_by_default(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    db = tmp_path / "drover.duckdb"
    parquet = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet, duckdb_path=db)
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts, last_error) VALUES (?, 'errored', 1, ?)",
            ["auth-failed", "401 invalid authentication credentials"],
        )
    finally:
        con.close()

    res = runner.invoke(main, ["--config", str(cfg), "retry-summarize-jobs"])

    assert res.exit_code == 0, res.output
    assert "dry-run" in res.output
    assert "auth-failed" in res.output
    con = duckdb.connect(str(db))
    try:
        assert (
            con.execute(
                "SELECT status FROM summarize_jobs WHERE session_id='auth-failed'"
            ).fetchone()[0]
            == "errored"
        )
    finally:
        con.close()


def test_cli_retry_summarize_jobs_accepts_db_override(tmp_path):
    runner = CliRunner()
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts, last_error) VALUES (?, 'errored', 1, ?)",
            ["auth-failed", "Error code: 401 - Invalid authentication credentials"],
        )
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts, last_error) VALUES (?, 'errored', 1, ?)",
            ["bad-json", "invalid json response"],
        )
    finally:
        con.close()

    res = runner.invoke(main, ["retry-summarize-jobs", "--db", str(db)])

    assert res.exit_code == 0, res.output
    assert "matched: 1" in res.output
    assert "auth-failed" in res.output
    assert "bad-json" not in res.output


def test_cli_retry_summarize_jobs_apply_resets_matching_jobs(tmp_path):
    runner = CliRunner()
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts, last_error) VALUES (?, 'errored', 1, ?)",
            ["auth-failed", "unauthorized"],
        )
    finally:
        con.close()

    res = runner.invoke(main, ["retry-summarize-jobs", "--db", str(db), "--apply"])

    assert res.exit_code == 0, res.output
    assert "matched: 1" in res.output
    assert "updated" in res.output
    con = duckdb.connect(str(db))
    try:
        status = con.execute(
            "SELECT status FROM summarize_jobs WHERE session_id='auth-failed'"
        ).fetchone()[0]
    finally:
        con.close()
    assert status == "pending"


def test_summarizer_backend_config_forwards_launchd_overrides(tmp_path, monkeypatch):
    for var in (
        "DROVER_LOCAL_OLLAMA_LAUNCHD_LABEL",
        "NEXUS_LOCAL_OLLAMA_LAUNCHD_LABEL",
        "DROVER_LOCAL_OLLAMA_LAUNCHD_PLIST",
        "NEXUS_LOCAL_OLLAMA_LAUNCHD_PLIST",
    ):
        monkeypatch.delenv(var, raising=False)
    from drover.config import load_config

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        "[summarizer]\n"
        "backend_policy = 'local'\n"
        "local_ollama_url = 'http://127.0.0.1:11435'\n"
        "local_ollama_launchd_label = 'com.custom.ollama'\n"
        "local_ollama_launchd_plist = '/tmp/com.custom.ollama.plist'\n"
    )
    cfg = load_config(cfg_file)
    backend_cfg = server_main._summarizer_backend_config(cfg)
    assert backend_cfg.local_ollama_launchd_label == "com.custom.ollama"
    assert backend_cfg.local_ollama_launchd_plist == "/tmp/com.custom.ollama.plist"


def test_run_passes_a_pairing_table_to_the_http_server():
    """The hub must own one PairingCodes instance, or `pair` has nothing to
    mint into. Booting the real `run` command here would take the DuckDB write
    lock and start every worker, so this pins the wiring by reading the
    source; the manual end-to-end check in task 7 is what proves it works.
    """
    from pathlib import Path

    import drover.server.__main__ as server_main

    source = Path(server_main.__file__).read_text(encoding="utf-8")
    assert "from drover.server.web.pairing import PairingCodes" in source
    assert "pairing=pairing" in source, "start_metrics_server needs the table"
