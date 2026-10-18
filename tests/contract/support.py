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
            if self.relay_connections:
                return self.relay_connections[-1]
            time.sleep(0.02)
        raise AssertionError("daemon did not establish relay websocket")


@pytest.fixture
def fake_hub():
    hub: FakeHub

    class Handler(BaseHTTPRequestHandler):
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
        "import json, os, sys\n"
        "if '--version' in sys.argv:\n"
        " print('contract-cli 1.0')\n"
        " raise SystemExit(0)\n"
        "log = os.environ['HARNESSD_FAKE_CLI_LOG']\n"
        "with open(log, 'a') as out:\n"
        " out.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "for line in sys.stdin:\n"
        " with open(log, 'a') as out: out.write(json.dumps({'stdin': line.rstrip()}) + '\\n')\n"
        " request = json.loads(line)\n"
        " if 'app-server' in sys.argv and 'id' in request:\n"
        "  method = request['method']\n"
        "  result = {'userAgent': 'contract'}\n"
        "  if method == 'account/read': result = {'account': {'email': 'contract@example.com', 'planType': 'plus'}}\n"
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


@pytest.fixture
def daemon(tmp_path, fake_hub, fake_clis, request):
    """Launch the selected implementation with an isolated home and store."""
    impl = os.environ.get("HARNESSD_IMPL", "py")
    if impl == "rs":
        binary = Path("target/release/drover-harnessd")
        if not binary.exists():
            pytest.skip("Rust harnessd binary has not been built")
        command = [str(binary)]
    elif impl == "py":
        command = [sys.executable, "-m", "drover.server.harness.cli"]
    else:
        raise pytest.UsageError("HARNESSD_IMPL must be py or rs")

    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    config = tmp_path / "config.toml"
    config.write_text(
        "[paths]\n"
        f"incoming_dir = {json.dumps(str(data / 'incoming'))}\n"
        f"parquet_dir = {json.dumps(str(data / 'parquet'))}\n"
        f"duckdb_path = {json.dumps(str(data / 'drover.duckdb'))}\n"
        f"worktrees_dir = {json.dumps(str(tmp_path / 'worktrees'))}\n"
    )
    bin_dir, log = fake_clis
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "PATH": f"{bin_dir}{os.pathsep}{environment['PATH']}",
            "HARNESSD_FAKE_CLI_LOG": str(log),
            "PYTHONPATH": str(Path.cwd() / "src")
            + os.pathsep
            + environment.get("PYTHONPATH", ""),
        }
    )
    process = subprocess.Popen(
        [
            *command,
            "--config",
            str(config),
            "--host-id",
            "contract-host",
            "--display-name",
            "Contract Host",
            "--listen",
            "127.0.0.1:0",
            "--host-token",
            "contract-token",
            "--central-url",
            fake_hub.url,
            *(["--relay"] if getattr(request, "param", False) else []),
        ],
        cwd=Path.cwd(),
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    line = process.stdout.readline().strip()
    assert line.startswith("LISTENING 127.0.0.1:"), process.stderr.read()
    port = int(line.rsplit(":", 1)[1])
    launched = Daemon(f"http://127.0.0.1:{port}", "contract-token", process, tmp_path)
    try:
        yield launched
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def wait_until(predicate, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()
