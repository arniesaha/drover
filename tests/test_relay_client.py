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
from drover.server.harness import relay_client
from drover.server.harness.relay_client import RelayClient
from drover.server.harness.relay_protocol import close_frame, open_frame, req_frame
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
    frame = _wait_for(hub_side, {"res"})
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
    assert _wait_for(hub_side, {"res"})["status"] == 502
    # loop must still be alive: a second request also answers
    send_json(hub_side, req_frame("r2", "GET", "/sessions", None))
    assert _wait_for(hub_side, {"res"})["id"] == "r2"
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
    frame = _wait_for(hub_side, {"opened", "open_error"})
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
    # The res and the close race, so collect both. Checking the res as soon as
    # it lands makes a failed terminate fail as itself rather than as a
    # generic "timed out waiting for close".
    seen: dict[str, dict] = {}
    while len(seen) < 2:
        frame = _wait_for(hub_side, {"res", "close"})
        seen[frame["kind"]] = frame
        if "res" in seen:
            assert 200 <= seen["res"]["status"] < 300, seen["res"]["body"]
    assert seen["close"]["chan"] == "c1"
    hub_side.close()


def test_hub_close_tears_down_a_live_channel(harnessd_server, tmp_path):
    """The other half of the hub's open-timeout cancel (RelayManager C1).

    Until the hub started sending a close on that path this branch was dead
    code, and the pump held a live PTY attach until the whole connection died.
    """
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
    assert _wait_for(hub_side, {"data"})["message"]["type"] == "attached"

    send_json(hub_side, close_frame("c1"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client._conn is not None and client._conn.get_channel("c1") is None:
            break
        time.sleep(0.05)
    assert client._conn.get_channel("c1") is None, "channel survived the hub close"
    # A hub-initiated close must not be echoed back, and the connection itself
    # stays serviceable: another request still answers.
    send_json(hub_side, req_frame("r2", "GET", "/sessions", None))
    assert _wait_for(hub_side, {"res", "close"})["kind"] == "res"
    hub_side.close()


class _StopAfter:
    """Stand-in for ``RelayClient._stopped`` that records backoff delays."""

    def __init__(self, sleeps: int) -> None:
        self.limit = sleeps
        self.delays: list[float] = []
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, delay: float | None = None) -> bool:
        self.delays.append(delay)
        if len(self.delays) >= self.limit:
            self._set = True
        return self._set


def _always_refuses(target):
    raise OSError("connection refused")


def test_backoff_grows_then_resets_after_a_stable_connection(monkeypatch):
    monkeypatch.setattr(relay_client.random, "uniform", lambda low, high: 1.0)

    client = RelayClient("http://127.0.0.1:9", "laptop", "test-token", 1)
    monkeypatch.setattr(client, "_connect", _always_refuses)
    recorder = _StopAfter(5)
    client._stopped = recorder
    client.run_forever()
    assert recorder.delays == [1.0, 2.0, 4.0, 8.0, 16.0]

    # A connection that counts as stable puts the next failure back at 1s.
    monkeypatch.setattr(relay_client, "STABLE_CONNECTION_S", 0.0)
    stable = RelayClient("http://127.0.0.1:9", "laptop", "test-token", 1)
    monkeypatch.setattr(stable, "_connect", _always_refuses)
    stable_recorder = _StopAfter(4)
    stable._stopped = stable_recorder
    stable.run_forever()
    assert stable_recorder.delays == [1.0, 1.0, 1.0, 1.0]


def test_stop_during_a_dial_tears_down_the_late_connection(monkeypatch):
    """stop() cannot see a connection that does not exist yet."""
    monkeypatch.setattr(relay_client, "STOP_JOIN_TIMEOUT_S", 0.2)
    hub_side, spoke_side = socket.socketpair()
    client = RelayClient("http://127.0.0.1:9", "laptop", "test-token", 1)
    dialing = threading.Event()
    release = threading.Event()

    def slow_connect(target):
        dialing.set()
        release.wait(10)
        return spoke_side

    monkeypatch.setattr(client, "_connect", slow_connect)
    thread = client.start()
    assert dialing.wait(5)
    client.stop()
    release.set()  # the dial completes *after* stop()
    thread.join(timeout=10)
    assert not thread.is_alive()
    # Served indefinitely would leave this socket open and silent.
    hub_side.settimeout(5)
    assert hub_side.recv(16) == b""
    hub_side.close()


def test_unsupported_relay_scheme_disables_the_client_instead_of_dialling():
    """Guessing a scheme is how a Bearer token ends up on the wire in clear."""
    dialed = []
    for url in ("hub:8787", "127.0.0.1:8787", "ftp://hub.example", "https://"):
        client = RelayClient(url, "laptop", "test-token", 1)
        client._connect = lambda target: dialed.append(target)
        client.run_forever()  # returns rather than looping forever
    assert dialed == []


def test_websocket_schemes_map_to_their_http_equivalents():
    secure = relay_client._Target("wss://hub.example")
    assert (secure.tls, secure.port) == (True, 443)
    plain = relay_client._Target("ws://hub.example:8787")
    assert (plain.tls, plain.port) == (False, 8787)
    assert (
        relay_client._Target("https://hub.example/base").path == "/base/harness/relay"
    )
