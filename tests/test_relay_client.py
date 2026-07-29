"""Drive RelayClient.serve_connection over a socketpair; this test plays the hub."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from drover.schema import bootstrap
from drover.server.harness.daemon import (
    DEFAULT_PRESETS,
    HarnessDaemonState,
    create_harness_server,
    register_daemon_host,
)
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.relay_client import RelayClient
from drover.server.harness.relay_protocol import open_frame, req_frame
from drover.server.harness.websocket import recv_json, send_json


def _wait_for(sock, kinds, timeout_s=20.0):
    """Next frame of one of ``kinds``; ignores everything else."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        sock.settimeout(max(0.1, deadline - time.monotonic()))
        frame = recv_json(sock)
        if frame is not None and frame.get("kind") in kinds:
            return frame
    raise AssertionError(f"timed out waiting for {kinds}")


@pytest.fixture
def harnessd_server(tmp_path):
    """In-process harnessd on a loopback ephemeral port (api_token test-token)."""
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    state = HarnessDaemonState(
        host_id="laptop",
        display_name="Laptop",
        kind="mac",
        registry=HarnessRegistry(duckdb_path),
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        local_url="http://127.0.0.1:0",
        api_token="test-token",
        worktrees_dir=tmp_path / "worktrees",
    )
    register_daemon_host(state)
    server = create_harness_server(listen_host="127.0.0.1", listen_port=0, state=state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        state.pty.close_all()
        server.server_close()


def test_req_frame_dispatches_to_loopback_and_answers(harnessd_server):
    client = RelayClient(
        central_url="https://unused.example",
        host_id="laptop",
        token="test-token",
        loopback_port=harnessd_server.server_port,
    )
    hub_side, spoke_side = socket.socketpair()
    thread = threading.Thread(
        target=client.serve_connection, args=(spoke_side,), daemon=True
    )
    thread.start()
    send_json(hub_side, req_frame("r1", "GET", "/sessions", None))
    frame = None
    while frame is None or frame.get("kind") != "res":
        frame = recv_json(hub_side)
    assert frame["id"] == "r1"
    assert frame["status"] == 200
    assert "sessions" in json.loads(frame["body"])
    hub_side.close()


def test_req_frame_loopback_failure_is_502_not_crash(harnessd_server):
    client = RelayClient(
        central_url="https://unused.example",
        host_id="laptop",
        token="test-token",
        loopback_port=1,  # nothing listens here
    )
    hub_side, spoke_side = socket.socketpair()
    threading.Thread(
        target=client.serve_connection, args=(spoke_side,), daemon=True
    ).start()
    send_json(hub_side, req_frame("r1", "GET", "/sessions", None))
    frame = None
    while frame is None or frame.get("kind") != "res":
        frame = recv_json(hub_side)
    assert frame["status"] == 502
    # loop must still be alive: a second request also answers
    send_json(hub_side, req_frame("r2", "GET", "/sessions", None))
    frame = None
    while frame is None or frame.get("kind") != "res":
        frame = recv_json(hub_side)
    assert frame["id"] == "r2"
    hub_side.close()


def test_open_frame_against_missing_session_reports_open_error(harnessd_server):
    client = RelayClient(
        central_url="https://unused.example",
        host_id="laptop",
        token="test-token",
        loopback_port=harnessd_server.server_port,
    )
    hub_side, spoke_side = socket.socketpair()
    threading.Thread(
        target=client.serve_connection, args=(spoke_side,), daemon=True
    ).start()
    send_json(hub_side, open_frame("c1", "/sessions/nope/terminal"))
    frame = None
    while frame is None or frame.get("kind") not in {"opened", "open_error"}:
        frame = recv_json(hub_side)
    assert frame["kind"] == "open_error"
    assert frame["chan"] == "c1"
    hub_side.close()


def test_channel_pumps_terminal_and_closes_when_the_session_dies(
    harnessd_server, tmp_path
):
    """The hub-side bridge only learns a session ended from our close frame."""
    client = RelayClient(
        central_url="https://unused.example",
        host_id="laptop",
        token="test-token",
        loopback_port=harnessd_server.server_port,
    )
    hub_side, spoke_side = socket.socketpair()
    threading.Thread(
        target=client.serve_connection, args=(spoke_side,), daemon=True
    ).start()

    send_json(
        hub_side,
        req_frame(
            "r1", "POST", "/sessions", {"harness": "shell", "cwd": str(tmp_path)}
        ),
    )
    created = _wait_for(hub_side, {"res"})
    assert created["status"] == 201
    session_id = json.loads(created["body"])["session_id"]

    send_json(hub_side, open_frame("c1", f"/sessions/{session_id}/terminal"))
    assert _wait_for(hub_side, {"opened", "open_error"})["kind"] == "opened"
    # The daemon's "attached" greeting proves the pump is forwarding.
    assert _wait_for(hub_side, {"data"})["message"]["type"] == "attached"

    send_json(
        hub_side, req_frame("r2", "POST", f"/sessions/{session_id}/terminate", {})
    )
    assert _wait_for(hub_side, {"close"})["chan"] == "c1"
    hub_side.close()
