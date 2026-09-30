"""Host-local Factory observer launch coverage through the existing /sessions API."""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.request

from drover.schema import bootstrap
from drover.server.harness.daemon import (
    DEFAULT_PRESETS,
    HarnessDaemonState,
    create_harness_server,
    register_daemon_host,
)
from drover.server.harness.model_catalog import CatalogEnvelope, ModelOption
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.registry import HarnessRegistry


class _Catalog:
    def validate(self, harness, model, thinking_effort):
        assert (harness, model, thinking_effort) == ("codex", "gpt-5", "medium")


class _Structured:
    def __init__(self):
        self.starts = []
        self.closed = set()

    def start(self, *args, **kwargs):
        self.starts.append((args, kwargs))

    def has(self, session_id):
        return session_id not in self.closed

    def close(self, session_id):
        self.closed.add(session_id)


def _server(tmp_path):
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=tmp_path / "drover.duckdb")
    state = HarnessDaemonState(
        host_id="studio",
        display_name="Studio",
        kind="macos",
        registry=HarnessRegistry(tmp_path / "drover.duckdb"),
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        worktrees_dir=tmp_path / "worktrees",
    )
    state.model_catalog_service = _Catalog()
    state.structured = _Structured()
    register_daemon_host(state)
    server = create_harness_server(listen_host="127.0.0.1", listen_port=0, state=state)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return server, state, f"http://{host}:{port}"


def _init_repo(path):
    path.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    (path / "file.txt").write_text("hello\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "file.txt"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )


def _post(base, payload):
    request = urllib.request.Request(
        f"{base}/sessions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode())


def test_factory_observer_launches_host_local_isolated_worktree_and_retries_once(
    tmp_path,
):
    server, state, base = _server(tmp_path)
    repo = tmp_path / "drover"
    _init_repo(repo)
    payload = {
        "factory_observer": {
            "run_id": "run_FACTORY001",
            "expected_revision": 4,
            "idempotency_key": "factory.launch:run_FACTORY001:4",
            "target_hostname": "studio",
            "repo": {
                "owner": "arniesaha",
                "name": "drover",
                "branch": "factory/run_FACTORY001",
            },
            "worktree": {"cwd": str(repo), "policy": "isolated_required"},
            "command": "harness_default",
        },
        "harness": "codex",
        "model": "gpt-5",
        "thinking_effort": "medium",
    }
    try:
        created_status, created = _post(base, payload)
        retry_status, retry = _post(base, payload)

        assert created_status == 201
        assert retry_status == 200
        assert retry["deduplicated"] is True
        assert retry["session_id"] == created["session_id"]
        assert retry["factory_observer"] == created["factory_observer"]
        assert len(state.structured.starts) == 1
        assert created["factory_observer"] == {
            "run_id": "run_FACTORY001",
            "expected_revision": 4,
            "target_hostname": "studio",
            "status": "running",
            "authority": "taskflow",
            "worktree": {
                "path": str(tmp_path / "worktrees" / created["session_id"]),
                "branch": f"drover/{created['session_id']}",
            },
        }
        session = state.registry.get_session(created["session_id"])
        assert session is not None
        assert session.cwd == str(tmp_path / "worktrees" / created["session_id"])
        assert session.handoff_mode == "factory_observer"
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()
