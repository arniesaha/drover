# Multi-Host Relay (M0–M2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a harnessd host with no inbound reachability (the work laptop) join the Drover fleet by dialing out one persistent WSS connection to the hub, over which the hub multiplexes all its existing request and terminal-attach traffic.

**Architecture:** harnessd gains a relay client that connects out to `{central_url}/harness/relay` and services hub requests by proxying them to its own loopback HTTP server (zero per-endpoint code). The hub gains a `RelayManager` that owns live relay sockets and answers "route this request/terminal-attach to host X" for hosts whose `connection_kind` is `relay`. Tailscale Funnel fronts the hub publicly. Spec: `docs/superpowers/specs/2026-07-28-multihost-relay-ux-design.md`.

**Tech Stack:** Python 3.12 stdlib only (http.server, socket, ssl, threading; ws framing via the existing `src/drover/server/harness/websocket.py` helpers — no ASGI/aiohttp dependency, this is deliberate), DuckDB registry, pytest.

## Global Constraints

- No new Python dependencies. The harness data plane is stdlib-only by design (`websocket.py` module docstring).
- DuckDB: one connection config per file + serialized connects (commit `56bad33`); follow existing `HarnessRegistry(duckdb_path)` usage patterns, never hold long-lived write connections.
- Bearer token auth is mandatory on every hub and harnessd route (the funnel URL is internet-reachable). Never add an unauthenticated endpoint.
- WebSocket RFC 6455: client→server frames MUST be masked, server→client MUST NOT be. `send_json`/`send_frame` are server-role (unmasked); `client_send_json` is client-role (masked).
- Frame protocol names are fixed: `hello`, `req`, `res`, `open`, `opened`, `open_error`, `data`, `close`. Field names: `id`, `method`, `path`, `body`, `status`, `chan`, `message`, `host_id`, `error`.
- `connection_kind` values are exactly `"direct"` and `"relay"`; default `"direct"`.
- Commit after every task. Run the full suite (`pytest -x -q`) before each commit.

---

### Task 1: Land the in-flight iOS presentation + token-usage work (M0)

The working tree has uncommitted changes from the harness-presentation session: two new NexusKit files plus edits across Chat/Sessions/Terminal views and the claude structured parser. Land them before relay work churns anything.

**Files (already modified/created in the working tree — commit, don't write):**
- New: `apps/drover/NexusKit/Sources/NexusKit/HarnessPresentation.swift`, `apps/drover/NexusKit/Sources/NexusKit/TokenUsageSummary.swift`
- Modified: `apps/drover/Drover/Screens/Chat/ChatView.swift`, `apps/drover/Drover/Screens/Chat/MessageBubble.swift`, `apps/drover/Drover/Screens/Launch/LaunchView.swift`, `apps/drover/Drover/Screens/Sessions/SessionRow.swift`, `apps/drover/Drover/Screens/Sessions/SessionsView.swift`, `apps/drover/Drover/Screens/Terminal/TerminalView.swift`, `apps/drover/NexusKit/Sources/NexusKit/ChatModel.swift`, `apps/drover/NexusKit/Sources/NexusKit/Models.swift`, `apps/drover/NexusKit/Tests/NexusKitTests/ChatModelTests.swift`, `apps/drover/NexusKit/Tests/NexusKitTests/ModelsTests.swift`, `src/drover/server/harness/structured/claude.py`, `tests/test_structured_claude.py`

**Interfaces:**
- Produces: `HarnessPresentation(harness:)` (name + SF Symbol per harness) and `TokenUsageSummary` (`compactText`, `contextText`) — Plan 2 (UX) builds on these.

- [ ] **Step 1: Run the Python tests for the parser change**

Run: `cd "/Volumes/M2 1/drover" && python -m pytest tests/test_structured_claude.py -q`
Expected: PASS

- [ ] **Step 2: Run the Swift package tests**

Run: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test 2>&1 | tail -5`
Expected: all suites pass (MockURLProtocol suites are serialized under one root suite — do not parallelize them).

- [ ] **Step 3: Run the full Python suite**

Run: `cd "/Volumes/M2 1/drover" && python -m pytest -x -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
cd "/Volumes/M2 1/drover"
git add apps/drover src/drover/server/harness/structured/claude.py tests/test_structured_claude.py
git commit -m "feat(ios): harness presentation identity + token usage summaries"
git push origin main
```

---

### Task 2: Masked client-role frame helpers in websocket.py

The relay client is a ws *client*, so every frame it sends (including pong replies) must be masked. `recv_json` auto-pongs with unmasked `send_frame` — correct only for the server role. Add client-role helpers.

**Files:**
- Modify: `src/drover/server/harness/websocket.py`
- Test: `tests/test_websocket_helpers.py` (create)

**Interfaces:**
- Produces: `client_send_frame(sock, opcode, payload: bytes = b"") -> None` (masked frame, any opcode); `client_recv_json(sock) -> dict | None` (like `recv_json` but pongs pings with a MASKED pong; raises `WebSocketClosed` on close). `client_send_json` is refactored to delegate to `client_send_frame` (behavior unchanged).
- Consumes: existing `send_frame`, `recv_frame`, `WebSocketClosed`, `OPCODE_*`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_websocket_helpers.py
"""Round-trip tests for client-role (masked) websocket helpers."""
import socket

import pytest

from drover.server.harness.websocket import (
    OPCODE_PING,
    OPCODE_PONG,
    OPCODE_TEXT,
    WebSocketClosed,
    client_recv_json,
    client_send_frame,
    recv_frame,
    send_close,
    send_frame,
    send_json,
)


def _pair() -> tuple[socket.socket, socket.socket]:
    return socket.socketpair()


def test_client_send_frame_is_masked_and_round_trips() -> None:
    client, server = _pair()
    try:
        client_send_frame(client, OPCODE_TEXT, b'{"a": 1}')
        raw = server.recv(2)
        assert raw[1] & 0x80  # mask bit set on client frames
    finally:
        client.close()
        server.close()


def test_client_send_frame_payload_decodes_via_recv_frame() -> None:
    client, server = _pair()
    try:
        client_send_frame(client, OPCODE_TEXT, b'{"a": 1}')
        frame = recv_frame(server)
        assert frame.opcode == OPCODE_TEXT
        assert frame.payload == b'{"a": 1}'
    finally:
        client.close()
        server.close()


def test_client_recv_json_reads_server_text_frame() -> None:
    client, server = _pair()
    try:
        send_json(server, {"kind": "req", "id": "1"})
        assert client_recv_json(client) == {"kind": "req", "id": "1"}
    finally:
        client.close()
        server.close()


def test_client_recv_json_pongs_ping_with_masked_frame() -> None:
    client, server = _pair()
    try:
        send_frame(server, OPCODE_PING, b"hb")
        assert client_recv_json(client) is None  # ping consumed
        pong = recv_frame(server)  # recv_frame unmasks masked frames
        assert pong.opcode == OPCODE_PONG
        assert pong.payload == b"hb"
    finally:
        client.close()
        server.close()


def test_client_recv_json_raises_on_close() -> None:
    client, server = _pair()
    try:
        send_close(server)
        with pytest.raises(WebSocketClosed):
            client_recv_json(client)
    finally:
        client.close()
        server.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_websocket_helpers.py -q`
Expected: FAIL — `ImportError: cannot import name 'client_send_frame'`

- [ ] **Step 3: Implement the helpers**

In `src/drover/server/harness/websocket.py`, add below `client_send_json` (and refactor it to call the new helper):

```python
def client_send_frame(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
    """Send a masked frame (client role). RFC 6455 requires masking client->server."""
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        header = bytes([0x80 | opcode, 0x80 | length])
    elif length < 65536:
        header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", length)
    else:
        header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", length)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(header + mask + masked)


def client_recv_json(sock: socket.socket) -> dict[str, Any] | None:
    """recv_json for the client role: pongs pings with a masked frame."""
    frame = recv_frame(sock)
    if frame.opcode == OPCODE_CLOSE:
        raise WebSocketClosed()
    if frame.opcode == OPCODE_PING:
        client_send_frame(sock, OPCODE_PONG, frame.payload)
        return None
    if frame.opcode == OPCODE_PONG:
        return None
    if frame.opcode != OPCODE_TEXT:
        return None
    loaded = json.loads(frame.payload.decode("utf-8"))
    return loaded if isinstance(loaded, dict) else None
```

Refactor `client_send_json` body to:

```python
def client_send_json(sock: socket.socket, payload: dict[str, Any]) -> None:
    client_send_frame(
        sock, OPCODE_TEXT, json.dumps(payload, sort_keys=True).encode("utf-8")
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_websocket_helpers.py tests/test_harness_daemon.py -q`
Expected: PASS (daemon tests prove the `client_send_json` refactor regressed nothing)

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/harness/websocket.py tests/test_websocket_helpers.py
git commit -m "feat(harness): masked client-role websocket frame helpers for relay"
```

---

### Task 3: Relay frame protocol module

One shared vocabulary for both ends of the relay socket: constructors + a validator. Pure functions, no I/O.

**Files:**
- Create: `src/drover/server/harness/relay_protocol.py`
- Test: `tests/test_relay_protocol.py`

**Interfaces:**
- Produces (all return `dict[str, Any]` ready for `send_json`/`client_send_json`):
  - `hello_frame(host_id: str)` → `{"kind": "hello", "host_id": ...}`
  - `req_frame(request_id: str, method: str, path: str, body: dict | None)` → `{"kind": "req", "id": ..., "method": ..., "path": ..., "body": ...}`
  - `res_frame(request_id: str, status: int, body: str)` → `{"kind": "res", "id": ..., "status": ..., "body": ...}` (`body` is the raw response text, matching `_proxy_harness_request`'s `tuple[int, str]` convention)
  - `open_frame(chan: str, path: str)`, `opened_frame(chan: str)`, `open_error_frame(chan: str, error: str)`
  - `data_frame(chan: str, message: dict)` (terminal ws messages are JSON dicts — carried verbatim under `"message"`)
  - `close_frame(chan: str)`
  - `parse_frame(payload: Any) -> dict[str, Any]` — returns the dict if it has a str `"kind"` in the known set, else raises `RelayProtocolError`
  - `class RelayProtocolError(ValueError)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_relay_protocol.py
import pytest

from drover.server.harness.relay_protocol import (
    RelayProtocolError,
    close_frame,
    data_frame,
    hello_frame,
    open_error_frame,
    open_frame,
    opened_frame,
    parse_frame,
    req_frame,
    res_frame,
)


def test_req_res_round_trip() -> None:
    req = req_frame("abc", "POST", "/sessions", {"harness": "shell"})
    assert parse_frame(req) == {
        "kind": "req",
        "id": "abc",
        "method": "POST",
        "path": "/sessions",
        "body": {"harness": "shell"},
    }
    res = res_frame("abc", 200, '{"session_id": "s1"}\n')
    assert res["status"] == 200
    assert res["body"] == '{"session_id": "s1"}\n'


def test_channel_frames() -> None:
    assert open_frame("c1", "/sessions/s1/terminal")["kind"] == "open"
    assert opened_frame("c1") == {"kind": "opened", "chan": "c1"}
    assert open_error_frame("c1", "no session")["error"] == "no session"
    assert data_frame("c1", {"type": "stdin", "data": "ls\n"})["message"] == {
        "type": "stdin",
        "data": "ls\n",
    }
    assert close_frame("c1") == {"kind": "close", "chan": "c1"}


def test_hello_frame() -> None:
    assert hello_frame("work-laptop") == {"kind": "hello", "host_id": "work-laptop"}


@pytest.mark.parametrize(
    "bad", [None, [], "req", {}, {"kind": "unknown"}, {"kind": 7}]
)
def test_parse_frame_rejects_garbage(bad: object) -> None:
    with pytest.raises(RelayProtocolError):
        parse_frame(bad)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_relay_protocol.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'drover.server.harness.relay_protocol'`

- [ ] **Step 3: Implement the module**

```python
# src/drover/server/harness/relay_protocol.py
"""Frame vocabulary for the hub<->harnessd relay websocket.

One socket carries two families: request/response (hub-initiated API
calls, correlated by ``id``) and channels (terminal attach streams,
correlated by ``chan``). Terminal messages are JSON dicts already, so
``data`` frames carry them verbatim under ``message``.
"""

from __future__ import annotations

from typing import Any

FRAME_KINDS = frozenset(
    {"hello", "req", "res", "open", "opened", "open_error", "data", "close"}
)


class RelayProtocolError(ValueError):
    """A frame that does not conform to the relay vocabulary."""


def hello_frame(host_id: str) -> dict[str, Any]:
    return {"kind": "hello", "host_id": host_id}


def req_frame(
    request_id: str, method: str, path: str, body: dict[str, Any] | None
) -> dict[str, Any]:
    return {"kind": "req", "id": request_id, "method": method, "path": path, "body": body}


def res_frame(request_id: str, status: int, body: str) -> dict[str, Any]:
    return {"kind": "res", "id": request_id, "status": status, "body": body}


def open_frame(chan: str, path: str) -> dict[str, Any]:
    return {"kind": "open", "chan": chan, "path": path}


def opened_frame(chan: str) -> dict[str, Any]:
    return {"kind": "opened", "chan": chan}


def open_error_frame(chan: str, error: str) -> dict[str, Any]:
    return {"kind": "open_error", "chan": chan, "error": error}


def data_frame(chan: str, message: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "data", "chan": chan, "message": message}


def close_frame(chan: str) -> dict[str, Any]:
    return {"kind": "close", "chan": chan}


def parse_frame(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RelayProtocolError(f"relay frame must be an object, got {type(payload)}")
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in FRAME_KINDS:
        raise RelayProtocolError(f"unknown relay frame kind: {kind!r}")
    return payload
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_relay_protocol.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/harness/relay_protocol.py tests/test_relay_protocol.py
git commit -m "feat(harness): relay frame protocol vocabulary"
```

---

### Task 4: registry + schema + models grow `connection_kind`

**Files:**
- Modify: `src/drover/server/harness/schema.py` (hosts DDL + `_ensure_harness_columns` for `harness_hosts`)
- Modify: `src/drover/server/harness/models.py:20-46` (`HarnessHost`)
- Modify: `src/drover/server/harness/registry.py:75-121` (`register_host`)
- Modify: `src/drover/server/metrics.py:445-469` (`register_harness_host` payload passthrough)
- Test: extend `tests/test_harness_registry.py`

**Interfaces:**
- Produces: `HarnessHost.connection_kind: str` (default `"direct"`); `HarnessRegistry.register_host(..., connection_kind: str = "direct")`; hub `POST /harness/hosts` accepts optional `"connection_kind"` in the payload.
- Consumed by: Task 6 (relay attach validates registered kind), Task 7 (routing), Task 9 (fleet JSON), Task 10 (harnessd sends it).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_harness_registry.py` (follow the file's existing fixture pattern for building a registry against a tmp DuckDB path):

```python
def test_register_host_persists_connection_kind(tmp_path) -> None:
    registry = HarnessRegistry(tmp_path / "meta.duckdb")
    host = registry.register_host(
        host_id="work-laptop",
        display_name="Work Laptop",
        kind="mac",
        connection_kind="relay",
    )
    assert host.connection_kind == "relay"
    fetched = registry.get_host("work-laptop")
    assert fetched is not None and fetched.connection_kind == "relay"


def test_register_host_defaults_connection_kind_direct(tmp_path) -> None:
    registry = HarnessRegistry(tmp_path / "meta.duckdb")
    host = registry.register_host(
        host_id="mini", display_name="Mac Mini", kind="mac"
    )
    assert host.connection_kind == "direct"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_harness_registry.py -q`
Expected: FAIL — `TypeError: register_host() got an unexpected keyword argument 'connection_kind'`

- [ ] **Step 3: Implement**

1. `schema.py`: add `connection_kind VARCHAR` to `_HARNESS_HOSTS_DDL` and, in `bootstrap_harness_tables`, add an `_ensure_harness_columns(con, "harness_hosts", {"connection_kind": "VARCHAR"})` call right after the hosts DDL executes (existing DBs upgrade in place — same pattern the sessions/events tables already use).
2. `models.py` `HarnessHost`: add field `connection_kind: str = "direct"`; in `from_row`, `connection_kind=row.get("connection_kind") or "direct"`.
3. `registry.py` `register_host`: add keyword param `connection_kind: str = "direct"`, add the column to the INSERT column list, VALUES placeholders, and the `ON CONFLICT ... DO UPDATE SET connection_kind = excluded.connection_kind` clause, mirroring how `local_url` is handled.
4. `metrics.py` `register_harness_host`: pass `connection_kind=str(payload.get("connection_kind") or "direct")` through to `registry.register_host`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_harness_registry.py tests/test_metrics.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/harness/schema.py src/drover/server/harness/models.py src/drover/server/harness/registry.py src/drover/server/metrics.py tests/test_harness_registry.py
git commit -m "feat(harness): connection_kind column on harness hosts"
```

---

### Task 5: Hub RelayManager

The hub-side owner of live relay sockets. `attach()` receives an **already-upgraded** server-role socket (the HTTP handler does the 101 handshake in Task 6), so unit tests can drive the far end of a `socket.socketpair()` with client-role helpers — no real handshake needed.

**Files:**
- Create: `src/drover/server/relay_manager.py`
- Test: `tests/test_relay_manager.py`

**Interfaces:**
- Produces:
  - `class RelayChannel`: `send(message: dict) -> None` (data frame to spoke), `recv(timeout_s: float) -> dict | None` (`None` on timeout), `close() -> None`, `closed: bool`
  - `class RelayManager`:
    - `attach(host_id: str, sock: socket.socket) -> None` — spawns a reader thread; replaces any prior connection for that host (closing it)
    - `is_live(host_id: str) -> bool`
    - `live_host_ids() -> set[str]`
    - `request(host_id: str, method: str, path: str, body: dict | None, timeout_s: float = 15) -> tuple[int, str]` — `(502, json-error-text)` if host not live or timeout/socket death, matching `_proxy_harness_request`'s error convention
    - `open_channel(host_id: str, path: str, timeout_s: float = 10) -> RelayChannel` — raises `RelayUnavailable` if not live / open_error / timeout
    - `class RelayUnavailable(RuntimeError)`
  - On socket death: all pending `request()` futures resolve `(502, ...)`, all channels close, `is_live` flips false immediately.
  - Keepalive: reader thread side sends `OPCODE_PING` every 20s from a timer; `recv_json` on the server role already handles the client's pong (returns `None`).
- Consumes: `send_json`, `recv_json`, `send_frame`, `OPCODE_PING`, `WebSocketClosed` from `websocket.py`; frame constructors + `parse_frame` from `relay_protocol.py` (Task 3).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_relay_manager.py
"""Drive RelayManager over a socketpair; this test plays the spoke."""
import json
import socket
import threading

import pytest

from drover.server.harness.relay_protocol import (
    data_frame,
    open_error_frame,
    opened_frame,
    res_frame,
)
from drover.server.harness.websocket import client_recv_json, client_send_json
from drover.server.relay_manager import RelayManager, RelayUnavailable


def _attach(manager: RelayManager, host_id: str = "laptop"):
    hub_side, spoke_side = socket.socketpair()
    manager.attach(host_id, hub_side)
    return spoke_side


def _spoke_recv(spoke: socket.socket) -> dict:
    while True:
        frame = client_recv_json(spoke)
        if frame is not None:
            return frame


def test_request_round_trip() -> None:
    manager = RelayManager()
    spoke = _attach(manager)

    def spoke_loop() -> None:
        frame = _spoke_recv(spoke)
        assert frame["kind"] == "req"
        assert frame["method"] == "GET"
        assert frame["path"] == "/sessions"
        client_send_json(spoke, res_frame(frame["id"], 200, '{"sessions": []}\n'))

    thread = threading.Thread(target=spoke_loop, daemon=True)
    thread.start()
    status, body = manager.request("laptop", "GET", "/sessions", None, timeout_s=5)
    assert status == 200
    assert json.loads(body) == {"sessions": []}
    thread.join(timeout=5)


def test_request_to_unknown_host_is_502() -> None:
    manager = RelayManager()
    status, body = manager.request("ghost", "GET", "/sessions", None, timeout_s=1)
    assert status == 502
    assert "not connected" in body


def test_presence_flips_on_socket_death() -> None:
    manager = RelayManager()
    spoke = _attach(manager)
    assert manager.is_live("laptop")
    spoke.close()
    # request() must fail fast once the reader notices the dead socket
    status, _ = manager.request("laptop", "GET", "/sessions", None, timeout_s=5)
    assert status == 502
    assert not manager.is_live("laptop")
    assert "laptop" not in manager.live_host_ids()


def test_channel_open_data_close() -> None:
    manager = RelayManager()
    spoke = _attach(manager)

    def spoke_loop() -> None:
        frame = _spoke_recv(spoke)
        assert frame["kind"] == "open"
        chan = frame["chan"]
        client_send_json(spoke, opened_frame(chan))
        client_send_json(spoke, data_frame(chan, {"type": "output", "data": "hi"}))
        incoming = _spoke_recv(spoke)
        assert incoming == data_frame(chan, {"type": "stdin", "data": "ls\n"})

    thread = threading.Thread(target=spoke_loop, daemon=True)
    thread.start()
    channel = manager.open_channel("laptop", "/sessions/s1/terminal", timeout_s=5)
    assert channel.recv(timeout_s=5) == {"type": "output", "data": "hi"}
    channel.send({"type": "stdin", "data": "ls\n"})
    thread.join(timeout=5)
    channel.close()
    assert channel.closed


def test_channel_open_error_raises() -> None:
    manager = RelayManager()
    spoke = _attach(manager)

    def spoke_loop() -> None:
        frame = _spoke_recv(spoke)
        client_send_json(spoke, open_error_frame(frame["chan"], "unknown session"))

    threading.Thread(target=spoke_loop, daemon=True).start()
    with pytest.raises(RelayUnavailable):
        manager.open_channel("laptop", "/sessions/nope/terminal", timeout_s=5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_relay_manager.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'drover.server.relay_manager'`

- [ ] **Step 3: Implement RelayManager**

`src/drover/server/relay_manager.py`, single file, ~200 lines. Structure:

```python
"""Hub-side owner of live relay websocket connections, keyed by host_id."""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
import uuid
from typing import Any

from drover.server.harness.relay_protocol import (
    RelayProtocolError,
    close_frame,
    data_frame,
    open_frame,
    parse_frame,
    req_frame,
)
from drover.server.harness.websocket import (
    OPCODE_PING,
    WebSocketClosed,
    recv_json,
    send_frame,
    send_json,
)

log = logging.getLogger("drover.relay")

PING_INTERVAL_S = 20.0


class RelayUnavailable(RuntimeError):
    """No live relay connection can satisfy this call."""
```

Implementation notes the engineer must follow:

- `_Connection` (internal): holds `sock`, a `threading.Lock` for writes (many hub threads share one socket — every `send_json` must hold it), `pending: dict[str, queue.Queue]` for req/res correlation, `channels: dict[str, RelayChannel]`, and `alive: threading.Event`.
- Reader thread per connection: loop `recv_json(sock)` (server role — clients' pongs come back as `None`, skip them); `parse_frame` each dict; dispatch: `res` → `pending.pop(id).put((status, body))`; `data` → `channels[chan]._incoming.put(message)`; `opened`/`open_error` → resolve the open-wait queue stored in `pending` under `f"open:{chan}"`; `close` → close that channel. On `WebSocketClosed`/`OSError`/`RelayProtocolError`: call `_teardown(connection)`.
- `_teardown`: set `alive` false, fail every pending queue with `(502, json.dumps({"error": "relay connection lost"}) + "\n")`, close all channels, close the socket, and remove the host entry iff it still points at this connection.
- Ping thread per connection: every `PING_INTERVAL_S`, `send_frame(sock, OPCODE_PING, b"hb")` under the write lock; on `OSError` → `_teardown`. The ping is what turns a silently-dead TCP path into a detected disconnect within ~20s.
- `attach`: if the host already has a live connection, tear the old one down first (newest wins — a reconnecting spoke must not be blocked by its own zombie).
- `request`: not live → `(502, json.dumps({"error": f"relay host not connected: {host_id}"}) + "\n")`. Else register a `queue.Queue(maxsize=1)` under a `uuid4().hex` id, send `req_frame(...)` under the write lock, `queue.get(timeout=timeout_s)`; `queue.Empty` → `(502, ...timeout error...)` and drop the pending entry.
- `RelayChannel.send`: `send_json(sock, data_frame(chan, message))` under the write lock; if dead, raise `RelayUnavailable`. `recv`: `self._incoming.get(timeout=timeout_s)` returning `None` on `queue.Empty`. `close`: idempotent; sends `close_frame` (best-effort), marks `closed`, unregisters from the connection.
- `open_channel`: allocate `chan = uuid.uuid4().hex`, create the channel object and an open-wait queue, send `open_frame`, wait `timeout_s` for `opened` (return channel) or `open_error` (raise `RelayUnavailable(error)`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_relay_manager.py -q`
Expected: PASS (run twice to shake out thread-timing flakes: `-q --count=2` is not available; just run the file twice)

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/relay_manager.py tests/test_relay_manager.py
git commit -m "feat(server): RelayManager for hub-side relay connections"
```

---

### Task 6: Hub `/harness/relay` websocket endpoint

**Files:**
- Modify: `src/drover/server/web/app.py` (route in `do_GET` near the `/terminal` route at line 212; new method `_accept_relay_websocket`; `start_metrics_server` at line 754 constructs the manager)
- Modify: `src/drover/server/metrics.py` (collector gains `relay_manager` attribute, default `None`)
- Test: `tests/test_relay_endpoint.py`

**Interfaces:**
- Produces: `GET /harness/relay` (Bearer-gated, websocket upgrade). After the 101, the hub reads exactly one `hello` frame (client-masked) to learn `host_id`, then calls `collector.relay_manager.attach(host_id, sock)`. `MetricsCollector.relay_manager: RelayManager | None = None` set by `start_metrics_server`.
- Consumes: `RelayManager.attach` (Task 5), `accept_key` + `recv_json` from `websocket.py`, the existing `_gate`/auth pattern in `_MetricsHandler` (`web/app.py:114`).

- [ ] **Step 1: Write the failing test**

Follow the existing server-boot pattern in `tests/test_server_e2e.py` (start_metrics_server on port 0 with a tmp DuckDB + api token — copy its fixture shape):

```python
# tests/test_relay_endpoint.py
import socket

from drover.server.harness.relay_protocol import hello_frame
from drover.server.harness.websocket import client_handshake, client_send_json


def test_relay_upgrade_registers_live_host(metrics_server):  # fixture per test_server_e2e.py
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
    import time
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
        import pytest
        with pytest.raises(RuntimeError):  # handshake sees non-101 status
            client_handshake(sock, host=f"{host}:{port}", path="/harness/relay")
    finally:
        sock.close()
```

(If `test_server_e2e.py` exposes no reusable fixture, build one in this file: `start_metrics_server` with `port=0`, a tmp `duckdb_path`, and `api_token="test-token"`, yielding the server object + collector; shut down in teardown.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_relay_endpoint.py -q`
Expected: FAIL — handshake gets 404 (route missing), surfaced as `RuntimeError: websocket handshake failed`

- [ ] **Step 3: Implement the endpoint**

1. `metrics.py`: on `MetricsCollector`, add class attribute `relay_manager: "RelayManager | None" = None` (import under `TYPE_CHECKING` to avoid a runtime cycle).
2. `web/app.py` `start_metrics_server`: `from drover.server.relay_manager import RelayManager`; after the collector is constructed, `collector.relay_manager = RelayManager()`.
3. `web/app.py` `do_GET`: before the `/harness/sessions/.../terminal` route, add:

```python
if path == "/harness/relay":
    self._accept_relay_websocket()
    return
```

4. New method `_accept_relay_websocket`, modeled line-for-line on the upgrade half of `_proxy_terminal_websocket` (`web/app.py:420-485`): check Bearer token (same check `_gate` uses — relay must NEVER be open), 426 if no `Upgrade: websocket`, 400 if no `Sec-WebSocket-Key`, then `protocol_version = "HTTP/1.1"`, `send_response(101)` + `Upgrade`/`Connection`/`Sec-WebSocket-Accept` headers, `close_connection = True`. Then read the hello: `frame = recv_json(self.connection)` in a short loop (skip `None`) with a 10s `settimeout`; validate via `parse_frame` and `kind == "hello"`; finally `self.collector.relay_manager.attach(frame["host_id"], self.connection)` and **return without closing the socket** (the manager owns it now — do not let the handler's normal cleanup close it; `close_connection = True` only ends HTTP keep-alive handling, but verify the handler does not `shutdown` the socket on return; if it does, detach with `self.connection = None` after attach, the same hijack trick `_proxy_terminal_websocket` relies on).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_relay_endpoint.py tests/test_server_e2e.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/web/app.py src/drover/server/metrics.py tests/test_relay_endpoint.py
git commit -m "feat(server): /harness/relay websocket endpoint feeding RelayManager"
```

---

### Task 7: Route hub→harnessd API requests via relay when live

Every proxy call in the collector currently builds a URL from `_harness_endpoint(host)` and dials it. Introduce one routing choke point and convert all call sites.

**Files:**
- Modify: `src/drover/server/metrics.py` — new method `_harness_request(host, path, *, method, payload=None, timeout_s=15)`; convert the call sites in `proxy_create_harness_session` (:471), `proxy_terminate_harness_session` (:491), `proxy_harness_session_action` (:534), `proxy_harness_native_sessions` (:566), `proxy_harness_auth` (:597), `proxy_harness_native_transcript` (:637), `_reconcile_harness_session_from_host` (:766-790)
- Test: `tests/test_metrics.py` (extend)

**Interfaces:**
- Produces: `MetricsCollector._harness_request(host: HarnessHost, path: str, *, method: str, payload: Mapping | None = None, timeout_s: float = 15) -> tuple[int, str]` — `path` is host-relative (e.g. `/sessions`, `/sessions/{id}/terminate`). Routing rule: if `self.relay_manager is not None and self.relay_manager.is_live(host.host_id)` → `relay_manager.request(host.host_id, method, path, dict(payload or {}), timeout_s=timeout_s)`; else if `_harness_endpoint(host)` non-empty → existing `_proxy_harness_request(f"{endpoint}{path}", ...)`; else `(502, '{"error": "harness host has no reachable endpoint: <host_id>"}')`.
- Consumes: `RelayManager.request` (Task 5), `_harness_endpoint` (:294), `_proxy_harness_request` (:1019).

- [ ] **Step 1: Write the failing test**

Extend `tests/test_metrics.py` (reuse its existing collector fixture pattern):

```python
class _FakeRelay:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def is_live(self, host_id: str) -> bool:
        return host_id == "laptop"

    def request(self, host_id, method, path, body, timeout_s=15):
        self.calls.append((host_id, method, path, body))
        return 200, '{"ok": true}\n'


def test_harness_request_prefers_live_relay(collector_with_hosts) -> None:
    collector = collector_with_hosts  # registers host "laptop" with connection_kind="relay", no URLs
    fake = _FakeRelay()
    collector.relay_manager = fake
    host = collector._harness_host("laptop")
    status, body = collector._harness_request(host, "/sessions", method="GET")
    assert status == 200
    assert fake.calls == [("laptop", "GET", "/sessions", {})]


def test_harness_request_falls_back_to_direct_url(collector_with_hosts) -> None:
    collector = collector_with_hosts  # also registers "mini" with local_url of a dead port
    collector.relay_manager = _FakeRelay()  # not live for "mini"
    host = collector._harness_host("mini")
    status, _ = collector._harness_request(host, "/sessions", method="GET", timeout_s=0.2)
    assert status == 502  # tried the direct URL and it refused — proves the direct path ran


def test_harness_request_no_endpoint_no_relay_is_502(collector_with_hosts) -> None:
    collector = collector_with_hosts  # registers "island" with no URLs
    host = collector._harness_host("island")
    status, body = collector._harness_request(host, "/sessions", method="GET")
    assert status == 502
    assert "no reachable endpoint" in body
```

Build the `collector_with_hosts` fixture in the test file if one does not exist: construct the collector the way `tests/test_metrics.py` already does, then `registry.register_host(...)` three hosts as commented.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_metrics.py -q -k harness_request`
Expected: FAIL — `AttributeError: 'MetricsCollector' object has no attribute '_harness_request'`

- [ ] **Step 3: Implement `_harness_request` and convert the seven call sites**

Add the method next to `_proxy_harness_request`. Then convert each call site to `self._harness_request(host, "/<suffix>", method=..., payload=...)` — e.g. `proxy_create_harness_session` drops its own `_harness_endpoint`/empty-endpoint check and becomes a single `_harness_request(host, "/sessions", method="POST", payload=payload)` call. Preserve each site's existing behavior on 2xx (e.g. `_sync_created_harness_session`). Keep `_proxy_harness_request` itself unchanged (it is the direct-dial half).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_metrics.py tests/test_server_e2e.py -q`
Expected: PASS (e2e proves direct-mode behavior is unchanged end to end)

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/metrics.py tests/test_metrics.py
git commit -m "feat(server): route harness API calls via relay when host is relay-connected"
```

---

### Task 8: Terminal attach over a relay channel

**Files:**
- Modify: `src/drover/server/web/app.py:420-523` (`_proxy_terminal_websocket`)
- Modify: `src/drover/server/metrics.py:750-764` (`harness_terminal_endpoint` — split so the handler can learn the host first)
- Test: `tests/test_relay_terminal.py`

**Interfaces:**
- Produces: `MetricsCollector.harness_terminal_route(session_id: str) -> tuple[HarnessHost, str] | None` — returns `(host, "/sessions/{id}/terminal")` after the same reconcile/status/host checks `harness_terminal_endpoint` does today (keep `harness_terminal_endpoint` as a thin wrapper returning the joined URL for any other caller). In `_proxy_terminal_websocket`: resolve the route; if `relay_manager.is_live(host.host_id)` → `channel = relay_manager.open_channel(host.host_id, path)` and bridge browser↔channel; else existing direct socket bridge, byte-identical.
- Consumes: `RelayManager.open_channel` / `RelayChannel` (Task 5).
- Bridge loops (mirror the existing `browser_to_upstream`/`upstream_to_browser` thread pair at `web/app.py:486+`): browser→channel reads `recv_json(browser)` and calls `channel.send(message)`; channel→browser reads `channel.recv(timeout_s=0.25)` and calls `send_json(browser, message)`; either side closing (`WebSocketClosed`, `RelayUnavailable`, `channel.closed`) stops both and closes the channel + browser socket.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_relay_terminal.py
"""Bridge a fake app websocket to a fake relay spoke through the hub."""
import socket
import threading

from drover.server.harness.relay_protocol import data_frame, opened_frame
from drover.server.harness.websocket import (
    client_handshake,
    client_recv_json,
    client_send_json,
)
```

The test needs the full server fixture from Task 6 plus a registered relay host and a seeded session row in `created` status (insert via `HarnessRegistry` directly — see how `tests/test_server_e2e.py` seeds sessions). Test body:

```python
def test_terminal_attach_bridges_over_relay(metrics_server_with_relay_host):
    env = metrics_server_with_relay_host  # server + connected fake spoke + session "s1" on host "laptop"
    spoke = env.spoke_sock

    def spoke_loop() -> None:
        frame = _spoke_recv(spoke)  # same helper as test_relay_manager.py
        assert frame["kind"] == "open"
        assert frame["path"] == "/sessions/s1/terminal"
        chan = frame["chan"]
        client_send_json(spoke, opened_frame(chan))
        client_send_json(spoke, data_frame(chan, {"type": "output", "data": "$ "}))
        echo = _spoke_recv(spoke)
        assert echo["message"] == {"type": "stdin", "data": "ls\n"}

    threading.Thread(target=spoke_loop, daemon=True).start()

    app = socket.create_connection((env.host, env.port), timeout=5)
    client_handshake(
        app,
        host=f"{env.host}:{env.port}",
        path="/harness/sessions/s1/terminal",
        headers={"Authorization": f"Bearer {env.token}"},
    )
    first = None
    while first is None:
        first = client_recv_json(app)
    assert first == {"type": "output", "data": "$ "}
    client_send_json(app, {"type": "stdin", "data": "ls\n"})
    app.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_relay_terminal.py -q`
Expected: FAIL — hub 502s the attach ("harness websocket upstream failed": it tried to dial the relay host's nonexistent URL)

- [ ] **Step 3: Implement the split + relay branch**

As specified in Interfaces. The relay branch replaces only the "connect upstream" section (`web/app.py:447-473`); the 101-to-browser section and the two pump threads are restructured so both flavors share the browser-side logic — extract the browser 101 into a small helper `_upgrade_browser_websocket(self) -> socket | None` used by both.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_relay_terminal.py tests/test_server_e2e.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/web/app.py src/drover/server/metrics.py tests/test_relay_terminal.py
git commit -m "feat(server): terminal attach bridges over relay channels"
```

---

### Task 9: Fleet JSON reports relay presence truthfully

**Files:**
- Modify: `src/drover/server/metrics.py:428` (`render_harness_json`) and the host-serialization it uses
- Test: `tests/test_metrics.py` (extend)

**Interfaces:**
- Produces: each host object in `GET /harness` / `GET /harness/hosts` JSON gains `"connection_kind"` and its `"status"` obeys: relay hosts are `"online"` iff `relay_manager.is_live(host_id)` else `"offline"` (overriding the stored row status — a relay host's socket is ground truth); direct hosts keep stored status. `last_seen_at` continues to serialize as today.
- Consumes: `HarnessHost.connection_kind` (Task 4), `RelayManager.is_live` (Task 5).

- [ ] **Step 1: Write the failing test**

```python
def test_fleet_json_overrides_relay_status_from_socket(collector_with_hosts) -> None:
    collector = collector_with_hosts  # "laptop" registered connection_kind="relay", status "online"
    collector.relay_manager = _FakeRelay()  # is_live: only "laptop"

    import json as _json
    payload = _json.loads(collector.render_harness_json(include_sessions=False))
    hosts = {h["host_id"]: h for h in payload["hosts"]}
    assert hosts["laptop"]["connection_kind"] == "relay"
    assert hosts["laptop"]["status"] == "online"

    collector.relay_manager = None  # no live sockets at all
    payload = _json.loads(collector.render_harness_json(include_sessions=False))
    hosts = {h["host_id"]: h for h in payload["hosts"]}
    assert hosts["laptop"]["status"] == "offline"  # stored "online" must not leak
    assert hosts["mini"]["status"] == "online"      # direct host keeps stored status
```

(Adjust the top-level key to whatever `render_harness_json` actually emits — read the method first; if hosts are nested differently, index accordingly and note it in the test.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_metrics.py -q -k fleet_json`
Expected: FAIL — no `connection_kind` key / status not overridden

- [ ] **Step 3: Implement the override in the host serialization path inside `render_harness_json`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_metrics.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/metrics.py tests/test_metrics.py
git commit -m "feat(server): fleet JSON carries connection_kind and socket-truth relay presence"
```

---

### Task 10: harnessd relay client + `--relay` flag

The spoke side: one thread that connects out, services `req` frames via loopback HTTP, and pumps terminal channels via loopback websockets. Reconnects forever with jittered backoff.

**Files:**
- Create: `src/drover/server/harness/relay_client.py`
- Modify: `src/drover/server/harness/daemon.py:2364-2376` (`register_daemon_host_remote` payload gains `connection_kind`), `:2446` (`run_harnessd` gains `relay: bool = False`, starts the client)
- Modify: `src/drover/server/harness/cli.py` (`--relay` flag threaded through `run_harnessd_from_options`)
- Test: `tests/test_relay_client.py`

**Interfaces:**
- Produces: `class RelayClient` with `RelayClient(central_url: str, host_id: str, token: str, loopback_port: int)`, `.start() -> threading.Thread` (daemon thread running `run_forever`), `.stop()`, and (for tests) `.serve_connection(sock) -> None` which runs the frame loop on an already-connected socket until it dies. `run_harnessd(..., relay: bool = False)`; `drover-harnessd --relay` CLI flag. When `relay=True`, `register_daemon_host_remote` sends `"connection_kind": "relay"` (else `"direct"`).
- Consumes: `client_handshake`, `client_send_json`, `client_recv_json`, `WebSocketClosed` (Task 2); `relay_protocol` constructors + `parse_frame` (Task 3); loopback dispatch via `http.client.HTTPConnection("127.0.0.1", loopback_port)`.
- Behavior spec:
  - **Connect:** parse `central_url`; port 443/https → `ssl.create_default_context().wrap_socket(raw, server_hostname=host)`; `client_handshake(sock, host=netloc, path="/harness/relay", headers={"Authorization": f"Bearer {token}"})`; then `client_send_json(sock, hello_frame(host_id))`.
  - **`req` frames:** run each in a small worker thread (a slow `/sessions` create must not block terminal `data` frames): loopback `HTTPConnection.request(method, path, body=json.dumps(body or {}), headers={"Authorization": Bearer, "Content-Type": "application/json"})`; reply `res_frame(id, response.status, response_text)`. On loopback failure reply `res_frame(id, 502, '{"error": "loopback request failed: ..."}')` — never crash the frame loop.
  - **`open` frames:** open a loopback ws client (`socket.create_connection(("127.0.0.1", loopback_port))` + `client_handshake(path=frame["path"], headers=Bearer)`); on success send `opened_frame(chan)` and start a pump thread reading the local terminal ws (`recv_frame`-based server-role frames arrive unmasked → use `client_recv_json`... note: harnessd's terminal endpoint is a *server*, so its frames to us are unmasked; our frames to it must be masked → `client_send_json`) forwarding each message as `data_frame(chan, message)` to the hub; on failure send `open_error_frame(chan, str(exc))`.
  - **`data` frames from hub:** look up the channel's loopback ws, `client_send_json(local_ws, frame["message"])`.
  - **`close` frames / local ws EOF:** close the loopback ws, send `close_frame(chan)` if hub didn't initiate, forget the channel.
  - **All writes to the hub socket** go through one `threading.Lock` (frame loop thread + N req workers + N pump threads share it).
  - **Reconnect:** `run_forever` wraps connect+serve in `while not stopped:`; on any exception sleep `min(300, 2**attempt) * uniform(0.5, 1.5)` seconds; reset `attempt` after a connection that survived >60s. Log the first failure at WARNING, subsequent at DEBUG (a laptop offline for a weekend must not fill logs).
- `run_harnessd` wiring: after `start_remote_heartbeat(state)`, add — when `relay and state.central_url and state.api_token` — `RelayClient(state.central_url, state.host_id, state.api_token, listen_port).start()`; if `relay` is set without `central_url`/token, log an ERROR and continue serving locally. Stop the client in the `finally` block.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_relay_client.py
"""Drive RelayClient.serve_connection over a socketpair; this test plays the hub."""
import json
import socket
import threading

from drover.server.harness.relay_protocol import open_frame, req_frame
from drover.server.harness.websocket import recv_json, send_json
from drover.server.harness.relay_client import RelayClient


def test_req_frame_dispatches_to_loopback_and_answers(harnessd_server):
    # harnessd_server: the in-process HarnessHTTPServer fixture pattern from
    # tests/test_harness_daemon.py (port 0, api_token "test-token")
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
```

Build `harnessd_server` by copying the in-process daemon fixture pattern from `tests/test_harness_daemon.py` (state with tmp DuckDB, `create_harness_server(listen_host="127.0.0.1", listen_port=0, state=state)`, serve in a thread, yield, shutdown).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_relay_client.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'drover.server.harness.relay_client'`

- [ ] **Step 3: Implement `relay_client.py` per the behavior spec above, then wire `daemon.py` and `cli.py`**

`register_daemon_host_remote` change (daemon.py:2367): payload gains `"connection_kind": "relay" if state.relay else "direct"` — add `relay: bool = False` to `HarnessDaemonState` (daemon.py:1091) so the flag is visible where the payload is built.

`cli.py`: add `@click.option("--relay", is_flag=True, default=False, help="Dial out to --central-url instead of relying on inbound reachability")`, thread through `run_harnessd_from_options` → `run_harnessd`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_relay_client.py tests/test_harness_daemon.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/harness/relay_client.py src/drover/server/harness/daemon.py src/drover/server/harness/cli.py tests/test_relay_client.py
git commit -m "feat(harness): outbound relay client mode for harnessd (--relay)"
```

---

### Task 11: End-to-end integration test — full lifecycle over relay

Everything in-process: real hub (`start_metrics_server`), real harnessd (`create_harness_server`), real `RelayClient` connected through the hub's real `/harness/relay` endpoint. No sockets faked.

**Files:**
- Test: `tests/test_relay_e2e.py` (create)

**Interfaces:**
- Consumes: every prior task. This is the merge gate for the whole relay feature.

- [ ] **Step 1: Write the test**

```python
# tests/test_relay_e2e.py
"""Hub + harnessd + RelayClient wired together in-process.

The harnessd host registers over the hub API with connection_kind=relay and
NO urls — every subsequent hub->host interaction can only work via relay.
"""
import json
import socket
import time
import urllib.request

from drover.server.harness.websocket import client_handshake, client_recv_json, client_send_json


def _hub_get(env, path: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{env.hub_port}{path}",
        headers={"Authorization": f"Bearer {env.token}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _hub_post(env, path: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{env.hub_port}{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {env.token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.status, json.loads(response.read())


def test_full_session_lifecycle_over_relay(relay_env, tmp_path):
    """relay_env fixture (build in this file):
    1. start hub via start_metrics_server(port=0, tmp duckdb, token)
    2. start harnessd via create_harness_server(port=0, own tmp duckdb, same token)
       with a preset for a plain shell harness (command ["/bin/cat"] works: it
       echoes stdin and exits on EOF -- see how tests/test_harness_daemon.py
       fakes harness commands)
    3. register the host on the hub: POST /harness/hosts with
       {"host_id": "laptop", "connection_kind": "relay"} and no urls
    4. start RelayClient(central_url=f"http://127.0.0.1:{hub_port}",
       host_id="laptop", token=token, loopback_port=harnessd_port).start()
       (RelayClient must accept plain http for tests -- no ssl wrap when
       scheme is http)
    5. wait until hub fleet shows laptop online
    """
    env = relay_env

    # presence: relay socket is live
    hosts = {h["host_id"]: h for h in _hub_get(env, "/harness/hosts")["hosts"]}
    assert hosts["laptop"]["status"] == "online"
    assert hosts["laptop"]["connection_kind"] == "relay"

    # create a session on the relay host THROUGH the hub
    status, created = _hub_post(
        env,
        "/harness/hosts/laptop/sessions",
        {"harness": "shell", "cwd": str(tmp_path)},
    )
    assert status == 200
    session_id = created["session_id"]

    # terminal attach through hub -> relay channel -> harnessd pty
    app = socket.create_connection(("127.0.0.1", env.hub_port), timeout=10)
    client_handshake(
        app,
        host=f"127.0.0.1:{env.hub_port}",
        path=f"/harness/sessions/{session_id}/terminal",
        headers={"Authorization": f"Bearer {env.token}"},
    )
    client_send_json(app, {"type": "stdin", "data": "hello-relay\n"})
    deadline = time.monotonic() + 15
    echoed = ""
    while time.monotonic() < deadline and "hello-relay" not in echoed:
        frame = client_recv_json(app)
        if frame and isinstance(frame.get("data"), str):
            echoed += frame["data"]
    assert "hello-relay" in echoed
    app.close()

    # terminate through the hub
    status, _ = _hub_post(env, f"/harness/sessions/{session_id}/terminate", {})
    assert status == 200

    # kill the relay client; hub presence must flip offline within ~ping interval
    env.relay_client.stop()
    env.spoke_sock_close()  # fixture closes the client's socket hard
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        hosts = {h["host_id"]: h for h in _hub_get(env, "/harness/hosts")["hosts"]}
        if hosts["laptop"]["status"] == "offline":
            break
        time.sleep(0.5)
    assert hosts["laptop"]["status"] == "offline"
```

Adapt exact terminal message field names (`type`/`data`) to what `_terminal_loop` (daemon.py:1999) actually speaks — read it first and match; same for the create-session payload/response shape (`_create_session`, daemon.py:1335) and the hub's create route (`/harness/hosts/{id}/sessions`, web/app.py:337). The RelayClient http-scheme support belongs in Task 10's connect logic (scheme `http` → no ssl wrap) — if it was missed, add it now as part of making this pass.

- [ ] **Step 2: Run the test — iterate until green**

Run: `python -m pytest tests/test_relay_e2e.py -q -x`
Expected: PASS. This test legitimately shakes out integration bugs from Tasks 5–10; fix them here, adding a unit test in the relevant task's test file for any bug class it exposes.

- [ ] **Step 3: Run the entire suite**

Run: `python -m pytest -x -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_relay_e2e.py
git commit -m "test(relay): in-process hub+harnessd+client end-to-end lifecycle"
git push origin main
```

---

### Task 12: Enroll script + launchd template + docs

**Files:**
- Create: `scripts/enroll-host.sh`
- Create: `scripts/launchd/com.drover.harnessd-relay.plist.template`
- Create: `docs/multi-host.md`

**Interfaces:**
- Consumes: `drover-harnessd --relay --central-url ... --host-id ...` (Task 10).
- Produces: a one-command host enrollment used in Task 13.

- [ ] **Step 1: Write `scripts/enroll-host.sh`**

```bash
#!/usr/bin/env bash
# Enroll this machine as a Drover harness host.
# Usage: ./scripts/enroll-host.sh --host-id work-laptop --central-url https://mini.tailnet.ts.net [--relay]
set -euo pipefail

HOST_ID="" CENTRAL_URL="" RELAY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host-id) HOST_ID="$2"; shift 2 ;;
    --central-url) CENTRAL_URL="$2"; shift 2 ;;
    --relay) RELAY=1; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$HOST_ID" && -n "$CENTRAL_URL" ]] || { echo "need --host-id and --central-url" >&2; exit 2; }

TOKEN_FILE="$HOME/.drover/api_token"
[[ -s "$TOKEN_FILE" ]] || { echo "put the fleet API token in $TOKEN_FILE first" >&2; exit 2; }
TOKEN="$(cat "$TOKEN_FILE")"

# Validate the token before installing anything (spec: fail loudly, never a silent retry loop)
STATUS=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$CENTRAL_URL/harness/hosts")
[[ "$STATUS" == "200" ]] || { echo "token/URL check failed against $CENTRAL_URL (HTTP $STATUS)" >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RELAY_FLAG=""
[[ "$RELAY" == 1 ]] && RELAY_FLAG="--relay"

PLIST="$HOME/Library/LaunchAgents/com.drover.harnessd.plist"
sed -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e "s|__HOST_ID__|$HOST_ID|g" \
    -e "s|__CENTRAL_URL__|$CENTRAL_URL|g" \
    -e "s|__RELAY_FLAG__|$RELAY_FLAG|g" \
    "$REPO_DIR/scripts/launchd/com.drover.harnessd-relay.plist.template" > "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "enrolled $HOST_ID -> $CENTRAL_URL (relay=$RELAY); check the app for the new host"
```

- [ ] **Step 2: Write the plist template**

Model on an existing plist in `scripts/launchd/` (read one first for the ProgramArguments/venv-python invocation shape this repo uses; keep `KeepAlive` true, log paths under `~/Library/Logs/drover/`). ProgramArguments run `drover-harnessd --host-id __HOST_ID__ --central-url __CENTRAL_URL__ __RELAY_FLAG__ --listen 127.0.0.1:7081`.

- [ ] **Step 3: Write `docs/multi-host.md`**

Sections: fleet topology diagram (hub on Mac Mini; direct hosts on LAN/tailnet; relay hosts anywhere); the three host shapes (Mac direct, NAS direct/systemd, laptop relay) each with their enroll invocation; Tailscale Funnel setup (`tailscale funnel --bg <hub_port>`, where the public URL appears, how to turn it off); security note (funnel URL is public ⇒ token is the only gate; rotate via the existing token rotation flow; per-host tokens tracked as a follow-up issue); troubleshooting (relay host offline → check laptop logs, check funnel status, check token).

- [ ] **Step 4: Verify the script parses and the plist template renders**

Run: `bash -n scripts/enroll-host.sh && sed -e 's|__REPO_DIR__|/tmp/x|g' -e 's|__HOST_ID__|t|g' -e 's|__CENTRAL_URL__|http://x|g' -e 's|__RELAY_FLAG__||g' scripts/launchd/com.drover.harnessd-relay.plist.template | plutil -lint -`
Expected: `bash -n` silent; plutil reports OK

- [ ] **Step 5: Commit**

```bash
git add scripts/enroll-host.sh scripts/launchd/com.drover.harnessd-relay.plist.template docs/multi-host.md
git commit -m "feat(ops): host enroll script, relay launchd template, multi-host docs"
```

---

### Task 13: Deploy, funnel, live verify (closes #12, #6) + file trailing issues

Ops task — performed on real hardware, checkboxes instead of TDD steps. Requires the user present (physical devices, work laptop, phone on cellular).

- [ ] **Step 1: Deploy hub + Mac harnessd on the Mac Mini** (existing deploy flow: pull main, restart launchd services). This also completes the Mac half of #12's pending deploy.
- [ ] **Step 2: Enable Tailscale Funnel on the Mac Mini**: `tailscale funnel --bg <hub_port>`; record the public URL; `curl -s -o /dev/null -w '%{http_code}' <funnel_url>/harness/hosts` without a token must be 401/403 (token gate holds on the public path).
- [ ] **Step 3: Enroll the work laptop**: clone repo, create venv, put token in `~/.drover/api_token`, run `./scripts/enroll-host.sh --host-id work-laptop --central-url <funnel_url> --relay`. Verify the host shows `online` + `relay` in `GET /harness/hosts`.
- [ ] **Step 4: Live acceptance (spec's single test that exercises everything)**: iPhone on **cellular** (WiFi off) → create a session on `work-laptop` from the app → drive it in terminal + chat → terminate. Also run one claude-code auth flow against the laptop to verify #12's auth work end-to-end on a fresh host.
- [ ] **Step 5: Presence check**: close the work laptop's lid (or `launchctl unload` the agent); host flips offline in the app within ~30s; reopen: flips online; a fresh session works.
- [ ] **Step 6: Close #12 and #6 with verification notes; file the trailing issues**:

```bash
gh issue close 12 --comment "Auth flows deployed on both hosts + verified live on work-laptop over relay (cellular phone test, 2026-XX-XX)."
gh issue close 6 --comment "Physical iPhone verified on cellular via funnel+relay path with rotated token."
gh issue create --title "Cycle 2: public release — full nexus->drover rename incl. storage contracts" --body "Per docs/superpowers/specs/2026-07-28-multihost-relay-ux-design.md: rename nexus.* span keys and nexus_handoff to drover.* with one-time lakehouse migration; DroverKit rename; docs debranding; Traycer positioning; sanitization sweep; flip public. Supersedes the keep-storage-contracts stance in #4 (update #4 checklist accordingly)."
gh issue create --title "Cycle 3: OKF v0.1 assessment doc for the context layer" --body "Map OKF v0.1 (Google Cloud, June 2026) against handoffs, briefs/memory, long-term memory, episodic, skills, evals, context-performance metrics, traces. Verdict per artifact: adopt/adapt/skip. Assessment only; adoption work gated on verdicts."
gh issue create --title "Migrate NAS harnessd to relay mode" --body "Flip the NAS to --relay against the hub, retiring the SSH-tunnel dependency class (mitigation path for #11)."
gh issue create --title "Per-host tokens for the funnel-exposed hub" --body "Single shared bearer token is the only gate on the public funnel URL. Issue per-host tokens with revocation so one leaked host credential doesn't expose the fleet."
```

- [ ] **Step 7: Update memory + push**

---

## Plan 2 pointer

M3–M5 (fleet-first sessions screen, resilience layers, terminal/chat polish, onboarding diagnostics) get their own plan after Task 13 proves the relay fleet live — written against the then-current app code and the real relay presence semantics from Task 9.
