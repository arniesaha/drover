"""Bridge a fake app websocket to a fake relay spoke through the hub."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
    duckdb_path: object


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
            duckdb_path=duckdb_path,
        )
    finally:
        spoke.close()
        server.shutdown()
        server.server_close()


def test_terminal_attach_bridges_over_relay(metrics_server_with_relay_host):
    # env: server + connected fake spoke + session "s1" on host "laptop".
    env = metrics_server_with_relay_host
    spoke = env.spoke_sock
    errors: list[BaseException] = []

    def spoke_loop() -> None:
        try:
            # harness_terminal_route reconciles "created"/"starting"/"running"
            # sessions against the host before handing back a route -- for a
            # relay-connected host that reconcile GET rides the same socket,
            # so the fake spoke must answer it before the terminal "open"
            # frame.
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
            # A PTY event frame, same shape the daemon's terminal loop sends
            # over a direct connection -- proves the relay flow mirrors it
            # into the hub's own event log via _mirror_harness_event_message.
            client_send_json(
                spoke,
                data_frame(
                    chan,
                    {
                        "type": "event",
                        "event": {
                            "event_id": "evt-relay-1",
                            "event_type": "terminal.output",
                            "payload": {"data": "$ "},
                        },
                    },
                ),
            )
            echo = _spoke_recv(spoke)
            assert echo["message"] == {"type": "stdin", "data": "ls\n"}
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            raise

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
        second = None
        while second is None:
            second = client_recv_json(app)
        assert second == {
            "type": "event",
            "event": {
                "event_id": "evt-relay-1",
                "event_type": "terminal.output",
                "payload": {"data": "$ "},
            },
        }
        client_send_json(app, {"type": "stdin", "data": "ls\n"})
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors, f"spoke_loop raised: {errors}"
    finally:
        app.close()

    # The mirror is deliberately off the drain thread now, so it lands shortly
    # after the frame reaches the app rather than before it. Bounded poll.
    registry = HarnessRegistry(env.duckdb_path)
    deadline = time.monotonic() + 10
    mirrored = None
    while time.monotonic() < deadline:
        mirrored = registry.get_event("evt-relay-1")
        if mirrored is not None:
            break
        time.sleep(0.05)
    assert mirrored is not None
    assert mirrored.event_type == "terminal.output"
    assert mirrored.payload == {"data": "$ "}


def test_terminal_attach_never_dials_a_relay_host_by_url(
    metrics_server_with_relay_host,
):
    """Same rule as _harness_request, on the interactive path.

    A relay host whose row carries a URL must not be dialled when its socket
    is down: 127.0.0.1:7081 is the default listen address everywhere here, so
    the hub would attach the user's terminal to its own harnessd.
    """
    env = metrics_server_with_relay_host
    # A decoy standing in for "the hub's own harnessd on 127.0.0.1:7081".
    # Asserting on the app's status code would not discriminate -- a failed
    # upstream dial also yields 502 -- so the invariant under test is that
    # nothing ever arrives here.
    hits: list[str] = []

    class _Decoy(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits.append(self.path)
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    decoy = ThreadingHTTPServer(("127.0.0.1", 0), _Decoy)
    decoy_thread = threading.Thread(target=decoy.serve_forever, daemon=True)
    decoy_thread.start()

    HarnessRegistry(env.duckdb_path).register_host(
        host_id="laptop",
        display_name="Laptop",
        kind="mac",
        connection_kind="relay",
        local_url=f"http://127.0.0.1:{decoy.server_address[1]}",
    )
    env.spoke_sock.close()  # relay socket goes away; the stray URL remains
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not env.collector.relay_manager.is_live("laptop"):
            break
        time.sleep(0.05)
    assert not env.collector.relay_manager.is_live("laptop")

    app = socket.create_connection((env.host, env.port), timeout=5)
    try:
        with pytest.raises(RuntimeError) as caught:
            client_handshake(
                app,
                host=f"{env.host}:{env.port}",
                path="/harness/sessions/s1/terminal",
                headers={"Authorization": f"Bearer {env.token}"},
            )
        assert "502" in str(caught.value)
    finally:
        app.close()
        decoy.shutdown()
        decoy.server_close()
        decoy_thread.join(timeout=5)

    assert hits == [], f"hub dialled a relay host's URL for a terminal: {hits}"


def test_terminal_output_survives_a_wedged_event_mirror(
    metrics_server_with_relay_host, monkeypatch
):
    """The composition bug: a slow mirror must not eat terminal output.

    Before the mirror moved off the drain thread, every message paid a DuckDB
    connection under a process-wide lock. Under PTY burst rates the drain
    thread fell behind, the channel's bounded queue overflowed, and it dropped
    the *oldest* messages -- silently losing output the user was watching in
    order to finish a write nobody was waiting on.

    Here the writer is wedged outright, which is that failure taken to its
    limit: every frame must still reach the app.
    """
    env = metrics_server_with_relay_host
    spoke = env.spoke_sock
    blocked = threading.Event()

    def _wedged(self, records):
        blocked.wait(30)
        return 0

    monkeypatch.setattr(
        "drover.server.harness.registry.HarnessRegistry.append_events_if_new", _wedged
    )

    burst = 50
    errors: list[BaseException] = []

    def spoke_loop() -> None:
        try:
            reconcile = _spoke_recv(spoke)
            client_send_json(spoke, res_frame(reconcile["id"], 200, "{}"))
            frame = _spoke_recv(spoke)
            chan = frame["chan"]
            client_send_json(spoke, opened_frame(chan))
            for index in range(burst):
                # Every message is an event, so every one hits the mirror.
                client_send_json(
                    spoke,
                    data_frame(
                        chan,
                        {
                            "type": "event",
                            "event": {
                                "event_id": f"evt-burst-{index}",
                                "event_type": "terminal.output",
                                "payload": {"n": index},
                            },
                        },
                    ),
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

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
        app.settimeout(15)
        seen = []
        while len(seen) < burst:
            message = client_recv_json(app)
            if message is not None:
                seen.append(message["event"]["payload"]["n"])
        assert seen == list(range(burst)), "terminal output was dropped or reordered"
        assert not errors, f"spoke_loop raised: {errors}"
    finally:
        blocked.set()
        app.close()
        thread.join(timeout=5)
