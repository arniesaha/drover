"""Private, fenced rehearsal of the real all-role path up to HTTP binding.

No server is started. Worker scheduling, ingestion, push and model execution
are replaced or forbidden. PostgreSQL fixtures provision only a disposable
Unix-socket cluster (run with DROVER_TEST_POSTGRES_DSN unset).
"""

import faulthandler
import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import duckdb
import pytest
from click.testing import CliRunner

from drover.server import __main__ as server_main
from drover.server import watcher as watcher_module
from drover.server.control_store import close_control_store
from drover.server.db import (
    close_analytical_connections,
    close_control_plane_connections,
)
from drover.server.web import auth as auth_module


class FenceViolation(BaseException):
    """Bypass production best-effort Exception handlers so a breach fails."""


class BeforeBind(BaseException):
    pass


@contextmanager
def phase_deadline(name, seconds=10):
    # faulthandler includes stacks, never Python locals or fixture payloads.
    print("private phase starting " + name, flush=True)
    # Click 8.5 captures sys.stderr in a BytesIO-backed stream without fileno.
    # Keep a real descriptor for the hang guard; never drop the guard to pass CI.
    faulthandler.dump_traceback_later(seconds, exit=True, file=sys.__stderr__)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


def test_phase_deadline_survives_non_fd_stderr(monkeypatch):
    import io

    monkeypatch.setattr(sys, "stderr", io.StringIO())
    with phase_deadline("captured_stderr"):
        pass


def test_phase_deadline_dumps_stacks_and_exits():
    script = (
        "import faulthandler, time\n"
        "def blocked_fixture_phase():\n"
        "    faulthandler.dump_traceback_later(0.05, exit=True)\n"
        "    time.sleep(5)\n"
        "blocked_fixture_phase()\n"
    )
    import sys

    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, timeout=2, text=True
    )
    assert result.returncode != 0
    assert "blocked_fixture_phase" in result.stderr
    assert "Timeout" in result.stderr


def test_startup_phase_failure_does_not_log_exception_payload(caplog):
    with pytest.raises(ValueError):
        with server_main._startup_phase("synthetic_failure"):
            raise ValueError("credential-and-query-payload-must-not-be-logged")
    assert "synthetic_failure" in caplog.text
    assert "ValueError" in caplog.text
    assert "credential-and-query-payload" not in caplog.text


def test_backlog_prunes_archive_before_enumeration(tmp_path):
    incoming = tmp_path / "incoming"
    archive = incoming / "host" / ".processed" / "deep"
    archive.mkdir(parents=True)
    (archive / "old.jsonl").write_text("synthetic archive")
    pending = incoming / "host" / "pending.jsonl"
    pending.write_text("synthetic pending")
    w = watcher_module.IncomingWatcher(
        incoming_dir=incoming,
        parquet_dir=tmp_path / "parquet",
        duckdb_path=tmp_path / "private.duckdb",
    )
    scanned = []
    real_scandir = os.scandir

    def guarded_scandir(path):
        scanned.append(Path(path))
        if ".processed" in Path(path).parts:
            raise FenceViolation("startup enumerated the processed archive")
        return real_scandir(path)

    with patch.object(w._handler, "_maybe_ingest") as ingest:
        with patch.object(os, "scandir", guarded_scandir):
            w._ingest_backlog()
        ingest.assert_called_once_with(pending)
    assert w.backlog_done.is_set()
    assert not any(".processed" in path.parts for path in scanned)


def test_pending_order_symlinks_and_failed_file_are_preserved(tmp_path):
    incoming = tmp_path / "incoming"
    host = incoming / "host"
    host.mkdir(parents=True)
    first = host / "a.jsonl"
    last = host / "z.jsonl"
    first.write_text("synthetic first")
    last.write_text("synthetic last")
    (host / "ignored.jsonl.tmp").write_text("in flight")
    link = host / "b.jsonl"
    link.symlink_to(first)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "not-pending.jsonl").write_text("outside")
    (host / "linked-directory").symlink_to(outside, target_is_directory=True)
    w = watcher_module.IncomingWatcher(
        incoming_dir=incoming,
        parquet_dir=tmp_path / "parquet",
        duckdb_path=tmp_path / "private.duckdb",
    )
    with patch.object(
        w._handler,
        "_maybe_ingest",
        side_effect=[ValueError("synthetic failure"), None, None],
    ) as ingest:
        w._ingest_backlog()
        assert [call.args[0] for call in ingest.call_args_list] == [first, link, last]
    assert w.backlog_done.is_set()
    assert first.exists()


def test_three_pending_lock_conflicts_spend_93_seconds_in_prebind_retry(tmp_path):
    """A separate startup hypothesis: five exponential waits for each batch.

    No real waiting or database access. This diagnoses the existing policy;
    the archive pruning repair deliberately does not change ingest retries.
    """
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    for index in range(3):
        (incoming / f"{index}.jsonl").write_text("synthetic pending")
    w = watcher_module.IncomingWatcher(
        incoming_dir=incoming,
        parquet_dir=tmp_path / "parquet",
        duckdb_path=tmp_path / "private.duckdb",
    )
    with patch.object(
        w._handler, "_ingest_once", side_effect=duckdb.IOException("Could not set lock")
    ) as ingest:
        with patch.object(watcher_module.time, "sleep") as sleep:
            w._ingest_backlog()
    assert ingest.call_count == 18
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2, 4, 8, 16] * 3
    assert sum(call.args[0] for call in sleep.call_args_list) == 93
    assert len(list(incoming.glob("*.jsonl"))) == 3


@pytest.mark.parametrize("analytical_pin", [False, True])
@pytest.mark.parametrize("existing_views", [False, True])
@pytest.mark.parametrize("postgres", [False, True])
def test_all_role_reaches_bind_inside_fence(
    tmp_path, monkeypatch, request, postgres, existing_views, analytical_pin
):
    # No ambient configuration, DSN, credentials or default home enters run().
    monkeypatch.setattr(auth_module, "config_home", lambda: tmp_path)
    monkeypatch.setattr(server_main, "config_home", lambda: tmp_path)
    monkeypatch.setenv("DROVER_ANALYTICAL_PIN", "1" if analytical_pin else "0")
    monkeypatch.setenv("DROVER_CONTROL_PLANE_PIN", "0")
    monkeypatch.setenv("DROVER_CLAUDE_CREDENTIALS_PATH", str(tmp_path / "missing"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("DROVER_API_TOKEN", raising=False)
    monkeypatch.delenv("NEXUS_API_TOKEN", raising=False)
    control = "backend = 'duckdb'"
    if postgres:
        # Refuse caller-supplied databases; this test must own its cluster.
        assert not os.environ.get("DROVER_TEST_POSTGRES_DSN")
        dsn = request.getfixturevalue("postgres_dsn")
        monkeypatch.setenv("PRIVATE_REHEARSAL_DSN", dsn)
        control = "backend = 'postgres'\ndsn_env = 'PRIVATE_REHEARSAL_DSN'"
    config = tmp_path / "config.toml"
    config.write_text(
        f"[paths]\nincoming_dir = '{tmp_path / 'incoming'}'\n"
        f"parquet_dir = '{tmp_path / 'parquet'}'\n"
        f"duckdb_path = '{tmp_path / 'private.duckdb'}'\n"
        f"[control_store]\n{control}\n"
        "[auth]\nenabled = true\napi_token = 'synthetic-fixture-token'\n"
        "[update]\nenabled = false\n"
    )
    cfg = server_main._resolve_config(str(config))
    if postgres:
        # Approval applies only to the empty disposable fixture, before replay.
        server_main.bootstrap_control_plane_store(cfg.duckdb_path)
        server_main.initialize_empty_control_store(cfg.duckdb_path)

    if existing_views:
        server_main.bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)
        # Any historical view rebinding would inspect this invalid footer and
        # fail. The exact prebind replay must leave historical Parquet alone.
        history = (
            cfg.parquet_dir / "agent_events" / "date=2050-01-01" / "agent_id=fixture"
        )
        history.mkdir(parents=True)
        (history / "invalid.parquet").write_bytes(b"synthetic invalid footer")

    archive = cfg.incoming_dir / "host" / ".processed" / "deep"
    archive.mkdir(parents=True)
    (archive / "old.jsonl").write_text("synthetic archive")
    timeline = []
    original_phase = server_main._startup_phase

    @contextmanager
    def timed_phase(name):
        started = time.monotonic()
        try:
            with phase_deadline(name), original_phase(name):
                yield
        finally:
            timeline.append([name, round(time.monotonic() - started, 4)])

    monkeypatch.setattr(server_main, "_startup_phase", timed_phase)
    original_scandir = os.scandir

    def private_scandir(path):
        if not isinstance(path, int) and ".processed" in Path(path).parts:
            raise FenceViolation("prebind enumerated the processed archive")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", private_scandir)
    # Replay enumeration only: no observer, ingestion, retention or queue writes.
    monkeypatch.setattr(
        watcher_module.IncomingWatcher,
        "start",
        watcher_module.IncomingWatcher._ingest_backlog,
    )
    forbidden = []
    for target, attribute in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket.socket, "bind"),
        (subprocess, "Popen"),
        (ThreadPoolExecutor, "submit"),
        (watcher_module._Handler, "_maybe_ingest"),
    ):
        mock = Mock(side_effect=FenceViolation(attribute))
        monkeypatch.setattr(target, attribute, mock)
        forbidden.append(mock)
    # Every scheduled thread is inert, including analytical bootstrap/warmups.
    thread_start = Mock()
    monkeypatch.setattr(threading.Thread, "start", thread_start)
    for worker in (
        server_main.AdvisoryWorker,
        server_main.ControlOutboxExporter,
        server_main.UsageRollupWorker,
        server_main.NativeUsageRollupWorker,
    ):
        monkeypatch.setattr(worker, "start", Mock())
    push = Mock()
    monkeypatch.setattr(server_main, "_configure_push", push)
    from drover.server import push as push_module

    for attribute in ("configure", "dispatch_awaiting_transition"):
        mock = Mock(side_effect=FenceViolation(attribute))
        monkeypatch.setattr(push_module, attribute, mock)
        forbidden.append(mock)
    monkeypatch.setattr(server_main, "_register_stack_dump", Mock())
    bind = Mock(side_effect=BeforeBind())
    monkeypatch.setattr(server_main, "start_resilient_metrics_server", bind)
    # Bounded foreground rehearsal. A hang dumps all stacks, then exits.
    faulthandler.dump_traceback_later(20, exit=True, file=sys.__stderr__)
    try:
        with pytest.raises(BeforeBind):
            CliRunner().invoke(
                server_main.main,
                ["--config", str(config), "run", "--no-mcp", "--no-otlp"],
                catch_exceptions=False,
            )
        bind.assert_called_once()
        for mock in forbidden:
            mock.assert_not_called()
        # The replacement receives the registration, but sends nothing.
        push.assert_called_once()
        assert thread_start.call_count >= 2  # scheduled, never executed
        assert timeline
        assert sum(duration for _, duration in timeline) < 20
        names = [name for name, _ in timeline]
        assert "watcher_start_and_backlog" in names
        if postgres:
            assert "require_control_store_ready" in names
            assert "initialize_central_consent" in names
        print(
            "private prebind phases "
            + json.dumps(
                {
                    "postgres": postgres,
                    "existing_views": existing_views,
                    "analytical_pin": analytical_pin,
                    "phases": timeline,
                }
            )
        )
    finally:
        faulthandler.cancel_dump_traceback_later()
        close_analytical_connections()
        close_control_plane_connections()
        close_control_store(cfg.duckdb_path)
