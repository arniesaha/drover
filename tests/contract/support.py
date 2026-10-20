"""Black-box harnessd contract test support.

Nothing in this package imports the daemon.  The process is deliberately
launched exactly as a shipped harnessd is, making this suite usable by the
Rust replacement without a compatibility shim.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from drover.server.harness.websocket import accept_key, recv_json, send_json


@dataclass
class FakeHub:
    server: ThreadingHTTPServer
    requests: list[dict] = field(default_factory=list)
    responses: dict[str, list[tuple[int, dict]]] = field(default_factory=dict)
    relay_connections: list[object] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def script(self, path: str, status: int, body: dict) -> None:
        self.responses.setdefault(path, []).append((status, body))

    def relay(self, timeout: float = 5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.relay_connections and any(
                item["path"] == "/harness/relay" for item in self.requests
            ):
                return self.relay_connections[-1]
            time.sleep(0.02)
        raise AssertionError("daemon did not send relay hello")


@pytest.fixture
def fake_deepseek():
    """Small DSH Web-RPC double shared by Python and Rust contract lanes."""

    sessions: dict[str, list[dict]] = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request = json.loads(raw or b"{}")
            method = request.get("method")
            payload = request.get("payload") or {}
            session_id = payload.get("sessionId") or "dsh-fixture-1"
            if method == "session.create":
                sessions.setdefault("dsh-fixture-1", [])
                value = {"sessionId": "dsh-fixture-1"}
            elif method == "session.prompt":
                sessions[session_id] = [
                    {
                        "event": {
                            "type": "assistant/message",
                            "seq": 1,
                            "surfaceOp": "append",
                            "data": {
                                "message": {
                                    "content": [
                                        {"type": "text", "text": "deepseek fixture"}
                                    ]
                                },
                                "usage": {"input_tokens": 3, "output_tokens": 2},
                            },
                        },
                        "view": {"fixture": True},
                    },
                    {
                        "event": {
                            "type": "tool/call",
                            "seq": 2,
                            "data": {"callId": "call-1", "name": "Read", "arguments": {"path": "README.md"}},
                        },
                        "view": {"fixture": True},
                    },
                    {
                        "event": {
                            "type": "tool/result",
                            "seq": 3,
                            "data": {"message": {"content": [{"type": "tool-result", "toolCallId": "call-1", "content": [{"type": "text", "text": "fixture read"}]}]}},
                        },
                        "view": {"fixture": True},
                    },
                    {"event": {"type": "turn/end", "seq": 4, "data": {"reason": {"kind": "completed"}}}},
                ]
                value = {}
            elif method == "session.history":
                value = {"events": sessions.get(session_id, []), "hasMore": False}
            elif method in {"session.cancel", "session.selectModel"}:
                value = {}
            elif method == "session.models":
                value = {"current": {"provider": "deepseek", "model": "fixture"}}
            else:
                value = {}
            body = json.dumps(
                {"rpcId": request.get("rpcId"), "result": {"ok": True, "value": value}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def fake_hub():
    hub: FakeHub

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            if self.path != "/harness/relay":
                self.send_error(404)
                return
            key = self.headers.get("Sec-WebSocket-Key")
            if not key or self.headers.get("Authorization") != "Bearer contract-token":
                self.send_error(401)
                return
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept_key(key))
            self.end_headers()
            hub.relay_connections.append(self.connection)
            try:
                hello = recv_json(self.connection)
                hub.requests.append(
                    {
                        "path": self.path,
                        "body": hello,
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                while True:
                    time.sleep(0.1)
            except Exception:
                return

        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw or b"{}")
            hub.requests.append(
                {
                    "path": self.path,
                    "body": body,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            status, response = (hub.responses.get(self.path) or [(200, {})]).pop(0)
            data = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    hub = FakeHub(server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield hub
    finally:
        server.shutdown()
        server.server_close()


def _fake_cli(path: Path, fixture: Path, log: Path) -> None:
    path.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        " print('contract-cli 1.0')\n"
        " raise SystemExit(0)\n"
        "log = os.environ['HARNESSD_FAKE_CLI_LOG']\n"
        "with open(log, 'a') as out:\n"
        " out.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "if '--device-auth' in sys.argv:\n"
        " print('Open https://example.test/device and enter ABCD-EFGH', flush=True)\n"
        " time.sleep(60)\n"
        " raise SystemExit(0)\n"
        "if 'auth' in sys.argv and 'status' in sys.argv:\n"
        " logged_in = Path(os.environ['HARNESSD_FAKE_AUTH_STATE']).exists()\n"
        " print(json.dumps({'loggedIn': logged_in, 'email': 'contract@example.test', 'subscriptionType': 'max'}))\n"
        " raise SystemExit(0 if logged_in else 1)\n"
        "if 'auth' in sys.argv and 'login' in sys.argv:\n"
        " print('Paste your code:', flush=True)\n"
        " sys.stdin.readline()\n"
        " Path(os.environ['HARNESSD_FAKE_AUTH_STATE']).write_text('authenticated')\n"
        " print('Signed in', flush=True)\n"
        " raise SystemExit(0)\n"
        "if 'exec' in sys.argv:\n"
        " for event in open("
        + repr(str(fixture))
        + "): print(event.rstrip(), flush=True)\n"
        " raise SystemExit(0)\n"
        "for line in sys.stdin:\n"
        " with open(log, 'a') as out: out.write(json.dumps({'stdin': line.rstrip()}) + '\\n')\n"
        " request = json.loads(line)\n"
        " if 'app-server' in sys.argv and 'id' in request:\n"
        "  method = request['method']\n"
        "  result = {'userAgent': 'contract'}\n"
        "  if method == 'account/read': result = {'account': {'email': 'contract@example.com', 'planType': 'plus'}}\n"
        "  if method == 'account/rateLimits/read': result = {'rateLimits': {'primary': {'usedPercent': 25}}}\n"
        "  if method == 'model/list': result = {'data': [{'id': 'contract-model', 'displayName': 'Contract model', 'isDefault': True, 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'high'}], 'defaultReasoningEffort': 'low'}], 'nextCursor': None}\n"
        "  print(json.dumps({'id': request['id'], 'result': result}), flush=True)\n"
        "  continue\n"
        f" for event in open({str(fixture)!r}): print(event.rstrip(), flush=True)\n"
    )
    path.chmod(0o755)


@pytest.fixture
def fake_clis(tmp_path):
    """CLI-shaped executables that retain argv/stdin and replay fixture NDJSON."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "fake-cli.jsonl"
    fixtures = Path(__file__).parents[1] / "fixtures" / "structured"
    _fake_cli(bin_dir / "claude", fixtures / "claude_basic.ndjson", log)
    _fake_cli(bin_dir / "codex", fixtures / "codex_basic.ndjson", log)
    _fake_cli(bin_dir / "agy", fixtures / "codex_basic.ndjson", log)
    return bin_dir, log


@dataclass
class Daemon:
    url: str
    token: str
    process: subprocess.Popen
    root: Path
    launch: list[str]
    environment: dict[str, str]

    def restart(self) -> None:
        """Restart the shipped entry point against its existing on-disk state."""
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        self.process = subprocess.Popen(
            self.launch,
            cwd=Path.cwd(),
            env=self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        line = self.process.stdout.readline().strip()
        assert line.startswith("LISTENING 127.0.0.1:"), self.process.stderr.read()
        self.url = f"http://127.0.0.1:{int(line.rsplit(':', 1)[1])}"


@pytest.fixture
def daemon(tmp_path, fake_hub, fake_clis, fake_deepseek, request):
    """Launch the selected implementation with an isolated home and store."""
    options = getattr(request, "param", {})
    if options is True:
        options = {"relay": True}
    if not isinstance(options, dict):
        raise pytest.UsageError("daemon fixture parameters must be a mapping")
    impl = os.environ.get("HARNESSD_IMPL", "py")
    if impl == "rs":
        binary = Path("target/release/drover-harnessd")
        if not binary.exists():
            pytest.fail(
                "Rust contract gate requires a built target/release/drover-harnessd"
            )
        command = [str(binary)]
    elif impl == "py":
        command = [sys.executable, "-m", "drover.server.harness.cli"]
    else:
        raise pytest.UsageError("HARNESSD_IMPL must be py or rs")

    home = tmp_path / "home"
    home.mkdir()
    for relative, contents in options.get("home_files", {}).items():
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    data = tmp_path / "data"
    config = tmp_path / "config.toml"
    advisory_target = None
    advisory = ""
    if "advisory_target_text" in options:
        advisory_target = tmp_path / "AGENTS.md"
        advisory_target.write_text(options["advisory_target_text"])
        advisory = (
            "\n[advisory_content]\n"
            "enabled = true\n"
            'backend_policy = "local"\n'
            "external_consent = false\n"
            f"targets = [{json.dumps(str(advisory_target))}]\n"
            f"allowed_roots = [{json.dumps(str(tmp_path))}]\n"
            "max_file_bytes = 131072\n"
            "max_bundle_bytes = 524288\n"
            "excerpt_max_chars = 320\n"
        )
    config.write_text(
        "[paths]\n"
        f"incoming_dir = {json.dumps(str(data / 'incoming'))}\n"
        f"parquet_dir = {json.dumps(str(data / 'parquet'))}\n"
        f"duckdb_path = {json.dumps(str(data / 'drover.duckdb'))}\n"
        f"worktrees_dir = {json.dumps(str(tmp_path / 'worktrees'))}\n" + advisory
    )
    for path, status, body in options.get("hub_responses", []):
        fake_hub.script(path, status, body)
    if "preseed_consent" in options:
        consent_path = config.with_name(f".{config.name}.harness-content-consent.json")
        consent_path.write_text(json.dumps(options["preseed_consent"]) + "\n")
    bin_dir, log = fake_clis
    fixtures = Path(__file__).parents[1] / "fixtures" / "structured"
    claude_fixture_name = options.get("claude_fixture", "claude_basic.ndjson")
    if (
        not isinstance(claude_fixture_name, str)
        or Path(claude_fixture_name).name != claude_fixture_name
        or not (claude_fixture := fixtures / claude_fixture_name).is_file()
    ):
        raise pytest.UsageError("daemon claude_fixture must name a structured fixture")
    _fake_cli(bin_dir / "claude", claude_fixture, log)
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "PATH": f"{bin_dir}{os.pathsep}{environment['PATH']}",
            "HARNESSD_FAKE_CLI_LOG": str(log),
            "HARNESSD_FAKE_AUTH_STATE": str(tmp_path / "fake-auth-state"),
            "DROVER_DEEPSEEK_HARNESS_URL": fake_deepseek,
            "PYTHONPATH": str(Path.cwd() / "src")
            + os.pathsep
            + environment.get("PYTHONPATH", ""),
        }
    )
    launch = [
        *command,
        "--config",
        str(config),
        "--host-id",
        "contract-host",
        "--display-name",
        "Contract Host",
        "--listen",
        "127.0.0.1:0",
    ]
    if not options.get("no_token"):
        launch.extend(["--host-token", "contract-token"])
    if not options.get("no_central"):
        launch.extend(["--central-url", fake_hub.url])
    if options.get("relay"):
        launch.append("--relay")
    process = subprocess.Popen(
        launch,
        cwd=Path.cwd(),
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    line = process.stdout.readline().strip()
    assert line.startswith("LISTENING 127.0.0.1:"), process.stderr.read()
    port = int(line.rsplit(":", 1)[1])
    launched = Daemon(
        f"http://127.0.0.1:{port}",
        "contract-token",
        process,
        tmp_path,
        launch,
        environment,
    )
    try:
        yield launched
    finally:
        launched.process.terminate()
        try:
            launched.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            launched.process.kill()
            launched.process.wait(timeout=10)


def wait_until(predicate, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()
