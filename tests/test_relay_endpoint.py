"""GET /harness/relay: upgrades a spoke's websocket and hands it to RelayManager."""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass

import pytest

from drover.schema import bootstrap
from drover.server.harness.relay_protocol import hello_frame, req_frame, res_frame
from drover.server.harness.websocket import (
    OPCODE_PING,
    WebSocketClosed,
    client_handshake,
    client_recv_json,
    client_send_frame,
    client_send_json,
)
from drover.server.metrics import MetricsCollector
from drover.server.web import app as app_module
from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings


@dataclass
class _MetricsServer:
    host: str
    port: int
    token: str
    collector: MetricsCollector
    server: object


@pytest.fixture
def metrics_server(tmp_path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
    )
    token = "test-token"
    collector.api_token = token
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=collector,
        auth=AuthSettings(enabled=True, api_token=token),
    )
    host, port = server.server_address
    try:
        yield _MetricsServer(
            host=host, port=port, token=token, collector=collector, server=server
        )
    finally:
        server.shutdown()
        server.server_close()


def _connect_and_hello(metrics_server, host_id: str) -> socket.socket:
    host, port, token = metrics_server.host, metrics_server.port, metrics_server.token
    sock = socket.create_connection((host, port), timeout=5)
    client_handshake(
        sock,
        host=f"{host}:{port}",
        path="/harness/relay",
        headers={"Authorization": f"Bearer {token}"},
    )
    client_send_json(sock, hello_frame(host_id))
    return sock


def _spoke_recv(sock: socket.socket) -> dict:
    """Read frames, skipping pong/ping Nones, like test_relay_manager.py does."""
    while True:
        frame = client_recv_json(sock)
        if frame is not None:
            return frame


def test_relay_upgrade_registers_live_host(metrics_server):
    sock = _connect_and_hello(metrics_server, "work-laptop")
    # attach is async from the client's perspective; poll briefly
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if metrics_server.collector.relay_manager.is_live("work-laptop"):
            break
        time.sleep(0.05)
    assert metrics_server.collector.relay_manager.is_live("work-laptop")

    # is_live() only proves a flag flipped. Prove the hijacked fd actually
    # survived the handler's finish()/shutdown_request() cleanup and still
    # works bidirectionally by driving one real req/res round-trip through
    # RelayManager.request(), with this test playing the spoke -- mirrors
    # test_relay_manager.py's test_request_round_trip.
    result: dict[str, object] = {}

    def _issue_request() -> None:
        status, body = metrics_server.collector.relay_manager.request(
            "work-laptop", "GET", "/sessions", None, timeout_s=5
        )
        result["status"] = status
        result["body"] = body

    thread = threading.Thread(target=_issue_request, daemon=True)
    thread.start()
    try:
        frame = _spoke_recv(sock)
        assert frame["kind"] == "req"
        assert frame["method"] == "GET"
        assert frame["path"] == "/sessions"
        client_send_json(sock, res_frame(frame["id"], 200, '{"sessions": []}\n'))
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert result["status"] == 200
        assert json.loads(result["body"]) == {"sessions": []}
    finally:
        sock.close()


def test_relay_upgrade_requires_token(metrics_server):
    host, port = metrics_server.host, metrics_server.port
    sock = socket.create_connection((host, port), timeout=5)
    try:
        with pytest.raises(RuntimeError):  # handshake sees non-101 status
            client_handshake(sock, host=f"{host}:{port}", path="/harness/relay")
    finally:
        sock.close()


def test_relay_upgrade_rejects_wrong_kind_frame(metrics_server):
    host, port, token = metrics_server.host, metrics_server.port, metrics_server.token
    sock = socket.create_connection((host, port), timeout=5)
    try:
        client_handshake(
            sock,
            host=f"{host}:{port}",
            path="/harness/relay",
            headers={"Authorization": f"Bearer {token}"},
        )
        # A well-formed but non-hello frame must be rejected: the endpoint
        # only ever accepts a hello as the first frame.
        client_send_json(sock, req_frame("req-1", "GET", "/sessions", None))
        sock.settimeout(5)
        with pytest.raises(WebSocketClosed):
            _spoke_recv(sock)
        assert metrics_server.collector.relay_manager.live_host_ids() == set()
    finally:
        sock.close()


def test_relay_upgrade_rejects_empty_host_id(metrics_server):
    host, port, token = metrics_server.host, metrics_server.port, metrics_server.token
    sock = socket.create_connection((host, port), timeout=5)
    try:
        client_handshake(
            sock,
            host=f"{host}:{port}",
            path="/harness/relay",
            headers={"Authorization": f"Bearer {token}"},
        )
        client_send_json(sock, hello_frame(""))
        sock.settimeout(5)
        with pytest.raises(WebSocketClosed):
            _spoke_recv(sock)
        assert metrics_server.collector.relay_manager.live_host_ids() == set()
    finally:
        sock.close()


def test_hello_budget_is_total_not_per_recv(metrics_server, monkeypatch):
    """A pinger must not hold a handler thread open indefinitely.

    settimeout() is per socket operation, and recv_json answers a ping and
    returns None -- so a client pinging faster than the timeout used to reset
    the clock forever. On ThreadingHTTPServer that is one pinned thread per
    connection with no cap, reachable from the internet by anyone holding the
    shared token once the funnel is up.
    """
    monkeypatch.setattr(app_module, "RELAY_HELLO_TIMEOUT_S", 1.0)
    host, port, token = metrics_server.host, metrics_server.port, metrics_server.token
    sock = socket.create_connection((host, port), timeout=10)
    try:
        client_handshake(
            sock,
            host=f"{host}:{port}",
            path="/harness/relay",
            headers={"Authorization": f"Bearer {token}"},
        )
        sock.settimeout(20)
        started = time.monotonic()
        # Ping steadily and never say hello. Under the per-recv bug this loop
        # runs until the test's own deadline; under a total budget the server
        # gives up and closes.
        with pytest.raises((WebSocketClosed, OSError)):
            while time.monotonic() - started < 15:
                client_send_frame(sock, OPCODE_PING, b"stall")
                time.sleep(0.2)
                # Drains the pong, and raises once the server hangs up.
                client_recv_json(sock)
        elapsed = time.monotonic() - started
        assert elapsed < 10, f"handler stayed pinned for {elapsed:.1f}s"
        assert metrics_server.collector.relay_manager.live_host_ids() == set()
    finally:
        sock.close()
