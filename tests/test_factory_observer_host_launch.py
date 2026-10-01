"""Host-local Factory observer launch coverage through the existing /sessions API."""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

from drover.schema import bootstrap
from drover.server.harness.adapters import HarnessAdapterRegistry
from drover.server.harness.daemon import (
    DEFAULT_PRESETS,
    HarnessDaemonState,
    create_harness_server,
    register_daemon_host,
)
from drover.server.harness.model_catalog import (
    DiscoveredCatalog,
    ModelCatalogService,
    ModelOption,
    ReasoningOptions,
)
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.structured.adapters import ClaudeCodeAdapter, CodexAdapter
from drover.server.harness.worktree import WorktreeIsolationUnavailable
from drover.server.metrics import MetricsCollector, start_metrics_server


class _Catalog:
    def cache_identity(self):
        return "fake"

    def discover(self):
        return DiscoveredCatalog(
            account_scope_material="fake",
            harness_version="fake",
            models=(
                ModelOption(id="opus", display_name="Opus"),
                ModelOption(
                    id="gpt-5",
                    display_name="GPT-5",
                    reasoning=ReasoningOptions(
                        supported=("medium",),
                        default="medium",
                    ),
                ),
            ),
        )


class _ClaudeAdapter(ClaudeCodeAdapter):
    def default_command(self):
        return ["fake-claude", "-p", "--permission-mode", "bypassPermissions"]


class _Structured:
    def __init__(self):
        self.starts = []
        self.closed = set()

    def start(self, *args, **kwargs):
        self.starts.append((args, kwargs))
        self.closed.discard(args[0])

    def has(self, session_id):
        return session_id not in self.closed

    def is_alive(self, session_id):
        return self.has(session_id)

    def record_recovered(self, session_id, native_session_id):
        pass

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
    state.adapters = HarnessAdapterRegistry([CodexAdapter(), _ClaudeAdapter()])
    state.model_catalog_service = ModelCatalogService(
        host_id="studio", adapters={"codex": _Catalog(), "claude-code": _Catalog()}
    )
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


def _post(base, payload, path="/sessions"):
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode())


@pytest.mark.parametrize(
    "harness,model,effort",
    [
        ("codex", "gpt-5", "medium"),
        ("claude-code", "opus", None),
    ],
)
def test_factory_observer_launches_host_local_isolated_worktree_and_retries_once(
    tmp_path,
    harness,
    model,
    effort,
):
    server, state, base = _server(tmp_path)
    hub_path = tmp_path / "hub.duckdb"
    bootstrap(parquet_dir=tmp_path / "hub-parquet", duckdb_path=hub_path)
    collector = MetricsCollector(
        duckdb_path=hub_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
    )
    hub_registry = HarnessRegistry(hub_path)
    hub_registry.register_host(
        host_id="studio", display_name="Studio", kind="macos", local_url=base
    )
    hub_registry.create_session(
        session_id="legacy-null-cwd", host_id="studio", harness="codex", command="codex"
    )
    hub_server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
    hub_base = f"http://127.0.0.1:{hub_server.server_address[1]}"
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
        "harness": harness,
        "model": model,
    }
    if effort is not None:
        payload["thinking_effort"] = effort
    try:
        created_status, created = _post(
            hub_base, payload, "/harness/hosts/studio/sessions"
        )
        retry_status, retry = _post(hub_base, payload, "/harness/hosts/studio/sessions")

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
        with urllib.request.urlopen(
            f"{hub_base}/harness/sessions", timeout=5
        ) as response:
            listing = json.loads(response.read())
        sessions = {item["session_id"]: item for item in listing["sessions"]}
        listed = sessions[created["session_id"]]
        assert listed["cwd"] == session.cwd
        assert listed["cwd"] != str(repo)
        assert listed["repo_owner"] == "arniesaha"
        assert listed["repo_name"] == "drover"
        assert listed["branch"] == "factory/run_FACTORY001"
        assert listed["mode"] == "structured"
        assert listed["factory_observer"]["run_id"] == "run_FACTORY001"
        assert sessions["legacy-null-cwd"]["cwd"] is None
        assert (session.repo_owner, session.repo_name, session.branch) == (
            "arniesaha",
            "drover",
            "factory/run_FACTORY001",
        )
        assert session.model == model
        assert session.thinking_effort == effort
        started = state.structured.starts[0][1]
        assert started["cwd"] == session.cwd
        if harness == "claude-code":
            assert started["command"] == [
                "fake-claude",
                "-p",
                "--model",
                "opus",
                "--permission-mode",
                "dontAsk",
            ]
            state.structured.close(session.session_id)
            state.registry.update_session_status(
                session.session_id,
                "errored",
                last_error="daemon restarted; structured session lost",
                ended_at=datetime.now(timezone.utc),
            )
            recovery_status, recovered = _post(
                base,
                {"native_session_id": "claude-native"},
                path=f"/sessions/{session.session_id}/recover",
            )
            assert recovery_status == 200
            assert recovered["recovered"] is True
            assert state.structured.starts[-1][1]["command"] == started["command"]
            assert state.structured.starts[-1][1]["cwd"] == session.cwd

    finally:
        hub_server.shutdown()
        hub_server.server_close()
        state.pty.close_all()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("failure", ["effort", "not_git", "isolation_unavailable"])
def test_claude_factory_rejects_invalid_effort_or_unavailable_isolation(
    tmp_path,
    monkeypatch,
    failure,
):
    from test_factory_observer_bridge import _payload

    server, state, base = _server(tmp_path)
    repo = tmp_path / "drover"
    if failure == "not_git":
        repo.mkdir()
    else:
        _init_repo(repo)
    payload = _payload()
    payload.update(harness="claude-code", model="opus")
    payload["factory_observer"]["worktree"]["cwd"] = str(repo)
    if failure == "effort":
        payload["thinking_effort"] = "high"
        expected_status = 400
        expected_error = "reasoning effort is not supported by this model"
    else:
        payload.pop("thinking_effort")
        expected_status = 400
        expected_error = "requires an isolated Git worktree"
    if failure == "isolation_unavailable":

        def fail(*args):
            raise WorktreeIsolationUnavailable("test isolation failure")

        monkeypatch.setattr(
            "drover.server.harness.daemon.create_session_worktree", fail
        )
        expected_status = 503
        expected_error = "worktree isolation unavailable for claude-code"
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(base, payload)
        assert error.value.code == expected_status
        assert expected_error in json.load(error.value)["error"]
        assert state.structured.starts == []
        assert state.registry.list_sessions(host_id="studio") == []
        assert not state.session_worktrees
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()
