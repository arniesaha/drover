"""Tests for the Meta Harness host daemon."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from click.testing import CliRunner
import pytest

from drover.schema import bootstrap
from drover.server.harness.cli import main as harnessd_cli
from drover.server.harness import daemon as harness_daemon
from drover.server.harness.auth import (
    AuthFlowManager,
    HarnessAuthStatus,
    StaticAuthAdapter,
)
from drover.server.__main__ import main
from drover.server.harness.daemon import (
    DEFAULT_PRESETS,
    HarnessDaemonState,
    HarnessPreset,
    build_launch_command,
    create_harness_server,
    discover_native_resume_sessions,
    native_transcript_for_session,
    register_daemon_host,
    register_daemon_host_remote,
    resolve_harness_presets,
    wire_event_pusher,
)
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.websocket import client_handshake


class _FailingRegistry:
    def register_host(self, **kwargs):
        raise RuntimeError("locked")

    def create_session(self, **kwargs):
        raise RuntimeError("locked")

    def update_session_status(self, *args, **kwargs):
        raise RuntimeError("locked")

    def append_event(self, **kwargs):
        raise RuntimeError("locked")

    def append_transcript_chunk(self, **kwargs):
        raise RuntimeError("locked")


def test_run_harnessd_closes_auth_flows_on_shutdown(monkeypatch, tmp_path):
    calls = []

    class _Closer:
        def __init__(self, name):
            self.name = name

        def close_all(self):
            calls.append(self.name)

    class _State:
        api_token = ""
        host_token = None
        pty = _Closer("pty")
        auth = _Closer("auth")

    class _Server:
        def serve_forever(self):
            raise RuntimeError("stop")

        def server_close(self):
            calls.append("server")

    monkeypatch.setattr(harness_daemon, "HarnessDaemonState", lambda **kwargs: _State())
    monkeypatch.setattr(harness_daemon, "resolve_daemon_token", lambda token: "token")
    monkeypatch.setattr(harness_daemon, "wire_event_pusher", lambda state: None)
    monkeypatch.setattr(harness_daemon, "register_daemon_host", lambda state: None)
    monkeypatch.setattr(
        harness_daemon, "register_daemon_host_remote", lambda state: True
    )
    monkeypatch.setattr(harness_daemon, "start_remote_heartbeat", lambda state: None)
    monkeypatch.setattr(
        harness_daemon, "create_harness_server", lambda **kwargs: _Server()
    )

    with pytest.raises(RuntimeError, match="stop"):
        harness_daemon.run_harnessd(
            host_id="test-host",
            display_name="Test Host",
            kind="mac",
            duckdb_path=tmp_path / "drover.duckdb",
            listen_host="127.0.0.1",
            listen_port=0,
        )

    assert calls == ["pty", "auth", "server"]


class _CentralRegistrationHandler(BaseHTTPRequestHandler):
    payloads: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        self.__class__.payloads.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        payload = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _json_request(url: str, *, payload: dict | None = None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _raw_request(url: str, *, method: str = "GET", payload: dict | None = None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    return urllib.request.urlopen(request, timeout=5)


def _wait_until(predicate, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise AssertionError("condition was not met before timeout")


def _fetch_session(base_url: str, session_id: str) -> dict:
    _, session = _json_request(f"{base_url}/sessions/{session_id}")
    return session


# A fake, headless-safe "claude-code"-shaped CLI: it speaks the same
# stream-json control_request/control_response envelope ClaudeDriver parses,
# without running any real agent. Reading `sys.stdin` line by line mirrors
# ProcessDriver's long-lived bidirectional process model. Every non-
# control_response line it receives (i.e. every turn) triggers a fresh
# approval prompt, and a control_response unblocks it with a completed turn.
FAKE_STRUCTURED_CLI = [
    sys.executable,
    "-c",
    (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        "    obj=json.loads(line)\n"
        "    if obj.get('type')=='control_response':\n"
        "        print(json.dumps({'type':'assistant','message':{'role':'assistant',"
        "'content':[{'type':'text','text':'approved and done'}]}}),flush=True)\n"
        "        print(json.dumps({'type':'result','subtype':'success'}),flush=True)\n"
        "    else:\n"
        "        print(json.dumps({'type':'control_request','request_id':'req-1',"
        "'request':{'subtype':'can_use_tool','tool_name':'Bash',"
        "'input':{'command':'ls'}}}),flush=True)\n"
    ),
]


def _start_test_server(tmp_path, *, api_token: str = ""):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    state = HarnessDaemonState(
        host_id="test-host",
        display_name="Test Host",
        kind="linux",
        registry=registry,
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        local_url="http://127.0.0.1:0",
        api_token=api_token,
        worktrees_dir=tmp_path / "worktrees",
    )
    register_daemon_host(state)
    server = create_harness_server(listen_host="127.0.0.1", listen_port=0, state=state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, state, f"http://{host}:{port}"


def test_cli_harnessd_help_documents_core_options():
    result = CliRunner().invoke(main, ["harnessd", "--help"])

    assert result.exit_code == 0, result.output
    assert "--host-id" in result.output
    assert "--listen" in result.output
    assert "--local-url" in result.output
    assert "--tailscale-url" in result.output


def test_skinny_harnessd_entrypoint_documents_core_options():
    result = CliRunner().invoke(harnessd_cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "--host-id" in result.output
    assert "--listen" in result.output
    assert "--local-url" in result.output
    assert "--tailscale-url" in result.output
    assert "--central-url" in result.output
    assert "--host-token" in result.output
    assert "--relay" in result.output


def test_daemon_can_register_host_with_central_server(tmp_path):
    central = ThreadingHTTPServer(("127.0.0.1", 0), _CentralRegistrationHandler)
    _CentralRegistrationHandler.payloads = []
    thread = threading.Thread(target=central.serve_forever, daemon=True)
    thread.start()
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    state = HarnessDaemonState(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        registry=HarnessRegistry(duckdb_path),
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        local_url="http://192.168.1.70:7081",
        central_url=f"http://127.0.0.1:{central.server_address[1]}",
        host_token="secret",
    )

    try:
        assert register_daemon_host_remote(state) is True
    finally:
        central.shutdown()
        central.server_close()

    assert _CentralRegistrationHandler.payloads == [
        {
            "path": "/harness/hosts",
            "authorization": "Bearer secret",
            "body": {
                "capabilities": state.capabilities(),
                "connection_kind": "direct",
                "display_name": "NAS",
                "host_id": "nas",
                "kind": "linux",
                "local_url": "http://192.168.1.70:7081",
                "status": "online",
                "tailscale_url": None,
            },
        }
    ]


def test_relay_daemon_registers_itself_as_relay_connected(tmp_path):
    central = ThreadingHTTPServer(("127.0.0.1", 0), _CentralRegistrationHandler)
    _CentralRegistrationHandler.payloads = []
    thread = threading.Thread(target=central.serve_forever, daemon=True)
    thread.start()
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    state = HarnessDaemonState(
        host_id="laptop",
        display_name="Laptop",
        kind="mac",
        registry=HarnessRegistry(duckdb_path),
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        central_url=f"http://127.0.0.1:{central.server_address[1]}",
        host_token="secret",
        relay=True,
    )

    try:
        assert register_daemon_host_remote(state) is True
    finally:
        central.shutdown()
        central.server_close()

    assert _CentralRegistrationHandler.payloads[0]["body"]["connection_kind"] == "relay"


def test_wire_event_pusher_pushes_structured_events_to_central(tmp_path):
    central = ThreadingHTTPServer(("127.0.0.1", 0), _CentralRegistrationHandler)
    _CentralRegistrationHandler.payloads = []
    thread = threading.Thread(target=central.serve_forever, daemon=True)
    thread.start()
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    state = HarnessDaemonState(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        registry=HarnessRegistry(duckdb_path),
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        central_url=f"http://127.0.0.1:{central.server_address[1]}",
        api_token="secret",
    )

    pusher = wire_event_pusher(state)
    try:
        assert pusher is not None
        assert state.push_event == pusher.push
        # Drive an event through the exact hook the structured manager calls.
        state.push_event(
            "harness-s1",
            {
                "event_id": "harness-event-w1",
                "session_id": "harness-s1",
                "seq": 1,
                "type": "status",
                "payload": {"turn_complete": True},
            },
        )
        pusher.stop()
    finally:
        central.shutdown()
        central.server_close()

    event_posts = [
        payload
        for payload in _CentralRegistrationHandler.payloads
        if payload["path"] == "/harness/events"
    ]
    assert len(event_posts) == 1
    assert event_posts[0]["authorization"] == "Bearer secret"
    events = event_posts[0]["body"]["events"]
    assert [event["event_id"] for event in events] == ["harness-event-w1"]


def test_wire_event_pusher_is_noop_without_central_url_or_token(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    base = dict(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        registry=HarnessRegistry(duckdb_path),
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
    )

    no_central = HarnessDaemonState(**base, api_token="secret")
    no_token = HarnessDaemonState(**base, central_url="http://127.0.0.1:9")

    assert wire_event_pusher(no_central) is None
    assert wire_event_pusher(no_token) is None
    # The no-op default stays: calling it must not raise.
    no_central.push_event("harness-s1", {"event_id": "e1", "type": "status"})
    no_token.push_event("harness-s1", {"event_id": "e1", "type": "status"})


def test_harnessd_health_and_capabilities(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        status, health = _json_request(f"{base_url}/healthz")
        assert status == 200
        assert health["ok"] is True
        assert health["host_id"] == "test-host"

        _, capabilities = _json_request(f"{base_url}/capabilities")
        harnesses = {item["name"]: item for item in capabilities["harnesses"]}
        assert harnesses["shell"]["enabled"] is True
        assert harnesses["codex"]["enabled"] is False
        assert state.registry.get_host("test-host") is not None
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_harnessd_auth_status_route(tmp_path):
    server, state, base_url = _start_test_server(tmp_path, api_token="secret")
    state.auth = AuthFlowManager(
        {
            "claude-code": StaticAuthAdapter(
                "claude-code",
                status_value=HarnessAuthStatus("claude-code", "unauthenticated"),
            )
        }
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/auth/claude-code/status",
            headers={"Authorization": "Bearer secret"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        state.pty.close_all()

    assert response.status == 200
    assert body["host_id"] == "test-host"
    assert body["harness"] == "claude-code"
    assert body["state"] == "unauthenticated"


def test_harnessd_auth_start_poll_and_cancel(tmp_path):
    script = tmp_path / "login.py"
    script.write_text(
        "import time\n"
        "print('Open https://example.test/device and enter WXYZ-1234', flush=True)\n"
        "time.sleep(5)\n"
    )
    server, state, base_url = _start_test_server(tmp_path, api_token="secret")
    state.auth = AuthFlowManager(
        {
            "codex": StaticAuthAdapter(
                "codex",
                status_value=HarnessAuthStatus("codex", "unauthenticated"),
                start_command=[sys.executable, str(script)],
            )
        },
        timeout_s=30,
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/auth/codex/start",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.status == 200
            started = json.loads(response.read().decode("utf-8"))
        flow_id = started["flow_id"]

        poll_req = urllib.request.Request(
            f"{base_url}/auth/codex/flows/{flow_id}",
            headers={"Authorization": "Bearer secret"},
        )
        _wait_until(
            lambda: json.loads(
                urllib.request.urlopen(poll_req, timeout=5).read().decode("utf-8")
            ).get("login_url")
        )
        with urllib.request.urlopen(poll_req, timeout=5) as response:
            polled = json.loads(response.read().decode("utf-8"))

        cancel_req = urllib.request.Request(
            f"{base_url}/auth/codex/flows/{flow_id}/cancel",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(cancel_req, timeout=5) as response:
            cancelled = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        state.pty.close_all()

    assert started["host_id"] == "test-host"
    assert started["harness"] == "codex"
    assert polled["host_id"] == "test-host"
    assert cancelled["state"] == "cancelled"
    assert cancelled["host_id"] == "test-host"


def test_harnessd_auth_routes_require_bearer_token(tmp_path):
    server, state, base_url = _start_test_server(tmp_path, api_token="secret")
    state.auth = AuthFlowManager(
        {
            "codex": StaticAuthAdapter(
                "codex",
                status_value=HarnessAuthStatus("codex", "unauthenticated"),
            )
        }
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/auth/codex/status", timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        state.pty.close_all()

    assert exc_info.value.code == 401


def test_harnessd_auth_status_unknown_harness_returns_404(tmp_path):
    server, state, base_url = _start_test_server(tmp_path, api_token="secret")
    state.auth = AuthFlowManager({})
    try:
        req = urllib.request.Request(
            f"{base_url}/auth/unknown/status",
            headers={"Authorization": "Bearer secret"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        state.pty.close_all()

    assert exc_info.value.code == 404


def test_harnessd_auth_status_unavailable_returns_404(tmp_path):
    server, state, base_url = _start_test_server(tmp_path, api_token="secret")
    state.auth = AuthFlowManager({"gemini": StaticAuthAdapter("gemini")})
    try:
        req = urllib.request.Request(
            f"{base_url}/auth/gemini/status",
            headers={"Authorization": "Bearer secret"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        body = json.loads(exc_info.value.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        state.pty.close_all()

    assert exc_info.value.code == 404
    assert body["host_id"] == "test-host"
    assert body["harness"] == "gemini"
    assert body["state"] == "unavailable"


def test_harnessd_auth_status_decodes_harness_path(tmp_path):
    server, state, base_url = _start_test_server(tmp_path, api_token="secret")
    state.auth = AuthFlowManager(
        {
            "provider/test": StaticAuthAdapter(
                "provider/test",
                status_value=HarnessAuthStatus("provider/test", "unauthenticated"),
            )
        }
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/auth/provider%2Ftest/status",
            headers={"Authorization": "Bearer secret"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        state.pty.close_all()

    assert response.status == 200
    assert body["harness"] == "provider/test"


def test_harnessd_auth_flow_unknown_id_returns_404(tmp_path):
    server, state, base_url = _start_test_server(tmp_path, api_token="secret")
    state.auth = AuthFlowManager(
        {
            "codex": StaticAuthAdapter(
                "codex",
                status_value=HarnessAuthStatus("codex", "unauthenticated"),
            )
        }
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/auth/codex/flows/missing",
            headers={"Authorization": "Bearer secret"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        state.pty.close_all()

    assert exc_info.value.code == 404


def test_harnessd_auth_start_unsupported_returns_400(tmp_path):
    server, state, base_url = _start_test_server(tmp_path, api_token="secret")
    state.auth = AuthFlowManager({"gemini": StaticAuthAdapter("gemini")})
    try:
        req = urllib.request.Request(
            f"{base_url}/auth/gemini/start",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        state.pty.close_all()

    assert exc_info.value.code == 400


def test_harnessd_auth_start_launch_failure_returns_structured_500(tmp_path):
    server, state, base_url = _start_test_server(tmp_path, api_token="secret")
    state.auth = AuthFlowManager(
        {"codex": StaticAuthAdapter("codex", start_command=["missing-cli"])}
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/auth/codex/start",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        body = json.loads(exc_info.value.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        state.pty.close_all()

    assert exc_info.value.code == 500
    assert body == {"error": "authentication command could not start"}


def test_resolve_harness_presets_enables_available_login_shell_clis(
    monkeypatch, tmp_path
):
    class _Completed:
        def __init__(self, *, returncode: int, stdout: str):
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(argv, **kwargs):
        command = argv[-1]
        if command.endswith("codex"):
            return _Completed(returncode=0, stdout="/opt/homebrew/bin/codex\n")
        return _Completed(returncode=1, stdout="")

    monkeypatch.setattr("drover.server.harness.auth.subprocess.run", fake_run)
    monkeypatch.setattr("drover.server.harness.auth.Path.home", lambda: tmp_path)
    presets = resolve_harness_presets(
        {
            "shell": DEFAULT_PRESETS["shell"],
            "codex": DEFAULT_PRESETS["codex"],
            "gemini": DEFAULT_PRESETS["gemini"],
        },
        shell="/bin/zsh",
    )

    assert presets["shell"].enabled is True
    assert presets["codex"].enabled is True
    assert presets["codex"].command == (
        "/bin/zsh",
        "-lc",
        "exec /opt/homebrew/bin/codex",
    )
    assert presets["gemini"].enabled is False
    assert "not found" in presets["gemini"].description


def test_resolve_harness_presets_discovers_nvm_clis_and_preserves_node_path(
    monkeypatch, tmp_path
):
    class _Completed:
        returncode = 1
        stdout = ""

    nvm_bin = tmp_path / ".nvm/versions/node/v24.13.0/bin"
    nvm_bin.mkdir(parents=True)
    codex = nvm_bin / "codex"
    codex.write_text("#!/usr/bin/env node\n")
    codex.chmod(0o755)

    monkeypatch.setattr("drover.server.harness.auth.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "drover.server.harness.auth.subprocess.run",
        lambda *args, **kwargs: _Completed(),
    )

    presets = resolve_harness_presets(
        {"codex": DEFAULT_PRESETS["codex"]},
        shell="/bin/zsh",
    )

    assert presets["codex"].enabled is True
    assert presets["codex"].command == (
        "/bin/zsh",
        "-lc",
        f"export PATH={nvm_bin}:$PATH; exec {codex}",
    )


def test_resolve_harness_presets_enables_versioned_claude_cli(monkeypatch, tmp_path):
    class _Completed:
        returncode = 1
        stdout = ""

    versions_dir = tmp_path / ".local/share/claude/versions"
    versions_dir.mkdir(parents=True)
    older = versions_dir / "2.1.183"
    newer = versions_dir / "2.1.185"
    older.write_text("")
    newer.write_text("")
    older.chmod(0o755)
    newer.chmod(0o755)

    monkeypatch.setattr("drover.server.harness.auth.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "drover.server.harness.auth.subprocess.run",
        lambda *args, **kwargs: _Completed(),
    )

    presets = resolve_harness_presets(
        {
            "shell": DEFAULT_PRESETS["shell"],
            "claude-code": DEFAULT_PRESETS["claude-code"],
        },
        shell="/bin/zsh",
    )

    assert presets["claude-code"].enabled is True
    assert presets["claude-code"].command == (
        "/bin/zsh",
        "-lc",
        f"exec {newer}",
    )
    assert f"available at {newer}" in presets["claude-code"].description


def test_build_launch_command_adds_provider_native_resume_args():
    claude = HarnessPreset(
        name="claude-code",
        command=("/bin/zsh", "-lc", "exec /Users/arnabmac/.local/bin/claude"),
        enabled=True,
        description="Claude Code",
    )
    codex = HarnessPreset(
        name="codex",
        command=("/bin/zsh", "-lc", "exec /opt/homebrew/bin/codex"),
        enabled=True,
        description="Codex",
    )
    gemini = HarnessPreset(
        name="gemini",
        command=("/bin/zsh", "-lc", "exec /opt/homebrew/bin/gemini"),
        enabled=True,
        description="Gemini",
    )

    assert build_launch_command(
        claude,
        harness="claude-code",
        native_resume={"session_id": "claude-session-1"},
    ) == [
        "/bin/zsh",
        "-lc",
        "exec /Users/arnabmac/.local/bin/claude --resume claude-session-1",
    ]
    assert build_launch_command(
        claude,
        harness="claude-code",
        native_resume={"latest": True},
    ) == ["/bin/zsh", "-lc", "exec /Users/arnabmac/.local/bin/claude --continue"]
    assert build_launch_command(
        codex,
        harness="codex",
        native_resume={"latest": True},
    ) == ["/bin/zsh", "-lc", "exec /opt/homebrew/bin/codex resume --last"]
    assert build_launch_command(
        codex,
        harness="codex",
        native_resume={"session_id": "codex-session-1"},
    ) == ["/bin/zsh", "-lc", "exec /opt/homebrew/bin/codex resume codex-session-1"]
    assert build_launch_command(
        gemini,
        harness="gemini",
        native_resume={"session_id": "gemini-session-1"},
    ) == [
        "/bin/zsh",
        "-lc",
        "exec /opt/homebrew/bin/gemini --resume gemini-session-1",
    ]


def test_build_launch_command_preserves_plain_command_without_resume():
    preset = HarnessPreset(
        name="shell",
        command=("/bin/sh",),
        enabled=True,
        description="Shell",
    )

    assert build_launch_command(preset, harness="shell") == ["/bin/sh"]


def test_discovers_claude_native_resume_sessions(tmp_path):
    session_dir = tmp_path / ".claude/projects/-home-Arnab-dev-nexus"
    session_dir.mkdir(parents=True)
    session = session_dir / "claude-session-1.jsonl"
    session.write_text(
        "\n".join(
            [
                json.dumps({"type": "last-prompt", "sessionId": "claude-session-1"}),
                json.dumps(
                    {
                        "type": "user",
                        "cwd": "/home/Arnab/dev/nexus",
                        "timestamp": "2026-06-23T01:00:00Z",
                    }
                ),
            ]
        )
    )

    candidates = discover_native_resume_sessions(
        home=tmp_path,
        harness="claude-code",
        cwd="/home/Arnab/dev/nexus",
    )

    assert candidates == [
        {
            "cwd": "/home/Arnab/dev/nexus",
            "harness": "claude-code",
            "label": "nexus · claude-s",
            "native_resume": {
                "label": "nexus · claude-s",
                "session_id": "claude-session-1",
            },
            "path_hint": str(session),
            "session_id": "claude-session-1",
            "source": "claude jsonl",
            "updated_at": candidates[0]["updated_at"],
        }
    ]


def test_reads_claude_jsonl_native_transcript(tmp_path):
    session_dir = tmp_path / ".claude/projects/-home-Arnab-dev-nexus"
    session_dir.mkdir(parents=True)
    session = session_dir / "claude-session-1.jsonl"
    session.write_text(
        "\n".join(
            [
                json.dumps({"type": "last-prompt", "sessionId": "claude-session-1"}),
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "claude-session-1",
                        "cwd": "/home/Arnab/dev/nexus",
                        "timestamp": "2026-06-23T01:00:00Z",
                        "uuid": "user-1",
                        "message": {
                            "role": "user",
                            "content": "Summarise this project.",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "claude-session-1",
                        "cwd": "/home/Arnab/dev/nexus",
                        "timestamp": "2026-06-23T01:00:01Z",
                        "uuid": "assistant-1",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "I will inspect it."},
                                {
                                    "type": "tool_use",
                                    "name": "Bash",
                                    "input": {
                                        "description": "Inspect repo",
                                        "command": "ls -1",
                                    },
                                },
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "claude-session-1",
                        "cwd": "/home/Arnab/dev/nexus",
                        "timestamp": "2026-06-23T01:00:02Z",
                        "uuid": "tool-result-1",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool-1",
                                    "content": "README.md\nsrc\n",
                                }
                            ],
                        },
                    }
                ),
            ]
        )
    )

    transcript = native_transcript_for_session(
        home=tmp_path,
        harness="claude-code",
        cwd="/home/Arnab/dev/nexus",
    )

    assert transcript["source"] == "claude jsonl"
    assert transcript["session_id"] == "claude-session-1"
    assert [item["role"] for item in transcript["messages"]] == [
        "user",
        "assistant",
        "tool_use",
        "tool_result",
    ]
    assert transcript["messages"][0]["text"] == "Summarise this project."
    assert transcript["messages"][1]["text"] == "I will inspect it."
    assert transcript["messages"][2]["title"] == "Tool: Bash"
    assert "```sh\nls -1\n```" in transcript["messages"][2]["text"]
    assert "README.md" in transcript["messages"][3]["text"]


def test_reads_codex_jsonl_native_transcript(tmp_path):
    session_dir = tmp_path / ".codex/sessions/2026/06/23"
    session_dir.mkdir(parents=True)
    session_id = "019ef2b6-7000-79c3-93c6-039d129b9513"
    session = session_dir / f"rollout-2026-06-23T01-00-00-{session_id}.jsonl"
    session.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-06-23T01:00:00Z",
                        "payload": {
                            "id": session_id,
                            "cwd": "/Users/arnabmac/jenny/nexus",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "timestamp": "2026-06-23T01:00:01Z",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "Check status."}
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "timestamp": "2026-06-23T01:00:02Z",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "I will inspect git."}
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "timestamp": "2026-06-23T01:00:03Z",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "call-1",
                            "arguments": json.dumps({"cmd": "git status --short"}),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "timestamp": "2026-06-23T01:00:04Z",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": "M src/drover/server/metrics.py\n",
                        },
                    }
                ),
            ]
        )
    )

    transcript = native_transcript_for_session(
        home=tmp_path,
        harness="codex",
        cwd="/Users/arnabmac/jenny/nexus",
    )

    assert transcript["source"] == "codex jsonl"
    assert transcript["session_id"] == session_id
    assert [item["role"] for item in transcript["messages"]] == [
        "user",
        "assistant",
        "tool_use",
        "tool_result",
    ]
    assert transcript["messages"][0]["text"] == "Check status."
    assert transcript["messages"][2]["title"] == "Tool: exec_command"
    assert "```sh\ngit status --short\n```" in transcript["messages"][2]["text"]
    assert "metrics.py" in transcript["messages"][3]["text"]


def test_reads_gemini_json_native_transcript(tmp_path):
    project_dir = tmp_path / ".gemini/tmp/nexus"
    chats_dir = project_dir / "chats"
    chats_dir.mkdir(parents=True)
    (project_dir / ".project_root").write_text("/home/Arnab/dev/nexus\n")
    session = chats_dir / "session-2026-06-23T01-00-abcd1234.json"
    session.write_text(
        json.dumps(
            {
                "sessionId": "gemini-session-1",
                "lastUpdated": "2026-06-23T01:03:00Z",
                "messages": [
                    {
                        "id": "user-1",
                        "timestamp": "2026-06-23T01:00:00Z",
                        "type": "user",
                        "content": [{"text": "Explain this repo."}],
                    },
                    {
                        "id": "assistant-1",
                        "timestamp": "2026-06-23T01:00:01Z",
                        "type": "gemini",
                        "content": "I will read the README.",
                        "toolCalls": [
                            {
                                "id": "tool-1",
                                "name": "read_file",
                                "args": {"file_path": "README.md"},
                                "result": [
                                    {
                                        "functionResponse": {
                                            "response": {
                                                "output": "# Nexus\n\nLocal context store."
                                            }
                                        }
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        )
    )

    transcript = native_transcript_for_session(
        home=tmp_path,
        harness="gemini",
        cwd="/home/Arnab/dev/nexus",
    )

    assert transcript["source"] == "gemini chat"
    assert transcript["session_id"] == "gemini-session-1"
    assert [item["role"] for item in transcript["messages"]] == [
        "user",
        "assistant",
        "tool_use",
        "tool_result",
    ]
    assert transcript["messages"][0]["text"] == "Explain this repo."
    assert transcript["messages"][2]["title"] == "Tool: read_file"
    assert '"file_path": "README.md"' in transcript["messages"][2]["text"]
    assert "Local context store." in transcript["messages"][3]["text"]


def test_discovers_codex_native_resume_sessions(tmp_path):
    session_dir = tmp_path / ".codex/sessions/2026/06/23"
    session_dir.mkdir(parents=True)
    session_id = "019ef2b6-7000-79c3-93c6-039d129b9513"
    session = session_dir / f"rollout-2026-06-23T01-00-00-{session_id}.jsonl"
    session.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {}}),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "cwd": "/Users/arnabmac/jenny/nexus",
                            "type": "task_started",
                        },
                    }
                ),
            ]
        )
    )

    candidates = discover_native_resume_sessions(
        home=tmp_path,
        harness="codex",
        cwd="/Users/arnabmac/jenny/nexus",
    )

    assert len(candidates) == 1
    assert candidates[0]["harness"] == "codex"
    assert candidates[0]["session_id"] == session_id
    assert candidates[0]["native_resume"]["session_id"] == session_id
    assert candidates[0]["cwd"] == "/Users/arnabmac/jenny/nexus"
    assert candidates[0]["path_hint"] == str(session)


def test_discovers_gemini_native_resume_sessions(tmp_path):
    project_dir = tmp_path / ".gemini/tmp/nexus"
    chats_dir = project_dir / "chats"
    chats_dir.mkdir(parents=True)
    (project_dir / ".project_root").write_text("/home/Arnab/dev/nexus\n")
    session = chats_dir / "session-2026-06-23T01-00-abcd1234.json"
    session.write_text(
        json.dumps(
            {
                "sessionId": "gemini-session-1",
                "lastUpdated": "2026-06-23T01:01:00Z",
                "kind": "main",
            }
        )
    )

    candidates = discover_native_resume_sessions(
        home=tmp_path,
        harness="gemini",
        cwd="/home/Arnab/dev/nexus",
    )

    assert len(candidates) == 1
    assert candidates[0]["harness"] == "gemini"
    assert candidates[0]["session_id"] == "gemini-session-1"
    assert candidates[0]["native_resume"]["session_id"] == "gemini-session-1"
    assert candidates[0]["cwd"] == "/home/Arnab/dev/nexus"
    assert candidates[0]["path_hint"] == str(session)


def test_harnessd_creates_shell_session(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        status, payload = _json_request(
            f"{base_url}/sessions",
            payload={"harness": "shell", "cwd": str(tmp_path)},
        )
        assert status == 201
        assert payload["status"] == "running"
        assert payload["pid"] > 0

        session = state.registry.get_session(payload["session_id"])
        assert session is not None
        assert session.host_id == "test-host"
        assert session.harness == "shell"
        assert session.status == "running"
        events = state.registry.list_events(session.session_id)
        assert events[0].event_type == "session.started"
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_harnessd_lists_and_gets_live_sessions(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        _, created = _json_request(
            f"{base_url}/sessions",
            payload={"harness": "shell", "cwd": str(tmp_path)},
        )

        status, inventory = _json_request(f"{base_url}/sessions")
        assert status == 200
        assert inventory["host_id"] == "test-host"
        assert [item["session_id"] for item in inventory["sessions"]] == [
            created["session_id"]
        ]
        assert inventory["sessions"][0]["status"] == "running"

        _, session = _json_request(f"{base_url}/sessions/{created['session_id']}")
        assert session["session_id"] == created["session_id"]
        assert session["pid"] == created["pid"]
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_harnessd_reconciles_exited_sessions_out_of_live_inventory(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    state.presets = {
        **state.presets,
        "quick": HarnessPreset(
            name="quick",
            command=("/bin/sh", "-lc", "true"),
            enabled=True,
            description="exits immediately",
        ),
    }
    try:
        _, created = _json_request(
            f"{base_url}/sessions",
            payload={"harness": "quick", "cwd": str(tmp_path)},
        )
        deadline = time.time() + 3
        inventory = {"sessions": [{"session_id": created["session_id"]}]}
        while time.time() < deadline and inventory["sessions"]:
            _, inventory = _json_request(f"{base_url}/sessions")
            time.sleep(0.05)

        assert inventory["sessions"] == []
        session = state.registry.get_session(created["session_id"])
        assert session is not None
        assert session.status == "completed"
        assert session.ended_at is not None
        events = [
            event.event_type for event in state.registry.list_events(session.session_id)
        ]
        assert "session.exited" in events
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_harnessd_reconciles_orphaned_structured_sessions_on_startup(tmp_path):
    # Simulate a killed daemon process: a structured session row left
    # "running" in the registry with no live driver behind it anymore
    # (the in-memory StructuredSessionManager that owned it died with the
    # old process). create_harness_server must finalize it as errored
    # before serving a single request, AND the reconciled row must still
    # show up in GET /sessions (it has no manager entry, so it only
    # appears via the registry-merge path in _list_sessions).
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(host_id="test-host", display_name="Test Host", kind="linux")
    registry.create_session(
        host_id="test-host",
        harness="claude-code",
        command="claude",
        session_id="harness-orphan",
        status="running",
        mode="structured",
    )
    state = HarnessDaemonState(
        host_id="test-host",
        display_name="Test Host",
        kind="linux",
        registry=registry,
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        local_url="http://127.0.0.1:0",
    )
    server = create_harness_server(listen_host="127.0.0.1", listen_port=0, state=state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        session = registry.get_session("harness-orphan")
        assert session is not None
        assert session.status == "errored"
        assert session.last_error == "daemon restarted; structured session lost"
        assert session.ended_at is not None

        host, port = server.server_address
        status, inventory = _json_request(f"http://{host}:{port}/sessions")
        assert status == 200
        by_id = {item["session_id"]: item for item in inventory["sessions"]}
        assert by_id["harness-orphan"]["status"] == "errored"
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_harnessd_reconciles_orphaned_pty_sessions_on_startup(tmp_path):
    # Simulate a killed daemon process: PTY-mode rows left "running" (or
    # created/starting) in the registry with no live PTY behind them anymore
    # (the PtySessionManager that owned them died with the old process; the
    # fresh one is always empty at boot). create_harness_server must finalize
    # them as completed before serving a single request, while leaving other
    # hosts' rows alone.
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(host_id="test-host", display_name="Test Host", kind="linux")
    registry.register_host(host_id="other-host", display_name="Other", kind="linux")
    registry.create_session(
        host_id="test-host",
        harness="shell",
        command="/bin/sh",
        session_id="harness-pty-orphan",
        status="running",
        mode="pty",
    )
    registry.create_session(
        host_id="test-host",
        harness="claude-code",
        command="claude",
        session_id="harness-pty-orphan-starting",
        status="starting",
        mode="pty",
    )
    registry.create_session(
        host_id="test-host",
        harness="shell",
        command="/bin/sh",
        session_id="harness-pty-done",
        status="completed",
        mode="pty",
    )
    registry.create_session(
        host_id="other-host",
        harness="shell",
        command="/bin/sh",
        session_id="harness-pty-elsewhere",
        status="running",
        mode="pty",
    )
    state = HarnessDaemonState(
        host_id="test-host",
        display_name="Test Host",
        kind="linux",
        registry=registry,
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        local_url="http://127.0.0.1:0",
    )
    server = create_harness_server(listen_host="127.0.0.1", listen_port=0, state=state)
    try:
        for session_id in ("harness-pty-orphan", "harness-pty-orphan-starting"):
            session = registry.get_session(session_id)
            assert session is not None
            assert session.status == "completed"
            assert session.last_error == "daemon restarted; PTY session lost"
            assert session.ended_at is not None

        done = registry.get_session("harness-pty-done")
        assert done is not None
        assert done.status == "completed"
        assert done.last_error is None

        elsewhere = registry.get_session("harness-pty-elsewhere")
        assert elsewhere is not None
        assert elsewhere.status == "running"
        assert elsewhere.ended_at is None
    finally:
        state.pty.close_all()
        server.server_close()


def test_harnessd_terminates_live_session_and_updates_registry(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        _, created = _json_request(
            f"{base_url}/sessions",
            payload={"harness": "shell", "cwd": str(tmp_path)},
        )
        status, terminated = _json_request(
            f"{base_url}/sessions/{created['session_id']}/terminate",
            payload={},
        )

        assert status == 200
        assert terminated["terminated"] is True
        assert state.pty.get(created["session_id"]) is None
        session = state.registry.get_session(created["session_id"])
        assert session is not None
        assert session.status == "terminated"
        events = [
            event.event_type for event in state.registry.list_events(session.session_id)
        ]
        assert "session.terminated" in events

        _, inventory = _json_request(f"{base_url}/sessions")
        assert inventory["sessions"] == []
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_harnessd_delete_terminates_live_session(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        _, created = _json_request(
            f"{base_url}/sessions",
            payload={"harness": "shell", "cwd": str(tmp_path)},
        )
        with _raw_request(
            f"{base_url}/sessions/{created['session_id']}", method="DELETE"
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload["status"] == "terminated"
        assert state.registry.get_session(created["session_id"]).status == "terminated"
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_harnessd_sessions_cors_preflight_and_post_headers(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        with _raw_request(f"{base_url}/sessions", method="OPTIONS") as response:
            assert response.status == 204
            assert response.headers["Access-Control-Allow-Origin"] == "*"
            assert "POST" in response.headers["Access-Control-Allow-Methods"]
            assert "DELETE" in response.headers["Access-Control-Allow-Methods"]

        with _raw_request(
            f"{base_url}/sessions",
            method="POST",
            payload={"harness": "shell", "cwd": str(tmp_path)},
        ) as response:
            assert response.status == 201
            assert response.headers["Access-Control-Allow-Origin"] == "*"
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_harnessd_creates_in_memory_session_when_registry_is_locked(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    state.registry = _FailingRegistry()
    try:
        status, payload = _json_request(
            f"{base_url}/sessions",
            payload={"harness": "shell", "cwd": str(tmp_path)},
        )

        assert status == 201
        assert payload["status"] == "running"
        assert payload["host_id"] == "test-host"
        assert payload["registry_synced"] is False
        assert state.pty.get(payload["session_id"]) is not None
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_harnessd_rejects_disabled_harness(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        try:
            _json_request(f"{base_url}/sessions", payload={"harness": "codex"})
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            payload = json.loads(exc.read().decode("utf-8"))
            assert "not enabled" in payload["error"]
        else:
            raise AssertionError("codex launch should fail until preset is enabled")
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_harnessd_launches_enabled_cli_preset(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    state.presets = {
        **state.presets,
        "codex": HarnessPreset(
            name="codex",
            command=("/bin/sh", "-lc", "printf CODEX_OK; sleep 1"),
            enabled=True,
            description="Codex test shim",
        ),
    }
    try:
        status, payload = _json_request(
            f"{base_url}/sessions",
            payload={"harness": "codex", "cwd": str(tmp_path)},
        )

        assert status == 201
        assert payload["status"] == "running"
        assert payload["harness"] == "codex"
        assert state.registry.get_session(payload["session_id"]).harness == "codex"
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_daemon_rejects_unauthenticated_when_token_set(tmp_path):
    server, state, base_url = _start_test_server(tmp_path, api_token="host-secret")
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/sessions", timeout=5)
        assert exc_info.value.code == 401
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_daemon_accepts_bearer(tmp_path):
    server, state, base_url = _start_test_server(tmp_path, api_token="host-secret")
    try:
        request = urllib.request.Request(
            f"{base_url}/sessions",
            headers={"Authorization": "Bearer host-secret"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_daemon_healthz_open(tmp_path):
    server, state, base_url = _start_test_server(tmp_path, api_token="host-secret")
    try:
        with urllib.request.urlopen(f"{base_url}/healthz", timeout=5) as response:
            assert response.status == 200
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_daemon_terminal_attach_rejects_unauthenticated(tmp_path):
    server, state, base_url = _start_test_server(tmp_path, api_token="host-secret")
    session_id = "harness-attach-auth"
    state.pty.start(session_id=session_id, command="/bin/sh", cwd=tmp_path)
    try:
        host_port = base_url.removeprefix("http://")
        host, port = host_port.split(":", 1)
        sock = socket.create_connection((host, int(port)), timeout=5)
        try:
            try:
                client_handshake(
                    sock, host=host_port, path=f"/sessions/{session_id}/terminal"
                )
            except RuntimeError as exc:
                assert "401" in str(exc)
            else:
                raise AssertionError("unauthenticated terminal attach should fail")
        finally:
            sock.close()
    finally:
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def _close_structured_sessions(state) -> None:
    for session_id in list(state.structured.session_ids()):
        state.structured.close(session_id)


def test_structured_session_full_lifecycle(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        status, body = _json_request(
            f"{base_url}/sessions",
            payload={
                "harness": "claude-code",
                "mode": "structured",
                "prompt": "list files",
                "command": FAKE_STRUCTURED_CLI,
                "cwd": str(tmp_path),
            },
        )
        assert status == 201
        assert body["mode"] == "structured"
        sid = body["session_id"]

        _wait_until(lambda: _fetch_session(base_url, sid)["awaiting"] == "approval")

        try:
            _json_request(f"{base_url}/sessions/{sid}/turns", payload={"text": "more"})
        except urllib.error.HTTPError as exc:
            assert exc.code == 409  # approval pending blocks new turns
        else:
            raise AssertionError("turn during pending approval should be rejected")

        status, _ = _json_request(
            f"{base_url}/sessions/{sid}/permission",
            payload={"request_id": "req-1", "decision": "allow"},
        )
        assert status == 200

        _wait_until(lambda: _fetch_session(base_url, sid)["awaiting"] == "input")

        listing = _fetch_session(base_url, sid)
        assert listing["mode"] == "structured"
        assert listing["last_activity"] is not None

        _, inventory = _json_request(f"{base_url}/sessions")
        assert sid in {item["session_id"] for item in inventory["sessions"]}
        structured_item = next(
            item for item in inventory["sessions"] if item["session_id"] == sid
        )
        assert structured_item["mode"] == "structured"

        session = state.registry.get_session(sid)
        assert session is not None
        assert session.mode == "structured"
        event_types = [event.event_type for event in state.registry.list_events(sid)]
        assert "approval_prompt" in event_types
        assert "approval_response" in event_types
    finally:
        _close_structured_sessions(state)
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_structured_turn_appends_user_input_and_seq_is_monotonic(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        status, body = _json_request(
            f"{base_url}/sessions",
            payload={
                "harness": "claude-code",
                "mode": "structured",
                "prompt": "first turn",
                "command": FAKE_STRUCTURED_CLI,
                "cwd": str(tmp_path),
            },
        )
        assert status == 201
        sid = body["session_id"]

        _wait_until(lambda: _fetch_session(base_url, sid)["awaiting"] == "approval")
        _json_request(
            f"{base_url}/sessions/{sid}/permission",
            payload={"request_id": "req-1", "decision": "allow"},
        )
        _wait_until(lambda: _fetch_session(base_url, sid)["awaiting"] == "input")

        status, turn_body = _json_request(
            f"{base_url}/sessions/{sid}/turns", payload={"text": "second turn"}
        )
        assert status == 202
        assert turn_body["turn_id"]

        def _has_second_turn_user_input() -> bool:
            events = state.registry.list_events(sid)
            return any(
                event.event_type == "user_input"
                and event.payload.get("text") == "second turn"
                for event in events
            )

        _wait_until(_has_second_turn_user_input)

        events = state.registry.list_events(sid)
        user_input_events = [
            event for event in events if event.event_type == "user_input"
        ]
        # One user_input event for the initial "prompt" turn plus one for the
        # explicit /turns call made above.
        assert len(user_input_events) == 2
        assert user_input_events[-1].payload["text"] == "second turn"

        seqs = [event.seq for event in events if event.seq is not None]
        assert seqs == list(range(1, len(seqs) + 1))
    finally:
        _close_structured_sessions(state)
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_structured_unknown_harness_rejected(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        try:
            _json_request(
                f"{base_url}/sessions",
                payload={"harness": "shell", "mode": "structured"},
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            payload = json.loads(exc.read().decode("utf-8"))
            assert "shell" in payload["error"]
        else:
            raise AssertionError("unknown structured harness should be rejected")
    finally:
        _close_structured_sessions(state)
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_structured_permission_without_approval_channel_returns_400(tmp_path):
    # codex/gemini drivers always raise RuntimeError from answer_permission
    # (no wire-level approval channel) -- the daemon must surface that as a
    # 400, not a 500, and must not record a phantom approval_response event.
    server, state, base_url = _start_test_server(tmp_path)
    try:
        codex_cli = [
            sys.executable,
            "-c",
            "import sys\nfor line in sys.stdin:\n    pass\n",
        ]
        status, body = _json_request(
            f"{base_url}/sessions",
            payload={
                "harness": "codex",
                "mode": "structured",
                "command": codex_cli,
                "cwd": str(tmp_path),
            },
        )
        assert status == 201
        sid = body["session_id"]

        try:
            _json_request(
                f"{base_url}/sessions/{sid}/permission",
                payload={"request_id": "req-1", "decision": "allow"},
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            payload = json.loads(exc.read().decode("utf-8"))
            assert "approval" in payload["error"]
        else:
            raise AssertionError("codex has no approval channel; expected 400")

        assert not any(
            event.event_type == "approval_response"
            for event in state.registry.list_events(sid)
        )
    finally:
        _close_structured_sessions(state)
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_structured_interrupt_unknown_session_is_404(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        try:
            _json_request(f"{base_url}/sessions/does-not-exist/interrupt", payload={})
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("interrupting an unknown session should 404")
    finally:
        _close_structured_sessions(state)
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_structured_terminate_closes_driver_and_marks_terminated(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        status, body = _json_request(
            f"{base_url}/sessions",
            payload={
                "harness": "claude-code",
                "mode": "structured",
                "command": FAKE_STRUCTURED_CLI,
                "cwd": str(tmp_path),
            },
        )
        assert status == 201
        sid = body["session_id"]
        _wait_until(lambda: state.structured.has(sid))

        status, terminated = _json_request(
            f"{base_url}/sessions/{sid}/terminate", payload={}
        )
        assert status == 200
        assert terminated["terminated"] is True
        assert not state.structured.has(sid)

        session = state.registry.get_session(sid)
        assert session is not None
        assert session.status == "terminated"

        try:
            _json_request(f"{base_url}/sessions/{sid}/turns", payload={"text": "x"})
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("turns after terminate should 404")
    finally:
        _close_structured_sessions(state)
        state.pty.close_all()
        server.shutdown()
        server.server_close()


# -- per-session worktrees for approval-less harnesses (codex/gemini) --------


def _init_git_repo(root) -> None:
    root.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True
        )
    (root / "file.txt").write_text("hello\n")
    subprocess.run(
        ["git", "-C", str(root), "add", "file.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )


def test_structured_codex_session_runs_in_worktree(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    try:
        # CodexDriver spawns nothing until the first turn, so the command is
        # never executed here -- the session only needs to exist.
        status, body = _json_request(
            f"{base_url}/sessions",
            payload={
                "harness": "codex",
                "mode": "structured",
                "command": ["codex-never-invoked"],
                "cwd": str(repo),
            },
        )
        assert status == 201
        sid = body["session_id"]

        worktree_path = tmp_path / "worktrees" / sid
        assert (worktree_path / "file.txt").is_file()
        session = state.registry.get_session(sid)
        assert session is not None
        assert session.cwd == str(worktree_path)

        started = next(
            event
            for event in state.registry.list_events(sid)
            if event.event_type == "session.started"
        )
        assert started.payload["worktree"]["path"] == str(worktree_path)
        assert started.payload["worktree"]["branch"] == f"drover/{sid}"

        # Terminating an untouched session reclaims the worktree and branch.
        status, _ = _json_request(f"{base_url}/sessions/{sid}/terminate", payload={})
        assert status == 200
        assert not worktree_path.exists()
        branches = subprocess.run(
            ["git", "-C", str(repo), "branch", "--list", f"drover/{sid}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert branches == ""
    finally:
        _close_structured_sessions(state)
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_structured_codex_session_non_git_cwd_runs_in_place(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    try:
        status, body = _json_request(
            f"{base_url}/sessions",
            payload={
                "harness": "codex",
                "mode": "structured",
                "command": ["codex-never-invoked"],
                "cwd": str(plain),
            },
        )
        assert status == 201
        sid = body["session_id"]
        session = state.registry.get_session(sid)
        assert session is not None
        assert session.cwd == str(plain)
        assert not (tmp_path / "worktrees" / sid).exists()
    finally:
        _close_structured_sessions(state)
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_structured_claude_session_stays_in_place(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    try:
        status, body = _json_request(
            f"{base_url}/sessions",
            payload={
                "harness": "claude-code",
                "mode": "structured",
                "command": FAKE_STRUCTURED_CLI,
                "cwd": str(repo),
            },
        )
        assert status == 201
        sid = body["session_id"]
        session = state.registry.get_session(sid)
        assert session is not None
        assert session.cwd == str(repo)
        assert not (tmp_path / "worktrees" / sid).exists()
    finally:
        _close_structured_sessions(state)
        state.pty.close_all()
        server.shutdown()
        server.server_close()
