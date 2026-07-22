from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.request

from drover.schema import bootstrap
from drover.server.harness.registry import HarnessRegistry
from drover.server.metrics import MetricsCollector
from drover.server.web.app import _parse_host_auth_route, start_metrics_server


class _HarnessAuthHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, str | None]] = []

    def do_GET(self) -> None:  # noqa: N802
        self.__class__.requests.append(
            {
                "method": "GET",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            }
        )
        body = json.dumps({"harness": "codex", "state": "unauthenticated"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        self.__class__.requests.append(
            {
                "method": "POST",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            }
        )
        body = json.dumps(
            {
                "harness": "codex",
                "flow_id": "auth-flow-1",
                "state": "waiting_for_user",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _start_central(tmp_path, upstream_url: str):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="mac",
        local_url=upstream_url,
        status="online",
        capabilities={"harnesses": [{"name": "codex", "enabled": True}]},
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
    )
    collector.api_token = "secret"
    from drover.server.web.auth import AuthSettings

    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=collector,
        auth=AuthSettings(enabled=True, api_token="secret"),
    )
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def _start_proxy_pair(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _HarnessAuthHandler)
    _HarnessAuthHandler.requests = []
    central = None
    try:
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        central, base = _start_central(
            tmp_path, f"http://127.0.0.1:{upstream.server_address[1]}"
        )
        return upstream, central, base
    except Exception:
        if central is not None:
            central.shutdown()
            central.server_close()
        upstream.shutdown()
        upstream.server_close()
        raise


def _close_proxy_pair(upstream, central):
    central.shutdown()
    central.server_close()
    upstream.shutdown()
    upstream.server_close()


def test_central_proxies_auth_status(tmp_path):
    upstream, central, base = _start_proxy_pair(tmp_path)
    try:
        req = urllib.request.Request(
            f"{base}/harness/hosts/mac-mini/auth/codex/status",
            headers={"Authorization": "Bearer secret"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            body = json.loads(response.read().decode())
    finally:
        _close_proxy_pair(upstream, central)

    assert response.status == 200
    assert body["host_id"] == "mac-mini"
    assert body["state"] == "unauthenticated"
    assert _HarnessAuthHandler.requests[0]["method"] == "GET"
    assert _HarnessAuthHandler.requests[0]["path"] == "/auth/codex/status"
    assert _HarnessAuthHandler.requests[0]["authorization"] == "Bearer secret"


def test_central_proxies_auth_start(tmp_path):
    upstream, central, base = _start_proxy_pair(tmp_path)
    try:
        req = urllib.request.Request(
            f"{base}/harness/hosts/mac-mini/auth/codex/start",
            data=b"{}",
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            body = json.loads(response.read().decode())
    finally:
        _close_proxy_pair(upstream, central)

    assert response.status == 200
    assert body["host_id"] == "mac-mini"
    assert body["state"] == "waiting_for_user"
    assert _HarnessAuthHandler.requests[0]["method"] == "POST"
    assert _HarnessAuthHandler.requests[0]["path"] == "/auth/codex/start"
    assert _HarnessAuthHandler.requests[0]["authorization"] == "Bearer secret"


def test_central_proxies_auth_flow_poll(tmp_path):
    upstream, central, base = _start_proxy_pair(tmp_path)
    try:
        req = urllib.request.Request(
            f"{base}/harness/hosts/mac-mini/auth/codex/flows/auth-flow-1",
            headers={"Authorization": "Bearer secret"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            body = json.loads(response.read().decode())
    finally:
        _close_proxy_pair(upstream, central)

    assert response.status == 200
    assert body["host_id"] == "mac-mini"
    assert _HarnessAuthHandler.requests[0]["method"] == "GET"
    assert _HarnessAuthHandler.requests[0]["path"] == "/auth/codex/flows/auth-flow-1"
    assert _HarnessAuthHandler.requests[0]["authorization"] == "Bearer secret"


def test_central_proxies_auth_flow_cancel(tmp_path):
    upstream, central, base = _start_proxy_pair(tmp_path)
    try:
        req = urllib.request.Request(
            f"{base}/harness/hosts/mac-mini/auth/codex/flows/auth-flow-1/cancel",
            data=b"{}",
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            body = json.loads(response.read().decode())
    finally:
        _close_proxy_pair(upstream, central)

    assert response.status == 200
    assert body["host_id"] == "mac-mini"
    assert _HarnessAuthHandler.requests[0]["method"] == "POST"
    assert (
        _HarnessAuthHandler.requests[0]["path"]
        == "/auth/codex/flows/auth-flow-1/cancel"
    )
    assert _HarnessAuthHandler.requests[0]["authorization"] == "Bearer secret"


def test_host_auth_route_parser_decodes_all_forms():
    assert _parse_host_auth_route(
        "/harness/hosts/mac-mini/auth/claude-code/status"
    ) == {
        "host_id": "mac-mini",
        "harness": "claude-code",
        "action": "status",
        "method": "GET",
    }
    assert _parse_host_auth_route("/harness/hosts/h/auth/codex/start") == {
        "host_id": "h",
        "harness": "codex",
        "action": "start",
        "method": "POST",
    }
    assert _parse_host_auth_route(
        "/harness/hosts/h/auth/provider%2Ftest/flows/flow%2F1"
    ) == {
        "host_id": "h",
        "harness": "provider/test",
        "flow_id": "flow/1",
        "action": "flow",
        "method": "GET",
    }
    assert _parse_host_auth_route(
        "/harness/hosts/h/auth/provider%2Ftest/flows/flow%2F1/cancel"
    ) == {
        "host_id": "h",
        "harness": "provider/test",
        "flow_id": "flow/1",
        "action": "cancel",
        "method": "POST",
    }


def test_proxy_harness_auth_builds_quoted_upstream_paths(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    HarnessRegistry(duckdb_path).register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="mac",
        local_url="http://127.0.0.1:30400",
        status="online",
    )
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
    )
    calls = []

    def fake_proxy(url, *, method, payload, timeout_s=15):
        calls.append((method, url, payload))
        return 200, json.dumps({"state": "waiting_for_user"})

    collector._proxy_harness_request = fake_proxy  # type: ignore[method-assign]

    status, body = collector.proxy_harness_auth(
        "mac-mini",
        "provider/test",
        "status",
    )
    assert status == 200
    assert json.loads(body)["host_id"] == "mac-mini"
    assert calls[-1] == (
        "GET",
        "http://127.0.0.1:30400/auth/provider%2Ftest/status",
        {},
    )

    collector.proxy_harness_auth("mac-mini", "provider/test", "start")
    assert calls[-1][0] == "POST"
    assert calls[-1][1] == "http://127.0.0.1:30400/auth/provider%2Ftest/start"

    collector.proxy_harness_auth(
        "mac-mini",
        "provider/test",
        "flow",
        flow_id="flow/1",
    )
    assert calls[-1][0] == "GET"
    assert (
        calls[-1][1]
        == "http://127.0.0.1:30400/auth/provider%2Ftest/flows/flow%2F1"
    )

    collector.proxy_harness_auth(
        "mac-mini",
        "provider/test",
        "cancel",
        flow_id="flow/1",
    )
    assert calls[-1][0] == "POST"
    assert (
        calls[-1][1]
        == "http://127.0.0.1:30400/auth/provider%2Ftest/flows/flow%2F1/cancel"
    )
