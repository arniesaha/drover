"""Bridge a fake app websocket to a fake relay spoke through the hub."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass

import pytest

from drover.schema import bootstrap
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.relay_protocol import (
    data_frame,
    hello_frame,
    opened_frame,
    res_frame,
)
from drover.server.harness.websocket import (
    client_handshake,
    client_recv_json,
    client_send_json,
)
from drover.server.metrics import MetricsCollector
from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings


@dataclass
class _MetricsServerWithRelayHost:
    host: str
    port: int
    token: str
    collector: MetricsCollector
    server: object
    spoke_sock: socket.socket


def _spoke_recv(sock: socket.socket) -> dict:
    """Read frames, skipping pong/ping Nones, like test_relay_manager.py does."""
    while True:
        frame = client_recv_json(sock)
        if frame is not None:
            return frame


@pytest.fixture
def metrics_server_with_relay_host(tmp_path):
    """Server + connected fake spoke + session "s1" on relay host "laptop"."""
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="laptop",
        display_name="Laptop",
        kind="mac",
        connection_kind="relay",
        status="online",
    )
    registry.create_session(
        session_id="s1",
        host_id="laptop",
        harness="shell",
        command="/bin/sh",
        status="created",
    )
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

    spoke = socket.create_connection((host, port), timeout=5)
    client_handshake(
        spoke,
        host=f"{host}:{port}",
        path="/harness/relay",
        headers={"Authorization": f"Bearer {token}"},
    )
    client_send_json(spoke, hello_frame("laptop"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if collector.relay_manager.is_live("laptop"):
            break
        time.sleep(0.05)
    assert collector.relay_manager.is_live("laptop")

    try:
        yield _MetricsServerWithRelayHost(
            host=host,
            port=port,
            token=token,
            collector=collector,
            server=server,
            spoke_sock=spoke,
        )
    finally:
        spoke.close()
        server.shutdown()
        server.server_close()


def test_terminal_attach_bridges_over_relay(metrics_server_with_relay_host):
    env = metrics_server_with_relay_host  # server + connected fake spoke + session "s1" on host "laptop"
    spoke = env.spoke_sock

    def spoke_loop() -> None:
        # harness_terminal_route reconciles "created"/"starting"/"running"
        # sessions against the host before handing back a route -- for a
        # relay-connected host that reconcile GET rides the same socket, so
        # the fake spoke must answer it before the terminal "open" frame.
        reconcile = _spoke_recv(spoke)
        assert reconcile["kind"] == "req"
        assert reconcile["method"] == "GET"
        assert reconcile["path"] == "/sessions/s1"
        client_send_json(spoke, res_frame(reconcile["id"], 200, "{}"))

        frame = _spoke_recv(spoke)
        assert frame["kind"] == "open"
        assert frame["path"] == "/sessions/s1/terminal"
        chan = frame["chan"]
        client_send_json(spoke, opened_frame(chan))
        client_send_json(spoke, data_frame(chan, {"type": "output", "data": "$ "}))
        echo = _spoke_recv(spoke)
        assert echo["message"] == {"type": "stdin", "data": "ls\n"}

    thread = threading.Thread(target=spoke_loop, daemon=True)
    thread.start()

    app = socket.create_connection((env.host, env.port), timeout=5)
    try:
        client_handshake(
            app,
            host=f"{env.host}:{env.port}",
            path="/harness/sessions/s1/terminal",
            headers={"Authorization": f"Bearer {env.token}"},
        )
        app.settimeout(5)
        first = None
        while first is None:
            first = client_recv_json(app)
        assert first == {"type": "output", "data": "$ "}
        client_send_json(app, {"type": "stdin", "data": "ls\n"})
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        app.close()
