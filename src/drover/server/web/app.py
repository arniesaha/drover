"""drover-server HTTP entry point: routing, auth gate, and the WS terminal proxy.

Business logic lives on MetricsCollector in drover.server.metrics; this module
is the thin HTTP shim around it.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import json
import logging
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlparse

from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.relay_protocol import RelayProtocolError, parse_frame
from drover.server.harness.websocket import (
    OPCODE_CLOSE,
    OPCODE_PING,
    OPCODE_PONG,
    OPCODE_TEXT,
    WebSocketClosed,
    accept_key,
    client_handshake,
    client_send_json,
    recv_frame,
    recv_json,
    send_close,
    send_frame,
    send_json,
)
from drover.server.web.auth import (
    DISABLED,
    AuthSettings,
    request_authorized,
    session_cookie_value,
    token_matches,
)
from drover.server.web.ui import load_page

if TYPE_CHECKING:
    from drover.server.harness.models import HarnessHost
    from drover.server.metrics import MetricsCollector
    from drover.server.relay_manager import RelayManager

log = logging.getLogger("drover.metrics")

_PUBLIC_PATHS = {"/healthz", "/readyz", "/auth/login"}

# Total budget for a spoke to send its hello after the 101, across every
# frame it sends -- not per recv. See _accept_relay_websocket.
RELAY_HELLO_TIMEOUT_S = 10.0
# Mirror records buffered for one relay terminal attach before the newest are
# dropped. Generous, because the worker below amortizes a whole backlog into a
# single DuckDB window: filling this means the database has been unavailable
# for a long time, not that a burst arrived.
MIRROR_QUEUE_MAX = 2048
# Cap on one batched write, so a huge backlog is drained in several bounded
# windows rather than one that holds the connect lock for seconds.
MIRROR_BATCH_MAX = 128
_MESSAGE_PAGE_DEFAULT = 200
_MESSAGE_PAGE_MAX = 500
_GZIP_MIN_BYTES = 1024


@dataclass(frozen=True)
class MessagePageQuery:
    after_seq: int | None
    before_seq: int | None
    through_seq: int | None
    limit: int | None


def _parse_optional_nonnegative_int(
    params: dict[str, list[str]], name: str
) -> int | None:
    values = params.get(name)
    if not values:
        return None
    if len(values) != 1:
        raise ValueError(f"{name} must appear once")
    try:
        value = int(values[0])
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return value


def _parse_message_page_query(params: dict[str, list[str]]) -> MessagePageQuery:
    query = MessagePageQuery(
        after_seq=_parse_optional_nonnegative_int(params, "after_seq"),
        before_seq=_parse_optional_nonnegative_int(params, "before_seq"),
        through_seq=_parse_optional_nonnegative_int(params, "through_seq"),
        limit=_parse_optional_nonnegative_int(params, "limit"),
    )
    if query.after_seq is not None and query.before_seq is not None:
        raise ValueError("after_seq and before_seq are mutually exclusive")
    if query.through_seq is not None and query.after_seq is None:
        raise ValueError("through_seq requires after_seq")
    if query.limit == 0 or (
        query.limit is not None and query.limit > _MESSAGE_PAGE_MAX
    ):
        raise ValueError(f"limit must be between 1 and {_MESSAGE_PAGE_MAX}")
    return query


def _harness_event_record(session_id: str, message: object) -> dict[str, Any] | None:
    """Extract the mirrorable event out of a terminal message, or ``None``.

    Pure and cheap on purpose: the relay drain thread runs this inline and
    hands the result to a worker, so nothing that touches the database may
    happen here.
    """
    from drover.server.metrics import _optional_str, _parse_event_timestamp

    if not isinstance(message, dict) or message.get("type") != "event":
        return None
    event = message.get("event")
    if not isinstance(event, dict):
        return None
    event_id = str(event.get("event_id") or "").strip()
    event_type = str(event.get("event_type") or "").strip()
    if not event_id or not event_type:
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    return {
        "event_id": event_id,
        "session_id": session_id,
        "event_type": event_type,
        "payload": payload,
        "normalized_type": _optional_str(event.get("normalized_type")),
        "normalized_source": _optional_str(event.get("normalized_source")),
        "content_preview": _optional_str(event.get("content_preview")),
        "created_at": _parse_event_timestamp(event.get("created_at")),
    }


class _EventMirror:
    """Batching, off-thread writer for one relay terminal attach's events.

    Why this exists: the relay drain thread used to mirror inline, and every
    mirror opened DuckDB connections under a process-wide per-path lock that
    is contended with fleet renders and every host's event ingestion. At PTY
    output rates the drain thread could not keep up, the channel's bounded
    inbound queue overflowed, and it dropped the oldest messages -- silently
    losing *terminal output the user was watching* in order to finish a
    database write nobody was waiting on.

    So the two are decoupled: the drain thread only parses and enqueues, and
    this worker does the writing, batching whatever has piled up into one
    connection window. If anything still has to be dropped it is now mirror
    records rather than the terminal stream, and loudly.
    """

    def __init__(self, registry: HarnessRegistry) -> None:
        self._registry = registry
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=MIRROR_QUEUE_MAX
        )
        self._dropped = 0
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="relay-event-mirror", daemon=True
        )
        self._thread.start()

    def offer(self, record: dict[str, Any]) -> None:
        """Never blocks: the caller is holding up a live terminal stream."""
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1000 == 0:
                log.warning(
                    "harness event mirror dropped %d event(s): writer behind",
                    self._dropped,
                )

    def close(self) -> None:
        """Signal the worker to finish its backlog and stop.

        Not joined: the attach is over and the caller is a request handler.
        The worker is a daemon thread draining a bounded queue, so it ends on
        its own -- but the sentinel `put_nowait` is best-effort: if the queue
        is exactly full (the overload state this class exists for), it is
        dropped under `suppress(queue.Full)`. Relying on the sentinel alone
        would then park the worker forever in a blocking `get()` once it
        drains the backlog, leaking a thread and the registry it closes over
        for the life of the process. `_closed` is the guarantee: `_run`'s
        wait is bounded, and once the queue is empty and `_closed` is set it
        stops on its own even with no sentinel in sight.
        """
        self._closed.set()
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)

    def _run(self) -> None:
        while True:
            batch, stopping = self._next_batch()
            if batch:
                try:
                    self._registry.append_events_if_new(batch)
                except Exception as exc:  # noqa: BLE001 - never kill the worker
                    log.debug(
                        "failed to mirror %d harness event(s): %s", len(batch), exc
                    )
            if stopping:
                return

    def _next_batch(self) -> tuple[list[dict[str, Any]], bool]:
        """Block for one record, then sweep up everything already queued."""
        first = self._next_record()
        if first is None:
            return [], True
        batch = [first]
        stopping = False
        while len(batch) < MIRROR_BATCH_MAX:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                stopping = True
                break
            batch.append(item)
        return batch, stopping

    def _next_record(self) -> dict[str, Any] | None:
        """Block for the next record, waking periodically to check `_closed`.

        A plain `self._queue.get()` would hang forever if `close()`'s
        sentinel was dropped for arriving on a full queue. Polling with a
        timeout costs nothing on the common path -- every real record wakes
        this immediately -- and bounds the worst case to one poll interval
        past the backlog draining.
        """
        while True:
            try:
                return self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._closed.is_set() and self._queue.empty():
                    return None


class _BrowserSocket:
    """The app-facing websocket of a terminal proxy, with a write lock.

    Two threads write here: the forwarding thread sends terminal messages
    while the reader thread answers pings. Unsynchronized, those interleave
    mid-frame and desync the stream to the app permanently -- the exact
    hazard every other socket in this codebase is meticulous about (see the
    relay_client module docstring). It was the one socket left unguarded, and
    the trigger becomes materially more likely once this path runs through
    Tailscale Funnel: a new intermediary that has not been verified not to
    ping. The iOS client itself never sends one.

    Acquiring the lock is bounded without a timeout parameter because the
    socket carries one (``settimeout(0.25)``), so no holder can sit in
    ``sendall`` indefinitely.
    """

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._write_lock = threading.Lock()

    def send_json(self, payload: dict[str, Any]) -> None:
        with self._write_lock:
            send_json(self.sock, payload)

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        with self._write_lock:
            send_frame(self.sock, opcode, payload)

    def send_close(self) -> None:
        with self._write_lock:
            send_close(self.sock)

    def recv_json(self) -> dict[str, Any] | None:
        """``recv_json``, except the pong goes through the write lock.

        Reads with ``recv_frame`` rather than delegating, because the module
        level ``recv_json`` answers a ping by writing unlocked -- which is the
        whole bug.
        """
        frame = recv_frame(self.sock)
        if frame.opcode == OPCODE_CLOSE:
            raise WebSocketClosed()
        if frame.opcode == OPCODE_PING:
            self.send_frame(OPCODE_PONG, frame.payload)
            return None
        if frame.opcode != OPCODE_TEXT:
            return None
        loaded = json.loads(frame.payload.decode("utf-8"))
        return loaded if isinstance(loaded, dict) else None


def _derive_awaiting(
    *, event_type: str, payload: dict[str, Any], current: str | None
) -> str | None:
    """Replay StructuredSessionManager.emit()'s awaiting-derivation rules.

    Kept in exact lockstep with drover.server.harness.structured.manager
    .StructuredSessionManager.emit -- any other event type leaves the
    current value unchanged.
    """
    if event_type == "approval_prompt":
        return "approval"
    if event_type in {"approval_response", "user_input"}:
        return None
    if event_type == "status" and payload.get("awaiting") == "input":
        return "input"
    return current


def _parse_host_auth_route(path: str) -> dict[str, str] | None:
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if (
        len(parts) == 6
        and parts[:2] == ["harness", "hosts"]
        and parts[3] == "auth"
        and parts[5] in {"status", "start"}
    ):
        return {
            "host_id": parts[2],
            "harness": parts[4],
            "action": parts[5],
            "method": "GET" if parts[5] == "status" else "POST",
        }
    if (
        len(parts) == 7
        and parts[:2] == ["harness", "hosts"]
        and parts[3] == "auth"
        and parts[5] == "flows"
    ):
        return {
            "host_id": parts[2],
            "harness": parts[4],
            "flow_id": parts[6],
            "action": "flow",
            "method": "GET",
        }
    if (
        len(parts) == 8
        and parts[:2] == ["harness", "hosts"]
        and parts[3] == "auth"
        and parts[5] == "flows"
        and parts[7] == "cancel"
    ):
        return {
            "host_id": parts[2],
            "harness": parts[4],
            "flow_id": parts[6],
            "action": "cancel",
            "method": "POST",
        }
    return None


class _MetricsHandler(BaseHTTPRequestHandler):
    collector: "MetricsCollector"
    auth: AuthSettings = DISABLED

    def _gate(self, path: str) -> bool:
        """Authorize the request or write the refusal response.

        Returns True when handling may proceed.
        """
        if path in _PUBLIC_PATHS or not self.auth.enabled:
            return True
        if request_authorized(self.auth, self.headers):
            return True
        if path in {"/", "/ui"} or path.startswith("/ui/"):
            self.send_response(302)
            self.send_header("Location", "/auth/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(
                401, "application/json", '{"error": "authentication required"}\n'
            )
        return False

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._gate(path):
            return
        if path in {"/healthz", "/readyz"}:
            self._send(200, "text/plain; charset=utf-8", "ok\n")
            return
        if path == "/auth/login":
            self._send(200, "text/html; charset=utf-8", load_page("login.html"))
            return
        if path in {"/", "/ui"}:
            self._send(200, "text/html; charset=utf-8", load_page("observatory.html"))
            return
        if path == "/ui/harness":
            self._send(200, "text/html; charset=utf-8", load_page("harness.html"))
            return
        if path.startswith("/ui/harness/sessions/"):
            session_id = unquote(path.removeprefix("/ui/harness/sessions/").strip("/"))
            if not session_id:
                self._send(
                    404,
                    "text/html; charset=utf-8",
                    "<!doctype html><title>Harness session not found</title>"
                    "<p>Missing harness session id.</p>"
                    '<p><a href="/ui/harness">Back to sessions</a></p>',
                )
                return
            self._send(
                200, "text/html; charset=utf-8", load_page("harness_terminal.html")
            )
            return
        if path == "/observability":
            self._send(200, "application/json", self.collector.render_json())
            return
        if path == "/harness":
            self._send(200, "application/json", self.collector.render_harness_json())
            return
        if path == "/harness/hosts":
            self._send(
                200,
                "application/json",
                self.collector.render_harness_json(include_sessions=False),
            )
            return
        auth_route = _parse_host_auth_route(path)
        if auth_route and auth_route["method"] == "GET":
            status, body = self.collector.proxy_harness_auth(
                auth_route["host_id"],
                auth_route["harness"],
                auth_route["action"],
                flow_id=auth_route.get("flow_id"),
            )
            self._send(status, "application/json", body)
            return
        if path.startswith("/harness/hosts/") and path.endswith("/native-sessions"):
            host_id = unquote(
                path.removeprefix("/harness/hosts/")
                .removesuffix("/native-sessions")
                .strip("/")
            )
            status, body = self.collector.proxy_harness_native_sessions(
                host_id,
                {
                    key: values[-1]
                    for key, values in parse_qs(parsed.query).items()
                    if values
                },
            )
            self._send(status, "application/json", body)
            return
        if path == "/harness/sessions":
            self._send(
                200,
                "application/json",
                self.collector.render_harness_json(include_hosts=False),
            )
            return
        if path == "/harness/relay":
            self._accept_relay_websocket()
            return
        if path.startswith("/harness/sessions/") and path.endswith("/terminal"):
            session_id = unquote(
                path.removeprefix("/harness/sessions/")
                .removesuffix("/terminal")
                .strip("/")
            )
            self._proxy_terminal_websocket(session_id)
            return
        if path.startswith("/harness/sessions/") and path.endswith(
            "/native-transcript"
        ):
            session_id = unquote(
                path.removeprefix("/harness/sessions/")
                .removesuffix("/native-transcript")
                .strip("/")
            )
            status, body = self.collector.proxy_harness_native_transcript(session_id)
            self._send(status, "application/json", body)
            return
        if path.startswith("/harness/sessions/") and path.endswith("/stream"):
            session_id = unquote(
                path.removeprefix("/harness/sessions/").removesuffix("/stream")
            ).strip("/")
            params = parse_qs(parsed.query)
            raw_after = (params.get("after_seq") or ["0"])[0]
            try:
                after_seq = int(raw_after)
            except ValueError:
                self._send(
                    400,
                    "application/json",
                    '{"error": "after_seq must be an integer"}\n',
                )
                return
            if (self.headers.get("Upgrade") or "").lower() != "websocket":
                self._send(
                    400,
                    "application/json",
                    '{"error": "websocket upgrade required"}\n',
                )
                return
            self._stream_session_messages(session_id, after_seq=after_seq)
            return
        if path.startswith("/harness/sessions/") and path.endswith("/messages"):
            session_id = unquote(
                path.removeprefix("/harness/sessions/").removesuffix("/messages")
            ).strip("/")
            try:
                query = _parse_message_page_query(parse_qs(parsed.query))
            except ValueError as exc:
                self._send(
                    400,
                    "application/json",
                    json.dumps({"error": str(exc)}) + "\n",
                )
                return
            registry = self._harness_registry()
            compatibility_mode = (
                query.limit is None
                and query.before_seq is None
                and query.through_seq is None
            )
            if compatibility_mode:
                events = registry.list_events_after(
                    session_id, query.after_seq if query.after_seq is not None else 0
                )
                payload = {
                    "messages": [event.wire_payload() for event in events],
                    "max_seq": registry.max_event_seq(session_id),
                }
            else:
                page = registry.list_event_page(
                    session_id,
                    after_seq=query.after_seq,
                    before_seq=query.before_seq,
                    through_seq=query.through_seq,
                    limit=query.limit or _MESSAGE_PAGE_DEFAULT,
                )
                payload = {
                    "messages": [event.wire_payload() for event in page.events],
                    "page_min_seq": page.page_min_seq,
                    "page_max_seq": page.page_max_seq,
                    "max_seq": page.max_seq,
                    "has_older": page.has_older,
                    "has_newer": page.has_newer,
                }
            self._send(
                200,
                "application/json",
                json.dumps(payload) + "\n",
                allow_gzip=True,
                route_class="session_messages",
            )
            return
        if path.startswith("/harness/sessions/"):
            session_id = unquote(path.removeprefix("/harness/sessions/").strip("/"))
            status, body = self.collector.render_harness_session_json(session_id)
            self._send(status, "application/json", body)
            return
        if path == "/metrics":
            self._send(
                200,
                "text/plain; version=0.0.4; charset=utf-8",
                self.collector.render_prometheus(),
            )
            return
        self._send(404, "text/plain; charset=utf-8", "not found\n")

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._gate(path):
            return
        if path == "/auth/login":
            self._handle_login()
            return
        if path == "/harness/events":
            self._ingest_harness_events()
            return
        if path == "/harness/hosts":
            body = self._read_json()
            if body is None:
                self._send(
                    400,
                    "application/json",
                    '{"error": "request body must be valid JSON"}\n',
                )
                return
            status, payload = self.collector.register_harness_host(body)
            self._send(status, "application/json", payload)
            return
        if path.startswith("/harness/hosts/") and path.endswith("/heartbeat"):
            host_id = unquote(
                path.removeprefix("/harness/hosts/")
                .removesuffix("/heartbeat")
                .strip("/")
            )
            if not host_id:
                self._send(400, "application/json", '{"error": "missing host_id"}\n')
                return
            body = self._read_json()
            if body is None:
                self._send(
                    400,
                    "application/json",
                    '{"error": "request body must be valid JSON"}\n',
                )
                return
            body["host_id"] = host_id
            status, payload = self.collector.register_harness_host(body)
            self._send(status, "application/json", payload)
            return
        auth_route = _parse_host_auth_route(path)
        if auth_route and auth_route["method"] == "POST":
            status, body = self.collector.proxy_harness_auth(
                auth_route["host_id"],
                auth_route["harness"],
                auth_route["action"],
                flow_id=auth_route.get("flow_id"),
            )
            self._send(status, "application/json", body)
            return
        if path.startswith("/harness/hosts/") and path.endswith("/sessions"):
            host_id = unquote(
                path.removeprefix("/harness/hosts/")
                .removesuffix("/sessions")
                .strip("/")
            )
            if not host_id:
                self._send(400, "application/json", '{"error": "missing host_id"}\n')
                return
            body = self._read_json()
            if body is None:
                self._send(
                    400,
                    "application/json",
                    '{"error": "request body must be valid JSON"}\n',
                )
                return
            status, payload = self.collector.proxy_create_harness_session(host_id, body)
            self._send(status, "application/json", payload)
            return
        for action in ("turns", "permission", "interrupt"):
            suffix = f"/{action}"
            if path.startswith("/harness/sessions/") and path.endswith(suffix):
                session_id = unquote(
                    path.removeprefix("/harness/sessions/")
                    .removesuffix(suffix)
                    .strip("/")
                )
                if not session_id:
                    self._send(
                        400, "application/json", '{"error": "missing session_id"}\n'
                    )
                    return
                body = self._read_json() if action != "interrupt" else {}
                if body is None:
                    self._send(
                        400,
                        "application/json",
                        '{"error": "request body must be valid JSON"}\n',
                    )
                    return
                status, payload = self.collector.proxy_harness_session_action(
                    session_id, action, body
                )
                self._send(status, "application/json", payload)
                return
        if path.startswith("/harness/sessions/") and path.endswith("/terminate"):
            session_id = unquote(
                path.removeprefix("/harness/sessions/")
                .removesuffix("/terminate")
                .strip("/")
            )
            if not session_id:
                self._send(400, "application/json", '{"error": "missing session_id"}\n')
                return
            status, payload = self.collector.proxy_terminate_harness_session(session_id)
            self._send(status, "application/json", payload)
            return
        if path.startswith("/harness/sessions/") and path.endswith("/continue"):
            session_id = unquote(
                path.removeprefix("/harness/sessions/")
                .removesuffix("/continue")
                .strip("/")
            )
            if not session_id:
                self._send(400, "application/json", '{"error": "missing session_id"}\n')
                return
            body = self._read_json()
            if body is None:
                self._send(
                    400,
                    "application/json",
                    '{"error": "request body must be valid JSON"}\n',
                )
                return
            status, payload = self.collector.continue_harness_session(session_id, body)
            self._send(status, "application/json", payload)
            return
        self._send(404, "text/plain; charset=utf-8", "not found\n")

    def log_message(self, fmt: str, *args: object) -> None:
        log.debug("metrics http: " + fmt, *args)

    def _proxy_terminal_websocket(self, session_id: str) -> None:
        if not session_id:
            self._send(404, "application/json", '{"error": "missing session_id"}\n')
            return
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self._send(
                426,
                "application/json",
                '{"error": "terminal attach requires websocket upgrade"}\n',
            )
            return
        if not self.headers.get("Sec-WebSocket-Key"):
            self._send(
                400,
                "application/json",
                '{"error": "missing Sec-WebSocket-Key"}\n',
            )
            return

        route = self.collector.harness_terminal_route(session_id)
        if route is None:
            self._send(
                404,
                "application/json",
                '{"error": "unknown terminal session or host endpoint"}\n',
            )
            return
        host, path = route

        relay_manager = self.collector.relay_manager
        if relay_manager is not None and relay_manager.is_live(host.host_id):
            self._proxy_terminal_over_relay(
                session_id, host.host_id, path, relay_manager
            )
            return
        if getattr(host, "connection_kind", "direct") == "relay":
            # Same rule as _harness_request: never dial a relay host by URL.
            # Its socket is the only way in, and the default listen address
            # everywhere in this repo is 127.0.0.1:7081 -- so a stray URL on a
            # relay row would attach the user's terminal to the HUB's own
            # harnessd. Unreachable is the safe failure.
            self._send(
                502,
                "application/json",
                json.dumps({"error": f"relay host is not connected: {host.host_id}"})
                + "\n",
            )
            return
        self._proxy_terminal_direct(session_id, host, path)

    def _proxy_terminal_direct(
        self, session_id: str, host: "HarnessHost", path: str
    ) -> None:
        from drover.server.metrics import _harness_endpoint

        endpoint = _harness_endpoint(host)
        if not endpoint:
            self._send(
                404,
                "application/json",
                '{"error": "unknown terminal session or host endpoint"}\n',
            )
            return

        parsed = urlparse(f"{endpoint}{path}")
        upstream_host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        upstream_path = parsed.path or "/"
        upstream: socket.socket | None = None
        try:
            upstream = socket.create_connection((upstream_host, port), timeout=10)
            upstream_headers = (
                {"Authorization": f"Bearer {self.collector.api_token}"}
                if self.collector.api_token
                else None
            )
            client_handshake(
                upstream,
                host=f"{upstream_host}:{port}",
                path=upstream_path,
                headers=upstream_headers,
            )
        except Exception as exc:  # noqa: BLE001
            if upstream is not None:
                upstream.close()
            self._send(
                502,
                "application/json",
                json.dumps({"error": f"harness websocket upstream failed: {exc}"})
                + "\n",
            )
            return

        raw_browser = self._upgrade_browser_websocket()
        if raw_browser is None:
            upstream.close()
            return

        stop = threading.Event()
        raw_browser.settimeout(0.25)
        upstream.settimeout(0.25)
        browser = _BrowserSocket(raw_browser)

        def browser_to_upstream() -> None:
            try:
                while not stop.is_set():
                    try:
                        message = browser.recv_json()
                    except socket.timeout:
                        continue
                    if message is not None:
                        client_send_json(upstream, message)
            except Exception:
                stop.set()

        def upstream_to_browser() -> None:
            try:
                while not stop.is_set():
                    try:
                        frame = recv_frame(upstream)
                    except socket.timeout:
                        continue
                    self._mirror_harness_event_frame(session_id, frame)
                    browser.send_frame(frame.opcode, frame.payload)
                    if frame.opcode == OPCODE_CLOSE:
                        stop.set()
                        return
            except Exception:
                stop.set()

        thread = threading.Thread(target=browser_to_upstream, daemon=True)
        thread.start()
        upstream_to_browser()
        stop.set()
        upstream.close()

    def _proxy_terminal_over_relay(
        self,
        session_id: str,
        host_id: str,
        path: str,
        relay_manager: "RelayManager",
    ) -> None:
        from drover.server.relay_manager import RelayUnavailable

        try:
            channel = relay_manager.open_channel(host_id, path)
        except RelayUnavailable as exc:
            self._send(
                502,
                "application/json",
                json.dumps({"error": f"harness websocket upstream failed: {exc}"})
                + "\n",
            )
            return

        mirror: _EventMirror | None = None
        try:
            raw_browser = self._upgrade_browser_websocket()
            if raw_browser is None:
                return

            stop = threading.Event()
            raw_browser.settimeout(0.25)
            browser = _BrowserSocket(raw_browser)
            # Off-thread and batched: mirroring inline here is what let a slow
            # DuckDB write turn into dropped terminal output (see _EventMirror).
            mirror = _EventMirror(self._harness_registry())

            def browser_to_channel() -> None:
                try:
                    while not stop.is_set():
                        try:
                            message = browser.recv_json()
                        except socket.timeout:
                            continue
                        if message is not None:
                            channel.send(message)
                except Exception:
                    stop.set()

            def channel_to_browser() -> None:
                try:
                    while not stop.is_set():
                        message = channel.recv(timeout_s=0.25)
                        if message is None:
                            if channel.closed:
                                stop.set()
                                try:
                                    browser.send_close()
                                except Exception:  # noqa: BLE001
                                    pass
                                return
                            continue
                        record = _harness_event_record(session_id, message)
                        if record is not None:
                            mirror.offer(record)
                        browser.send_json(message)
                except Exception:
                    stop.set()

            thread = threading.Thread(target=browser_to_channel, daemon=True)
            thread.start()
            channel_to_browser()
            stop.set()
        finally:
            channel.close()
            if mirror is not None:
                mirror.close()

    def _upgrade_browser_websocket(self) -> socket.socket | None:
        """Send the browser-side 101 upgrade and hijack the connection.

        Shared by the direct and relay terminal-proxy flavors. Must only be
        called once the upstream (direct socket or relay channel) is already
        established -- a browser must never see a successful upgrade for a
        session the hub could not actually reach; the caller sends a normal
        HTTP error response instead in that case.
        """
        websocket_key = self.headers.get("Sec-WebSocket-Key")
        if not websocket_key:
            self._send(
                400,
                "application/json",
                '{"error": "missing Sec-WebSocket-Key"}\n',
            )
            return None
        # BaseHTTPRequestHandler defaults to HTTP/1.0; strict WebSocket
        # clients (URLSessionWebSocketTask) reject an "HTTP/1.0 101" status
        # line. Scoped to the upgrade response only — the socket is hijacked
        # after this, so HTTP/1.1 keep-alive semantics never apply.
        self.protocol_version = "HTTP/1.1"
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(websocket_key))
        self.end_headers()
        self.close_connection = True
        return self.connection

    def _accept_relay_websocket(self) -> None:
        """Upgrade a spoke's connection and hand it to RelayManager.

        The Bearer check already ran in ``_gate`` (called at the top of
        ``do_GET``) -- this route is never reachable without it, same as
        every other non-public path.
        """
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self._send(
                426,
                "application/json",
                '{"error": "relay attach requires websocket upgrade"}\n',
            )
            return
        websocket_key = self.headers.get("Sec-WebSocket-Key")
        if not websocket_key:
            self._send(
                400,
                "application/json",
                '{"error": "missing Sec-WebSocket-Key"}\n',
            )
            return

        # See _proxy_terminal_websocket: HTTP/1.1 status line required by
        # strict WebSocket clients; scoped to this hijacked upgrade only.
        self.protocol_version = "HTTP/1.1"
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(websocket_key))
        self.end_headers()
        self.close_connection = True

        sock = self.connection
        # One deadline for the whole handshake, not per recv. settimeout() is
        # per socket operation, and recv_json returns None for a ping, a pong,
        # or any non-text opcode -- so a peer sending a ping every 9 seconds
        # used to loop here forever, pinning a ThreadingHTTPServer thread with
        # no cap on how many. Reachable from the internet by anyone holding
        # the shared token once the funnel is up, and unbounded thread growth
        # takes the whole hub down, not just the relay.
        deadline = time.monotonic() + RELAY_HELLO_TIMEOUT_S
        try:
            frame = None
            while frame is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RelayProtocolError(
                        f"no hello frame within {RELAY_HELLO_TIMEOUT_S}s"
                    )
                sock.settimeout(max(0.1, remaining))
                frame = recv_json(sock)
            parsed = parse_frame(frame)
            if parsed.get("kind") != "hello":
                raise RelayProtocolError(
                    f"expected hello frame, got kind={parsed.get('kind')!r}"
                )
            host_id = str(parsed.get("host_id") or "").strip()
            if not host_id:
                raise RelayProtocolError("hello frame missing host_id")
        except (OSError, WebSocketClosed, RelayProtocolError, ValueError) as exc:
            log.info("relay handshake for %s failed: %s", self.client_address, exc)
            sock.close()
            return

        # RelayManager owns a blocking socket -- a leftover read timeout
        # would tear a healthy idle connection down (socket.timeout is an
        # OSError, indistinguishable from a real failure to the reader).
        sock.settimeout(None)

        # The socket must outlive this handler: BaseRequestHandler.finish()
        # (called when this method returns) closes rfile/wfile, and
        # ThreadingHTTPServer's shutdown_request() then calls shutdown()
        # and close() on the *same* socket object RelayManager is about to
        # own. detach() extracts the live fd without closing it and marks
        # this socket object closed, so those later calls become no-ops
        # (shutdown raises EBADF, caught by socketserver; close is a
        # no-op) while the fd itself, rewrapped below, keeps working.
        fd = sock.detach()
        relay_sock = socket.socket(fileno=fd)
        self.collector.relay_manager.attach(host_id, relay_sock)

    def _harness_registry(self) -> HarnessRegistry:
        return HarnessRegistry(self.collector.duckdb_path)

    def _stream_session_messages(self, session_id: str, *, after_seq: int = 0) -> None:
        """WS handler: push new structured events for a session as they land.

        Handshakes at 101, then polls the registry roughly once a second,
        emitting each new event (seq order) as its own JSON text frame.
        Breaks cleanly on a client close frame, a socket timeout is just an
        empty poll (not an error), and any other socket/protocol failure
        (abrupt disconnect, reset, etc.) ends the loop without raising.
        """
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send(
                400,
                "application/json",
                '{"error": "missing Sec-WebSocket-Key"}\n',
            )
            return
        # See _proxy_terminal_websocket: HTTP/1.1 status line required by
        # strict WebSocket clients; scoped to this hijacked upgrade only.
        self.protocol_version = "HTTP/1.1"
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(key))
        self.end_headers()
        self.close_connection = True
        sock = self.connection
        sock.settimeout(0.2)
        registry = self._harness_registry()
        last_seq = after_seq
        try:
            while True:
                for event in registry.list_events_after(session_id, last_seq):
                    if event.seq is not None:
                        last_seq = event.seq
                    send_frame(
                        sock,
                        OPCODE_TEXT,
                        json.dumps(event.wire_payload()).encode("utf-8"),
                    )
                try:
                    frame = recv_frame(sock)
                except socket.timeout:
                    time.sleep(0.8)
                    continue
                if frame.opcode == OPCODE_CLOSE:
                    break
                if frame.opcode == OPCODE_PING:
                    send_frame(sock, OPCODE_PONG, frame.payload)
                    continue
                if frame.opcode == OPCODE_PONG:
                    continue
        except (OSError, WebSocketClosed):
            pass

    def _mirror_harness_event_frame(self, session_id: str, frame) -> None:
        if frame.opcode != OPCODE_TEXT:
            return
        try:
            message = json.loads(frame.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        self._mirror_harness_event_message(session_id, message)

    def _mirror_harness_event_message(self, session_id: str, message: object) -> None:
        """Persist a parsed harness terminal event message into the hub log.

        Used by the direct terminal-proxy flavor, which decodes a raw
        websocket frame first (`_mirror_harness_event_frame`, above) and
        delegates here. This is the sole delivery path for PTY terminal
        events into the hub's own DuckDB event log -- the daemon's local
        registry write never reaches the hub on its own.

        Deliberately synchronous here, unlike the relay flavor: there is no
        bounded queue between the upstream socket and the browser on this
        path, so a slow write applies TCP backpressure and the session merely
        runs slow. Nothing is discarded. Moving it off-thread would trade that
        for a drop path this flavor does not currently have.
        """
        record = _harness_event_record(session_id, message)
        if record is None:
            return
        try:
            self._harness_registry().append_events_if_new([record])
        except Exception as exc:  # noqa: BLE001
            log.debug("failed to mirror harness event %s: %s", record["event_id"], exc)

    def _ingest_harness_events(self) -> None:
        """POST /harness/events: bulk ingest from a remote host's EventPusher.

        Idempotent by event_id (skips events already recorded, e.g. one that
        the terminal-websocket mirror already wrote). Malformed bodies get a
        400; a well-formed body always responds 200 with the count of newly
        recorded events, even if some/all were already present.

        On split-DB deployments (central and harnessd each with their own
        DuckDB file) this is the ONLY place central's copy of the session row
        learns about `awaiting`/`last_activity` -- the daemon's own
        StructuredSessionManager.emit() only ever updates its local registry.
        So after recording each new event, replay the same awaiting-
        derivation rules emit() uses, and persist the final result once per
        session touched in this batch (not once per event, to avoid N
        redundant writes for a large batch).
        """
        from drover.server.metrics import _parse_event_timestamp

        body = self._read_json()
        if body is None:
            self._send(
                400,
                "application/json",
                '{"error": "request body must be valid JSON"}\n',
            )
            return
        events = body.get("events")
        if not isinstance(events, list) or not all(
            isinstance(event, dict)
            and event.get("event_id")
            and event.get("session_id")
            and event.get("type")
            for event in events
        ):
            self._send(
                400,
                "application/json",
                '{"error": "events must be a list of objects with '
                'event_id, session_id, and type"}\n',
            )
            return
        registry = self._harness_registry()
        ingested = 0
        # Sort defensively by seq: the derivation rules are order-sensitive
        # (e.g. approval_prompt then approval_response must land as "not
        # awaiting", not the reverse), and a batch is not guaranteed to
        # arrive in seq order.
        ordered = sorted(
            events,
            key=lambda event: (
                event.get("seq") if isinstance(event.get("seq"), int) else 0
            ),
        )
        session_awaiting: dict[str, str | None] = {}
        session_last_activity: dict[str, datetime] = {}
        touched_sessions: set[str] = set()
        for event in ordered:
            event_id = str(event["event_id"])
            session_id = str(event["session_id"])
            if registry.get_event(event_id) is not None:
                continue
            created_at = _parse_event_timestamp(event.get("ts")) or datetime.now(
                timezone.utc
            )
            registry.append_event(
                session_id=session_id,
                event_type=str(event["type"]),
                payload=event,
                seq=event.get("seq"),
                normalized_source="structured",
                event_id=event_id,
                created_at=created_at,
            )
            ingested += 1
            touched_sessions.add(session_id)
            if session_id not in session_awaiting:
                existing = registry.get_session(session_id)
                session_awaiting[session_id] = existing.awaiting if existing else None
            inner_payload = event.get("payload")
            session_awaiting[session_id] = _derive_awaiting(
                event_type=str(event["type"]),
                payload=inner_payload if isinstance(inner_payload, dict) else {},
                current=session_awaiting[session_id],
            )
            latest = session_last_activity.get(session_id)
            if latest is None or created_at > latest:
                session_last_activity[session_id] = created_at
        for session_id in touched_sessions:
            registry.update_session_activity(
                session_id,
                awaiting=session_awaiting.get(session_id),
                last_activity=session_last_activity.get(session_id),
            )
        self._send(200, "application/json", json.dumps({"ingested": ingested}) + "\n")

    def _handle_login(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = (
            self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        )
        fields = parse_qs(raw)
        candidate = (fields.get("token") or [""])[0].strip()
        if self.auth.enabled and not token_matches(self.auth, candidate):
            self.send_response(302)
            self.send_header("Location", "/auth/login?error=1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(302)
        self.send_header("Location", "/ui")
        if self.auth.enabled:
            self.send_header("Set-Cookie", session_cookie_value(self.auth))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _send(
        self,
        status: int,
        content_type: str,
        body: str,
        *,
        allow_gzip: bool = False,
        route_class: str | None = None,
    ) -> None:
        started = time.monotonic()
        payload = body.encode("utf-8")
        uncompressed_bytes = len(payload)
        headers = getattr(self, "headers", {})
        accepts_gzip = "gzip" in {
            encoding.split(";", 1)[0].strip().lower()
            for encoding in (headers.get("Accept-Encoding") or "").split(",")
        }
        compressed = allow_gzip and accepts_gzip and len(payload) >= _GZIP_MIN_BYTES
        if compressed:
            payload = gzip.compress(payload)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if allow_gzip:
            self.send_header("Vary", "Accept-Encoding")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(payload)))
        if self.path.startswith(("/ui/harness", "/harness")):
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            log.info(
                "client disconnected while sending %s bytes for %s",
                len(payload),
                route_class or self.path,
            )
        if route_class:
            log.info(
                "http_response route_class=%s status=%d uncompressed_bytes=%d "
                "transferred_bytes=%d elapsed_ms=%.3f",
                route_class,
                status,
                uncompressed_bytes,
                len(payload),
                (time.monotonic() - started) * 1000,
            )


def start_metrics_server(
    *,
    host: str,
    port: int,
    collector: "MetricsCollector",
    auth: AuthSettings | None = None,
) -> ThreadingHTTPServer:
    """Start the Drover metrics HTTP server in a daemon thread."""
    from drover.server.relay_manager import RelayManager

    collector.relay_manager = RelayManager()
    handler = type(
        "DroverMetricsHandler",
        (_MetricsHandler,),
        {"collector": collector, "auth": auth or DISABLED},
    )
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(
        target=server.serve_forever, name="drover-metrics", daemon=True
    )
    thread.start()
    return server
