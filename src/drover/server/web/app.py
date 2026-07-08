"""drover-server HTTP entry point: routing, auth gate, and the WS terminal proxy.

Business logic lives on MetricsCollector in drover.server.metrics; this module
is the thin HTTP shim around it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlparse

from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.websocket import (
    OPCODE_CLOSE,
    OPCODE_TEXT,
    WebSocketClosed,
    accept_key,
    client_handshake,
    client_send_json,
    recv_frame,
    recv_json,
    send_frame,
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
    from drover.server.metrics import MetricsCollector

log = logging.getLogger("drover.metrics")

_PUBLIC_PATHS = {"/healthz", "/readyz", "/auth/login"}


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
            if (self.headers.get("Upgrade") or "").lower() != "websocket":
                self._send(
                    400,
                    "application/json",
                    '{"error": "websocket upgrade required"}\n',
                )
                return
            self._stream_session_messages(session_id)
            return
        if path.startswith("/harness/sessions/") and path.endswith("/messages"):
            session_id = unquote(
                path.removeprefix("/harness/sessions/").removesuffix("/messages")
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
            registry = self._harness_registry()
            events = registry.list_events_after(session_id, after_seq)
            body = json.dumps(
                {
                    "messages": [event.payload for event in events],
                    "max_seq": registry.max_event_seq(session_id),
                }
            )
            self._send(200, "application/json", body + "\n")
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
        websocket_key = self.headers.get("Sec-WebSocket-Key")
        if not websocket_key:
            self._send(
                400,
                "application/json",
                '{"error": "missing Sec-WebSocket-Key"}\n',
            )
            return
        upstream_url = self.collector.harness_terminal_endpoint(session_id)
        if not upstream_url:
            self._send(
                404,
                "application/json",
                '{"error": "unknown terminal session or host endpoint"}\n',
            )
            return

        parsed = urlparse(upstream_url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        upstream: socket.socket | None = None
        try:
            upstream = socket.create_connection((host, port), timeout=10)
            upstream_headers = (
                {"Authorization": f"Bearer {self.collector.api_token}"}
                if self.collector.api_token
                else None
            )
            client_handshake(
                upstream, host=f"{host}:{port}", path=path, headers=upstream_headers
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

        browser = self.connection
        stop = threading.Event()
        browser.settimeout(0.25)
        upstream.settimeout(0.25)

        def browser_to_upstream() -> None:
            try:
                while not stop.is_set():
                    try:
                        message = recv_json(browser)
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
                    send_frame(browser, frame.opcode, frame.payload)
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

    def _harness_registry(self) -> HarnessRegistry:
        return HarnessRegistry(self.collector.duckdb_path)

    def _stream_session_messages(self, session_id: str) -> None:
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
        last_seq = 0
        try:
            while True:
                for event in registry.list_events_after(session_id, last_seq):
                    if event.seq is not None:
                        last_seq = event.seq
                    send_frame(
                        sock,
                        OPCODE_TEXT,
                        json.dumps(event.payload).encode("utf-8"),
                    )
                try:
                    frame = recv_frame(sock)
                except socket.timeout:
                    time.sleep(0.8)
                    continue
                if frame.opcode == OPCODE_CLOSE:
                    break
        except (OSError, WebSocketClosed):
            pass

    def _mirror_harness_event_frame(self, session_id: str, frame) -> None:
        from drover.server.metrics import _optional_str, _parse_event_timestamp

        if frame.opcode != OPCODE_TEXT:
            return
        try:
            message = json.loads(frame.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(message, dict) or message.get("type") != "event":
            return
        event = message.get("event")
        if not isinstance(event, dict):
            return
        event_id = str(event.get("event_id") or "").strip()
        event_type = str(event.get("event_type") or "").strip()
        if not event_id or not event_type:
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        created_at = _parse_event_timestamp(event.get("created_at"))
        try:
            registry = self._harness_registry()
            if registry.get_event(event_id) is not None:
                return
            registry.append_event(
                session_id=session_id,
                event_type=event_type,
                payload=payload,
                normalized_type=_optional_str(event.get("normalized_type")),
                normalized_source=_optional_str(event.get("normalized_source")),
                content_preview=_optional_str(event.get("content_preview")),
                event_id=event_id,
                created_at=created_at,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("failed to mirror harness event %s: %s", event_id, exc)

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

    def _send(self, status: int, content_type: str, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if self.path.startswith(("/ui/harness", "/harness")):
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(payload)


def start_metrics_server(
    *,
    host: str,
    port: int,
    collector: "MetricsCollector",
    auth: AuthSettings | None = None,
) -> ThreadingHTTPServer:
    """Start the Drover metrics HTTP server in a daemon thread."""
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
