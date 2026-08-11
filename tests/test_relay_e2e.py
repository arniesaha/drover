"""Hub + harnessd + RelayClient wired together in-process, full lifecycle.

Everything here is real: a real hub HTTP server (``start_metrics_server``), a
real harnessd (``create_harness_server``) on its own separate tmp duckdb, and
a real ``RelayClient`` dialling the hub's real ``/harness/relay`` endpoint.
The harnessd host registers on the hub with ``connection_kind=relay`` and NO
urls -- every hub->host interaction in this test (session create, terminal
attach, terminate, presence) can only work if it actually rides the relay
channel.

This is the merge gate for the whole multihost-relay feature: it is expected
to shake out integration bugs that unit tests, testing each hop in
isolation, cannot see.
"""

from __future__ import annotations

import contextlib
import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from drover.config import AdvisoryContentConfig
from drover.schema import bootstrap
from drover.server.advisory.service import InsightsService
from drover.server.harness.content_consent import DurableContentConsent
from drover.server.harness.daemon import (
    HarnessDaemonState,
    HarnessPreset,
    create_harness_server,
    register_daemon_host,
)
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.relay_client import RelayClient
from drover.server.harness.websocket import (
    WebSocketClosed,
    client_handshake,
    client_recv_json,
    client_send_json,
)
from drover.server.metrics import MetricsCollector
from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings

TOKEN = "e2e-relay-token"
HOST_ID = "laptop"


def _get(port: int, path: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _post(port: int, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body.decode("utf-8", errors="replace")}


def _hub_get(env: "_RelayEnv", path: str) -> dict[str, Any]:
    return _get(env.hub_port, path)


def _hub_post(
    env: "_RelayEnv", path: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    return _post(env.hub_port, path, payload)


def _recv_frames_until(sock: socket.socket, deadline: float):
    """Yield parsed terminal-ws frames from ``sock``, never blocking past ``deadline``.

    ``client_recv_json`` propagates ``socket.timeout``/``TimeoutError`` rather
    than swallowing it, so a bare deadline loop around it would either raise
    on the first slow frame or (with no socket timeout at all) block
    indefinitely and defeat the deadline entirely. Setting a short per-call
    timeout and catching it here is what makes this an actual bounded poll.
    """
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        sock.settimeout(max(0.05, min(remaining, 1.0)))
        try:
            frame = client_recv_json(sock)
        except TimeoutError:
            continue
        if frame is not None:
            yield frame


@dataclass
class _RelayEnv:
    hub_port: int
    hub_server: Any
    hub_collector: MetricsCollector
    hub_duckdb_path: Path
    harnessd_server: Any
    harnessd_state: HarnessDaemonState
    relay_client: RelayClient
    dialed_sockets: list[socket.socket] = field(default_factory=list)

    def spoke_sock_close(self) -> None:
        """Force an abrupt RST-style close of the client's dialled socket.

        Distinct from ``RelayClient.stop()``'s own graceful
        shutdown(SHUT_RDWR)+close(): SO_LINGER(1, 0) makes the kernel drop
        the connection with a RST instead of a clean FIN, exercising the
        "peer went away without warning" path on the hub side too.
        """
        if not self.dialed_sockets:
            return
        sock = self.dialed_sockets[-1]
        with contextlib.suppress(OSError):
            sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
        with contextlib.suppress(OSError):
            sock.close()


@pytest.fixture
def relay_env(tmp_path):
    # -- hub: its own duckdb -------------------------------------------
    hub_duckdb_path = tmp_path / "hub" / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "hub" / "parquet", duckdb_path=hub_duckdb_path)
    hub_collector = MetricsCollector(
        duckdb_path=hub_duckdb_path,
        incoming_dir=tmp_path / "hub" / "incoming",
        summarizer_report={},
    )
    hub_collector.advisory_service = InsightsService(
        hub_duckdb_path, config_path=tmp_path / "hub" / "config.toml"
    )
    hub_collector.api_token = TOKEN
    hub_server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=hub_collector,
        auth=AuthSettings(enabled=True, api_token=TOKEN),
    )
    _, hub_port = hub_server.server_address

    # -- harnessd: a SEPARATE duckdb -- mirrors production's split-DB
    # deployment, where the hub never shares a database file with a host.
    harnessd_duckdb_path = tmp_path / "harnessd" / "drover.duckdb"
    bootstrap(
        parquet_dir=tmp_path / "harnessd" / "parquet",
        duckdb_path=harnessd_duckdb_path,
    )
    presets = {
        "shell": HarnessPreset(
            name="shell",
            command=("/bin/cat",),
            enabled=True,
            description="inert stdin-echo harness for the relay e2e test",
        ),
    }
    advisory_target = tmp_path / "harnessd" / "AGENTS.md"
    advisory_target.write_text("Use the deployment skill.\n", encoding="utf-8")
    harnessd_state = HarnessDaemonState(
        host_id=HOST_ID,
        display_name="Laptop",
        kind="mac",
        registry=HarnessRegistry(harnessd_duckdb_path),
        pty=PtySessionManager(),
        presets=presets,
        local_url=None,
        tailscale_url=None,
        api_token=TOKEN,
        relay=True,
        worktrees_dir=tmp_path / "harnessd" / "worktrees",
        advisory_content=AdvisoryContentConfig(
            enabled=True,
            backend_policy="local",
            external_consent=False,
            targets=(str(advisory_target),),
            allowed_roots=(advisory_target.parent,),
            max_file_bytes=1024,
            max_bundle_bytes=2048,
            excerpt_max_chars=320,
        ),
        content_consent=DurableContentConsent(
            tmp_path / "harnessd" / "content-consent.json"
        ),
    )
    register_daemon_host(harnessd_state)
    harnessd_server = create_harness_server(
        listen_host="127.0.0.1", listen_port=0, state=harnessd_state
    )
    harnessd_thread = threading.Thread(
        target=harnessd_server.serve_forever, name="e2e-harnessd", daemon=True
    )
    harnessd_thread.start()

    # -- register the host on the hub: relay only, NO urls -------------
    status, _ = _post(
        hub_port,
        "/harness/hosts",
        {"host_id": HOST_ID, "connection_kind": "relay"},
    )
    assert status == 200

    # -- start the real RelayClient, capturing every socket it dials so
    # the test can force a hard close later, independent of stop().
    dialed_sockets: list[socket.socket] = []
    relay_client = RelayClient(
        central_url=f"http://127.0.0.1:{hub_port}",
        host_id=HOST_ID,
        token=TOKEN,
        loopback_port=harnessd_server.server_port,
    )
    original_connect = relay_client._connect

    def _tracking_connect(target):
        sock = original_connect(target)
        dialed_sockets.append(sock)
        return sock

    relay_client._connect = _tracking_connect  # type: ignore[method-assign]
    relay_client.start()

    deadline = time.monotonic() + 10
    online = False
    while time.monotonic() < deadline:
        hosts = {h["host_id"]: h for h in _get(hub_port, "/harness/hosts")["hosts"]}
        host = hosts.get(HOST_ID)
        if host is not None and host["status"] == "online":
            online = True
            break
        time.sleep(0.05)
    assert online, "relay client never came online on the hub within 10s"

    env = _RelayEnv(
        hub_port=hub_port,
        hub_server=hub_server,
        hub_collector=hub_collector,
        hub_duckdb_path=hub_duckdb_path,
        harnessd_server=harnessd_server,
        harnessd_state=harnessd_state,
        relay_client=relay_client,
        dialed_sockets=dialed_sockets,
    )
    try:
        yield env
    finally:
        relay_client.stop()
        harnessd_server.shutdown()
        harnessd_state.pty.close_all()
        harnessd_server.server_close()
        hub_server.shutdown()
        hub_server.server_close()


def test_full_session_lifecycle_over_relay(relay_env, tmp_path):
    env = relay_env

    # 1. Fleet shows the host online with connection_kind=relay.
    hosts = {h["host_id"]: h for h in _hub_get(env, "/harness/hosts")["hosts"]}
    assert hosts[HOST_ID]["status"] == "online"
    assert hosts[HOST_ID]["connection_kind"] == "relay"

    # 2. Session create through the hub, with NO urls registered: this can
    # only succeed if the hub routed the create over the relay channel.
    session_dir = tmp_path / "session-cwd"
    session_dir.mkdir()
    status, created = _hub_post(
        env,
        f"/harness/hosts/{HOST_ID}/sessions",
        {"harness": "shell", "cwd": str(session_dir)},
    )
    assert status == 201, created
    session_id = created["session_id"]
    assert created["status"] == "running"

    # 3. Terminal attach through hub -> relay channel -> harnessd's /bin/cat
    # pty, which echoes back whatever we write to stdin.
    app = socket.create_connection(("127.0.0.1", env.hub_port), timeout=10)
    try:
        client_handshake(
            app,
            host=f"127.0.0.1:{env.hub_port}",
            path=f"/harness/sessions/{session_id}/terminal",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        attach_deadline = time.monotonic() + 10
        attached = None
        for frame in _recv_frames_until(app, attach_deadline):
            if frame.get("type") == "attached":
                attached = frame
                break
        assert attached is not None, "never received the terminal attach greeting"
        assert attached["session_id"] == session_id

        client_send_json(app, {"type": "input", "data": "hello-relay\n"})

        echo_deadline = time.monotonic() + 15
        echoed = ""
        for frame in _recv_frames_until(app, echo_deadline):
            if frame.get("type") == "output" and isinstance(frame.get("data"), str):
                echoed += frame["data"]
                if "hello-relay" in echoed:
                    break
        assert "hello-relay" in echoed, f"stdin was never echoed back; saw: {echoed!r}"
    finally:
        with contextlib.suppress(OSError, WebSocketClosed):
            app.close()

    # 4. Task 8 pin: the hub's OWN registry (a separate duckdb from
    # harnessd's) must have mirrored a terminal.output event for this
    # session -- proof the event path works end-to-end over relay, not just
    # the raw output stream.
    mirror_deadline = time.monotonic() + 30
    mirrored_output = None
    while time.monotonic() < mirror_deadline and mirrored_output is None:
        events = HarnessRegistry(env.hub_duckdb_path).list_events(session_id)
        for event in events:
            if event.event_type == "terminal.output" and "hello-relay" in (
                event.payload or {}
            ).get("text", ""):
                mirrored_output = event
                break
        if mirrored_output is None:
            time.sleep(0.1)
    if mirrored_output is None:
        # Attributable, not mysterious (issue #90): say what the hub DID
        # mirror. "Only terminal.input landed" means the trailing
        # terminal.output event frame was discarded at detach; a completely
        # empty list means the relay event path never worked at all.
        landed = HarnessRegistry(env.hub_duckdb_path).list_events(session_id)
        raise AssertionError(
            "hub never mirrored a terminal.output event; hub registry holds "
            f"{len(landed)} event(s): {[e.event_type for e in landed]}"
        )

    # 5. Terminate through the hub.
    status, terminated = _hub_post(env, f"/harness/sessions/{session_id}/terminate", {})
    assert status == 200, terminated
    assert terminated["status"] == "terminated"

    # 6. Kill the relay client and hard-close its socket; the hub's presence
    # must flip the host offline within a bounded poll. The reader thread on
    # the hub side notices socket death (EOF/RST) almost immediately via its
    # blocking recv_frame call -- this is not gated on the 20s ping
    # interval -- but we give it generous margin (ping interval + slack) to
    # absorb CI scheduling jitter.
    env.relay_client.stop()
    env.spoke_sock_close()

    offline_deadline = time.monotonic() + 30
    offline = False
    while time.monotonic() < offline_deadline:
        hosts = {h["host_id"]: h for h in _hub_get(env, "/harness/hosts")["hosts"]}
        if hosts[HOST_ID]["status"] == "offline":
            offline = True
            break
        time.sleep(0.5)
    assert offline, "host never flipped offline after the relay socket died"


def test_live_content_consent_and_revoke_round_trip_over_real_relay(relay_env):
    """Consent changes reach an already-running relay daemon without restart."""

    env = relay_env
    status, consent = _hub_post(
        env, "/insights/content-analysis/consent", {"backend": "local"}
    )
    assert status == 200, consent
    assert consent["propagation"] == "complete"
    assert env.harnessd_state.content_consent.snapshot() == {
        "enabled": True,
        "epoch": consent["consent_epoch"],
    }
    bundle = env.hub_collector.fetch_advisory_content_bundle(HOST_ID, ["AGENTS.md"])
    assert bundle["targets"][0]["target_id"] == "AGENTS.md"
    version = env.hub_collector.fetch_advisory_content_version(HOST_ID, ["AGENTS.md"])
    assert version == {
        "bundle_hash": bundle["bundle_hash"],
        "targets": [
            {
                "target_id": "AGENTS.md",
                "content_hash": bundle["targets"][0]["content_hash"],
            }
        ],
    }

    status, revoked = _hub_post(env, "/insights/content-analysis/revoke", {})
    assert status == 200, revoked
    assert env.harnessd_state.content_consent.snapshot() == {
        "enabled": False,
        "epoch": revoked["consent_epoch"],
    }
    with pytest.raises(RuntimeError, match="disabled"):
        env.hub_collector.fetch_advisory_content_bundle(HOST_ID, ["AGENTS.md"])
    with pytest.raises(RuntimeError, match="disabled"):
        env.hub_collector.fetch_advisory_content_version(HOST_ID, ["AGENTS.md"])
