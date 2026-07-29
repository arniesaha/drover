"""GET /harness/relay: upgrades a spoke's websocket and hands it to RelayManager."""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass

import pytest

from drover.schema import bootstrap
from drover.server.harness.relay_protocol import hello_frame
from drover.server.harness.websocket import client_handshake, client_send_json
from drover.server.metrics import MetricsCollector
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


def test_relay_upgrade_registers_live_host(metrics_server):
    host, port, token = metrics_server.host, metrics_server.port, metrics_server.token
    sock = socket.create_connection((host, port), timeout=5)
    client_handshake(
        sock,
        host=f"{host}:{port}",
        path="/harness/relay",
        headers={"Authorization": f"Bearer {token}"},
    )
    client_send_json(sock, hello_frame("work-laptop"))
    # attach is async from the client's perspective; poll briefly
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if metrics_server.collector.relay_manager.is_live("work-laptop"):
            break
        time.sleep(0.05)
    assert metrics_server.collector.relay_manager.is_live("work-laptop")
    sock.close()


def test_relay_upgrade_requires_token(metrics_server):
    host, port = metrics_server.host, metrics_server.port
    sock = socket.create_connection((host, port), timeout=5)
    try:
        with pytest.raises(RuntimeError):  # handshake sees non-101 status
            client_handshake(sock, host=f"{host}:{port}", path="/harness/relay")
    finally:
        sock.close()
