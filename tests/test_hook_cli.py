"""End-to-end tests for the drover-hook CLI."""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner

from drover.hook.__main__ import main as hook_main
from drover.schema import bootstrap
from drover.server.mcp.server import build_mcp_server


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Return a minimal git repository suitable for CLI hook tests."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "git@github.com:test/repo.git",
        ],
        check=True,
    )
    (repo / "x").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "x"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "i"], check=True)
    return repo


@pytest.fixture
def live_server(tmp_path: Path):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    port = _free_port()
    server = build_mcp_server(duckdb_path=duckdb_path, host="127.0.0.1", port=port)

    def _run():
        try:
            server.run(transport="streamable-http")
        except Exception:
            pass

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.skip("MCP server did not start")
    yield {"url": f"http://127.0.0.1:{port}/mcp", "duckdb_path": duckdb_path}


def test_session_start_prints_handoff_block(monkeypatch, git_repo: Path) -> None:
    """Test CLI rendering with a deterministic mock payload (no live server)."""
    repo = git_repo

    fake_payload = {
        "task_id": "abc1234567890def",
        "repo_owner": "test",
        "repo_name": "repo",
        "branch": "main",
        "summaries": [],
        "active_sessions": [],
    }

    import drover.hook.__main__ as hook_main_mod

    monkeypatch.setattr(hook_main_mod, "call_tool", lambda **kw: fake_payload)

    runner = CliRunner()
    res = runner.invoke(
        hook_main,
        [
            "session-start",
            "--cwd",
            str(repo),
            "--timeout",
            "2",
            "--agent-id",
            "test-agent",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "Resuming task" in res.output
    assert "no prior summaries" in res.output.lower()


def test_session_start_in_non_git_dir_emits_skip_banner(
    tmp_path: Path, live_server
) -> None:
    runner = CliRunner()
    res = runner.invoke(
        hook_main,
        [
            "session-start",
            "--cwd",
            str(tmp_path),
            "--mcp-url",
            live_server["url"],
            "--timeout",
            "5",
            "--agent-id",
            "test-agent",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "no git context" in res.output


def test_session_start_offline_exits_zero_with_stderr(git_repo: Path) -> None:
    """Connection refused → '(drover offline)' sentinel, exit 0."""
    repo = git_repo

    runner = CliRunner()
    res = runner.invoke(
        hook_main,
        [
            "session-start",
            "--cwd",
            str(repo),
            "--mcp-url",
            "http://127.0.0.1:1/dead",
            "--timeout",
            "0.5",
            "--agent-id",
            "x",
        ],
    )
    assert res.exit_code == 0, res.output
    # Combined stdout+stderr — runner captures both into res.output by default
    assert "(drover offline)" in res.output


def test_session_start_timeout_sentinel(git_repo: Path, live_server) -> None:
    """Listening server + tiny budget → '(drover timeout: ...)' sentinel, never 'offline'."""
    repo = git_repo

    runner = CliRunner()
    res = runner.invoke(
        hook_main,
        [
            "session-start",
            "--cwd",
            str(repo),
            "--mcp-url",
            live_server["url"],
            "--timeout",
            "0.001",
            "--agent-id",
            "test-agent",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "(drover timeout: context unavailable)" in res.output
    assert "offline" not in res.output.lower()


def test_session_end_timeout_sentinel(live_server) -> None:
    """Listening server + tiny budget → timeout sentinel, never 'offline'."""
    runner = CliRunner()
    res = runner.invoke(
        hook_main,
        [
            "session-end",
            "--session-id",
            "sess-timeout-test",
            "--mcp-url",
            live_server["url"],
            "--timeout",
            "0.001",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "(drover timeout: context unavailable)" in res.output
    assert "offline" not in res.output.lower()


def test_session_end_enqueues_summarize_job(tmp_path: Path, live_server) -> None:
    runner = CliRunner()
    res = runner.invoke(
        hook_main,
        [
            "session-end",
            "--session-id",
            "sess-cli-end",
            "--mcp-url",
            live_server["url"],
            "--timeout",
            "5",
        ],
    )
    assert res.exit_code == 0, res.output

    con = duckdb.connect(str(live_server["duckdb_path"]))
    try:
        row = con.execute(
            "SELECT status FROM summarize_jobs WHERE session_id='sess-cli-end'"
        ).fetchone()
        assert row is not None and row[0] == "pending"
    finally:
        con.close()


def test_session_end_offline_exits_zero(tmp_path: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(
        hook_main,
        [
            "session-end",
            "--session-id",
            "x",
            "--mcp-url",
            "http://127.0.0.1:1/dead",
            "--timeout",
            "0.5",
        ],
    )
    assert res.exit_code == 0, res.output
    # Combined stdout+stderr — runner captures both into res.output by default
    assert "(drover offline)" in res.output
