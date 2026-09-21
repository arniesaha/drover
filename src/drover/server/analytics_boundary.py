"""Narrow, bounded loopback transport between API and analytics roles.

This module intentionally has no dependency on the lake, a collector, or the
public HTTP handler.  It is the one place that knows which cross-role routes
exist, how much data they may carry, and which internal credential authenticates
them.  Keeping that surface small prevents a role split from becoming a generic
authenticated proxy.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

from drover.config import AnalyticsBoundaryConfig

ANALYTICS_UNAVAILABLE_BODY = (
    json.dumps({"error": "analytics worker unavailable"}, separators=(",", ":")) + "\n"
)
ANALYTICS_UNAVAILABLE_HEADERS = {"Retry-After": "2"}
API_TO_WORKER_HEADER = "X-Drover-Api-To-Analytics"
WORKER_TO_API_HEADER = "X-Drover-Analytics-To-Api"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_COCKPIT_QUERY_FIELDS = frozenset(
    {
        "days",
        "host_id",
        "harness",
        "provider",
        "model",
        "project_key",
        "limit",
        "project_cursor",
        "harness_cursor",
        "host_cursor",
        "model_cursor",
    }
)
_INSIGHT_QUERY_FIELDS = frozenset(
    {
        "state",
        "severity",
        "confidence",
        "analyzer_class",
        "host",
        "harness",
        "target_type",
        "target_id",
        "cursor",
        "limit",
    }
)


class AnalyticsBoundaryUnavailable(RuntimeError):
    """The fixed analytics boundary could not return a safe response."""


class AnalyticsBoundaryRequestInvalid(ValueError):
    """A public request cannot cross the fixed analytics boundary."""


class HostDataBridgeUnavailable(RuntimeError):
    """The worker could not obtain relay-owned data from the API process."""


@dataclass(frozen=True, slots=True)
class BoundaryResponse:
    status: int
    content_type: str
    body: bytes


BoundaryTransport = Callable[[str, str, dict[str, str], bytes], BoundaryResponse]
BoundaryDispatcher = Callable[[str, str, str, bytes], BoundaryResponse]
ArchivePayloadResolver = Callable[[str, str, str], str | None]
WorkerHealthProvider = Callable[[], Mapping[str, Any]]


def _split_path(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.split("/") if part)


def _is_insight_detail(parts: tuple[str, ...]) -> bool:
    return len(parts) == 2 and parts[0] == "insights" and bool(parts[1])


def _is_insight_check_status(parts: tuple[str, ...]) -> bool:
    return (
        len(parts) == 4
        and parts[0] == "insights"
        and parts[1]
        and parts[2] == "checks"
        and parts[3]
    )


def _is_insight_mutation(parts: tuple[str, ...]) -> bool:
    return (
        len(parts) == 3
        and parts[0] == "insights"
        and bool(parts[1])
        and parts[2] in {"acknowledge", "dismiss", "check"}
    )


def _allowed_query_fields(method: str, path: str) -> frozenset[str]:
    if method != "GET":
        return frozenset()
    if path in {"/cockpit/overview", "/analytics"}:
        return _COCKPIT_QUERY_FIELDS
    if path == "/insights":
        return _INSIGHT_QUERY_FIELDS
    return frozenset()


def validate_analytics_request(method: str, path: str, query: str) -> None:
    """Validate one public analytical route before it reaches another role."""

    if method not in {"GET", "POST", "DELETE"}:
        raise ValueError("unsupported analytics boundary method")
    if not path.startswith("/") or "//" in path or len(path) > 4_096:
        raise ValueError("unsupported analytics boundary route")
    parts = _split_path(path)
    allowed = False
    if method == "GET":
        allowed = (
            path
            in {
                "/metrics",
                "/observability",
                "/cockpit/overview",
                "/analytics",
                "/insights",
                "/insights/content-analysis",
            }
            or _is_insight_detail(parts)
            or _is_insight_check_status(parts)
        )
    elif method == "POST":
        allowed = path in {
            "/insights/content-analysis/consent",
            "/insights/content-analysis/revoke",
        } or _is_insight_mutation(parts)
    elif method == "DELETE":
        allowed = path == "/insights/content-excerpts"
    if not allowed:
        raise ValueError("unsupported analytics boundary route")
    if len(query.encode("utf-8")) > 4_096:
        raise ValueError("analytics query is too large")
    seen: set[str] = set()
    fields = _allowed_query_fields(method, path)
    for key, _value in parse_qsl(query, keep_blank_values=True, strict_parsing=True):
        if key in seen:
            raise ValueError(f"{key} must appear once")
        if key not in fields:
            raise ValueError("unexpected analytics query parameter")
        seen.add(key)


def is_analytics_route(method: str, path: str, query: str) -> bool:
    try:
        validate_analytics_request(method, path, query)
    except ValueError:
        return False
    return True


class AnalyticsBoundaryClient:
    """Bounded API-to-worker client with no caller credential forwarding."""

    def __init__(
        self,
        config: AnalyticsBoundaryConfig,
        *,
        token: str,
        transport: BoundaryTransport | None = None,
    ) -> None:
        self._config = config
        self._token = token.strip()
        self._transport = transport or self._http_transport
        self._slots = threading.BoundedSemaphore(config.max_concurrent_requests)

    @property
    def config(self) -> AnalyticsBoundaryConfig:
        return self._config

    def request(
        self,
        method: str,
        path: str,
        query: str,
        body: bytes,
        *,
        public_headers: Mapping[str, str] | None = None,
    ) -> BoundaryResponse:
        """Forward an allowlisted request, or raise without exposing internals."""

        del public_headers  # Explicitly never carries browser credentials across.
        try:
            validate_analytics_request(method, path, query)
        except ValueError as exc:
            raise AnalyticsBoundaryRequestInvalid("invalid analytics request") from exc
        if not self._token or len(body) > self._config.max_request_bytes:
            raise AnalyticsBoundaryUnavailable("analytics request is unavailable")
        deadline = time.monotonic() + self._config.request_timeout_seconds
        if not self._slots.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise AnalyticsBoundaryUnavailable(
                "analytics request capacity is unavailable"
            )
        try:
            target = self._config.worker_url + path
            if query:
                target += "?" + query
            response = self._transport(
                method,
                target,
                {
                    "Accept": "application/json",
                    API_TO_WORKER_HEADER: self._token,
                },
                body,
            )
            if (
                not 100 <= response.status <= 599
                or not response.content_type
                or len(response.body) > self._config.max_response_bytes
            ):
                raise AnalyticsBoundaryUnavailable("invalid analytics response")
            return response
        except AnalyticsBoundaryUnavailable:
            raise
        except (
            Exception
        ) as exc:  # noqa: BLE001 - boundary response is intentionally opaque
            raise AnalyticsBoundaryUnavailable("analytics worker unavailable") from exc
        finally:
            self._slots.release()

    def resolve(
        self, *, event_id: str, batch_id: str, payload_sha256: str
    ) -> str | None:
        """Resolve one verified archive reference through the worker only.

        This satisfies ``ArchiveResolver`` without permitting the API process
        to inspect a parquet path or make a caller-selected boundary request.
        A worker failure becomes the registry's explicit unavailable payload
        state, never an invented empty envelope.
        """
        return self._resolve_archive_payload(
            event_id=event_id,
            batch_id=batch_id,
            payload_sha256=payload_sha256,
            deadline=time.monotonic() + self._config.request_timeout_seconds,
        )

    def page_resolver(self) -> "_PageArchiveResolver":
        """Give one cold-history page a single aggregate worker deadline."""
        return _PageArchiveResolver(
            self, time.monotonic() + self._config.request_timeout_seconds
        )

    def _resolve_archive_payload(
        self,
        *,
        event_id: str,
        batch_id: str,
        payload_sha256: str,
        deadline: float,
    ) -> str | None:
        if time.monotonic() >= deadline:
            return None
        if not all(
            isinstance(value, str)
            and value
            and len(value) <= 256
            and value.strip() == value
            for value in (event_id, batch_id, payload_sha256)
        ):
            return None
        body = json.dumps(
            {
                "event_id": event_id,
                "batch_id": batch_id,
                "payload_sha256": payload_sha256,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        if not self._token or len(body) > self._config.max_request_bytes:
            return None
        if not self._slots.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return None
        try:
            response = self._transport(
                "POST",
                self._config.worker_url + "/_internal/analytics/archive-payload",
                {
                    "Accept": "application/json",
                    API_TO_WORKER_HEADER: self._token,
                },
                body,
            )
            if (
                response.status != 200
                or len(response.body) > self._config.max_response_bytes
            ):
                return None
            decoded = json.loads(response.body.decode("utf-8"))
            payload = decoded.get("payload_json") if isinstance(decoded, dict) else None
            if not isinstance(payload, str):
                return None
            if hashlib.sha256(payload.encode("utf-8")).hexdigest() != payload_sha256:
                return None
            return payload
        except (Exception,):  # noqa: BLE001 - archive availability is opaque
            return None
        finally:
            self._slots.release()

    def health_state(self) -> str:
        """Return the worker's separate readiness state without public routing."""
        if not self._token:
            return "unavailable"
        deadline = time.monotonic() + self._config.request_timeout_seconds
        if not self._slots.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return "unavailable"
        try:
            response = self._transport(
                "GET",
                self._config.worker_url + "/_internal/analytics/health",
                {"Accept": "application/json", API_TO_WORKER_HEADER: self._token},
                b"",
            )
            if (
                response.status != 200
                or len(response.body) > self._config.max_response_bytes
            ):
                return "unavailable"
            payload = json.loads(response.body.decode("utf-8"))
            state = payload.get("state") if isinstance(payload, dict) else None
            return state if state in {"ok", "degraded"} else "unavailable"
        except Exception:  # noqa: BLE001 - readiness must not leak transport detail
            return "unavailable"
        finally:
            self._slots.release()

    def _http_transport(
        self, method: str, target: str, headers: dict[str, str], body: bytes
    ) -> BoundaryResponse:
        parsed = urlsplit(target)
        if parsed.hostname not in _LOOPBACK_HOSTS or parsed.scheme != "http":
            raise AnalyticsBoundaryUnavailable("analytics worker unavailable")
        deadline = time.monotonic() + self._config.request_timeout_seconds
        connection = http.client.HTTPConnection(
            parsed.hostname,
            parsed.port,
            timeout=self._config.connect_timeout_seconds,
        )
        try:
            connection.connect()
            if connection.sock is None:
                raise AnalyticsBoundaryUnavailable("analytics worker unavailable")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AnalyticsBoundaryUnavailable("analytics worker unavailable")
            connection.sock.settimeout(remaining)
            request_target = parsed.path or "/"
            if parsed.query:
                request_target += "?" + parsed.query
            if body:
                headers = {**headers, "Content-Type": "application/json"}
            connection.request(
                method, request_target, body=body or None, headers=headers
            )
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if length is not None and int(length) > self._config.max_response_bytes:
                raise AnalyticsBoundaryUnavailable("analytics response too large")
            payload = response.read(self._config.max_response_bytes + 1)
            return BoundaryResponse(
                status=response.status,
                content_type=response.getheader("Content-Type") or "application/json",
                body=payload,
            )
        except (OSError, ValueError, http.client.HTTPException, socket.timeout) as exc:
            raise AnalyticsBoundaryUnavailable("analytics worker unavailable") from exc
        finally:
            connection.close()


class _PageArchiveResolver:
    """A request-scoped archive resolver with one finite cold-read budget."""

    def __init__(self, client: AnalyticsBoundaryClient, deadline: float) -> None:
        self._client = client
        self._deadline = deadline

    def resolve(
        self, *, event_id: str, batch_id: str, payload_sha256: str
    ) -> str | None:
        return self._client._resolve_archive_payload(
            event_id=event_id,
            batch_id=batch_id,
            payload_sha256=payload_sha256,
            deadline=self._deadline,
        )


def analytics_unavailable_response() -> BoundaryResponse:
    return BoundaryResponse(
        503, "application/json", ANALYTICS_UNAVAILABLE_BODY.encode("utf-8")
    )


def _is_loopback_client(host: str) -> bool:
    return host in _LOOPBACK_HOSTS


def start_analytics_boundary_server(
    *,
    host: str,
    port: int,
    token: str,
    config: AnalyticsBoundaryConfig,
    dispatch: BoundaryDispatcher,
    archive_payload_resolver: ArchivePayloadResolver | None = None,
    health_provider: WorkerHealthProvider | None = None,
) -> ThreadingHTTPServer:
    """Start the worker-only internal listener.

    It accepts only the API credential on a loopback socket and only calls the
    supplied typed dispatcher after revalidating the public route allowlist.
    """

    if host not in _LOOPBACK_HOSTS:
        raise ValueError("analytics boundary listener must bind a loopback host")
    secret = token.strip()
    if not secret:
        raise ValueError("analytics boundary token is required")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._handle()

        def do_POST(self) -> None:  # noqa: N802
            self._handle()

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle()

        def _handle(self) -> None:
            parsed = urlsplit(self.path)
            if not _is_loopback_client(
                self.client_address[0]
            ) or not hmac.compare_digest(
                self.headers.get(API_TO_WORKER_HEADER, ""), secret
            ):
                self._send(401, "application/json", b'{"error":"unauthorized"}\n')
                return
            try:
                if (
                    self.command == "GET"
                    and parsed.path == "/_internal/analytics/health"
                    and not parsed.query
                ):
                    self._handle_health()
                    return
                if (
                    self.command == "POST"
                    and parsed.path == "/_internal/analytics/archive-payload"
                    and not parsed.query
                ):
                    self._handle_archive_payload()
                    return
                validate_analytics_request(self.command, parsed.path, parsed.query)
                raw_length = self.headers.get("Content-Length", "0")
                length = int(raw_length)
                if not 0 <= length <= config.max_request_bytes:
                    raise ValueError("analytics request is too large")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("incomplete analytics request")
                response = dispatch(self.command, parsed.path, parsed.query, body)
                if len(response.body) > config.max_response_bytes:
                    raise ValueError("analytics response is too large")
            except (ValueError, UnicodeError):
                self._send(
                    400, "application/json", b'{"error":"invalid analytics request"}\n'
                )
                return
            except (
                Exception
            ):  # noqa: BLE001 - do not reveal worker internals over boundary
                self._send(503, "application/json", ANALYTICS_UNAVAILABLE_BODY.encode())
                return
            self._send(response.status, response.content_type, response.body)

        def _handle_health(self) -> None:
            if health_provider is None:
                state: Mapping[str, Any] = {"state": "degraded"}
            else:
                state = health_provider()
            if state.get("state") not in {"ok", "degraded"}:
                state = {"state": "degraded"}
            body = json.dumps(dict(state), separators=(",", ":")).encode("utf-8")
            if len(body) > config.max_response_bytes:
                raise ValueError("analytics health response is too large")
            self._send(200, "application/json", body)

        def _handle_archive_payload(self) -> None:
            if archive_payload_resolver is None:
                self._send(503, "application/json", ANALYTICS_UNAVAILABLE_BODY.encode())
                return
            raw_length = self.headers.get("Content-Length", "0")
            length = int(raw_length)
            if not 2 <= length <= config.max_request_bytes:
                raise ValueError("invalid archive payload request")
            body = self.rfile.read(length)
            value = json.loads(body.decode("utf-8"))
            if not isinstance(value, dict) or set(value) != {
                "event_id",
                "batch_id",
                "payload_sha256",
            }:
                raise ValueError("invalid archive payload request")
            fields = tuple(
                value[key] for key in ("event_id", "batch_id", "payload_sha256")
            )
            if not all(
                isinstance(item, str)
                and item
                and len(item) <= 256
                and item.strip() == item
                for item in fields
            ):
                raise ValueError("invalid archive payload request")
            payload = archive_payload_resolver(*fields)
            if payload is None:
                self._send(503, "application/json", ANALYTICS_UNAVAILABLE_BODY.encode())
                return
            if len(payload.encode("utf-8")) > config.max_response_bytes:
                raise ValueError("analytics response is too large")
            rendered = json.dumps(
                {"payload_json": payload}, separators=(",", ":")
            ).encode()
            self._send(200, "application/json", rendered)

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(
        target=server.serve_forever, name="drover-analytics-boundary", daemon=True
    ).start()
    return server


def safe_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return only response metadata safe to re-emit to a public caller."""

    content_type = headers.get("Content-Type") or headers.get("content-type")
    if not content_type:
        return {}
    return {"Content-Type": content_type}


def _valid_host_id(host_id: str) -> bool:
    return (
        bool(host_id)
        and len(host_id) <= 256
        and host_id.strip() == host_id
        and "/" not in host_id
        and "\\" not in host_id
        and host_id not in {".", ".."}
    )


def _valid_target_ids(target_ids: list[str]) -> bool:
    return (
        bool(target_ids)
        and len(target_ids) <= 256
        and len(set(target_ids)) == len(target_ids)
        and all(
            isinstance(value, str)
            and value
            and value.strip() == value
            and len(value) <= 256
            and "/" not in value
            and "\\" not in value
            for value in target_ids
        )
    )


class HostDataBridgeClient:
    """Fixed worker-to-API bridge for relay-owned provider/content calls."""

    def __init__(
        self,
        config: AnalyticsBoundaryConfig,
        *,
        token: str,
        transport: BoundaryTransport | None = None,
    ) -> None:
        self._config = config
        self._token = token.strip()
        self._transport = transport or self._http_transport
        self._slots = threading.BoundedSemaphore(config.max_concurrent_requests)

    def fetch_provider_usage(self, host_id: str) -> Mapping[str, Any]:
        return self._json_request("GET", host_id, "provider-usage", b"", 1_048_576)

    def fetch_content_bundle(
        self, host_id: str, target_ids: list[str]
    ) -> Mapping[str, Any]:
        if not _valid_target_ids(target_ids):
            raise ValueError("target_ids must be a non-empty list of unique IDs")
        return self._json_request(
            "POST",
            host_id,
            "content-bundle",
            json.dumps({"target_ids": target_ids}, separators=(",", ":")).encode(),
            4 * 1024 * 1024,
        )

    def fetch_content_version(
        self, host_id: str, target_ids: list[str]
    ) -> Mapping[str, Any]:
        if not _valid_target_ids(target_ids):
            raise ValueError("target_ids must be a non-empty list of unique IDs")
        return self._json_request(
            "POST",
            host_id,
            "content-version",
            json.dumps({"target_ids": target_ids}, separators=(",", ":")).encode(),
            256 * 1024,
        )

    def propagate_content_consent(
        self, *, enabled: bool, epoch: int
    ) -> list[dict[str, str]]:
        response = self._request(
            "POST",
            "/_internal/analytics/content-consent",
            json.dumps(
                {"enabled": enabled, "epoch": epoch}, separators=(",", ":")
            ).encode(),
            self._config.max_response_bytes,
        )
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HostDataBridgeUnavailable("API host bridge unavailable") from exc
        hosts = payload.get("hosts") if isinstance(payload, dict) else None
        if not isinstance(hosts, list) or any(
            not isinstance(item, dict) for item in hosts
        ):
            raise HostDataBridgeUnavailable("API host bridge unavailable")
        return [
            {
                "host_id": str(item.get("host_id", "")),
                "state": str(item.get("state", "failed")),
            }
            for item in hosts
        ]

    def _json_request(
        self,
        method: str,
        host_id: str,
        operation: str,
        body: bytes,
        response_limit: int,
    ) -> Mapping[str, Any]:
        if not _valid_host_id(host_id):
            raise ValueError("invalid host_id")
        response = self._request(
            method,
            f"/_internal/analytics/hosts/{quote(host_id, safe='')}/{operation}",
            body,
            response_limit,
        )
        if not 200 <= response.status < 300:
            raise HostDataBridgeUnavailable("API host bridge unavailable")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HostDataBridgeUnavailable("API host bridge unavailable") from exc
        if not isinstance(payload, dict):
            raise HostDataBridgeUnavailable("API host bridge unavailable")
        return payload

    def _request(
        self, method: str, path: str, body: bytes, response_limit: int
    ) -> BoundaryResponse:
        if not self._token or len(body) > self._config.max_request_bytes:
            raise HostDataBridgeUnavailable("API host bridge unavailable")
        deadline = time.monotonic() + self._config.request_timeout_seconds
        if not self._slots.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise HostDataBridgeUnavailable("API host bridge unavailable")
        try:
            response = self._transport(
                method,
                self._config.api_url + path,
                {
                    "Accept": "application/json",
                    WORKER_TO_API_HEADER: self._token,
                },
                body,
            )
            if len(response.body) > min(
                response_limit, self._config.max_response_bytes
            ):
                raise HostDataBridgeUnavailable("API host bridge response too large")
            return response
        except HostDataBridgeUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - bridge errors are opaque to worker
            raise HostDataBridgeUnavailable("API host bridge unavailable") from exc
        finally:
            self._slots.release()

    def _http_transport(
        self, method: str, target: str, headers: dict[str, str], body: bytes
    ) -> BoundaryResponse:
        parsed = urlsplit(target)
        if parsed.hostname not in _LOOPBACK_HOSTS or parsed.scheme != "http":
            raise HostDataBridgeUnavailable("API host bridge unavailable")
        deadline = time.monotonic() + self._config.request_timeout_seconds
        connection = http.client.HTTPConnection(
            parsed.hostname,
            parsed.port,
            timeout=self._config.connect_timeout_seconds,
        )
        try:
            connection.connect()
            if connection.sock is None:
                raise HostDataBridgeUnavailable("API host bridge unavailable")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostDataBridgeUnavailable("API host bridge unavailable")
            connection.sock.settimeout(remaining)
            if body:
                headers = {**headers, "Content-Type": "application/json"}
            connection.request(method, parsed.path, body=body or None, headers=headers)
            response = connection.getresponse()
            payload = response.read(self._config.max_response_bytes + 1)
            return BoundaryResponse(
                response.status,
                response.getheader("Content-Type") or "application/json",
                payload,
            )
        except (OSError, ValueError, http.client.HTTPException, socket.timeout) as exc:
            raise HostDataBridgeUnavailable("API host bridge unavailable") from exc
        finally:
            connection.close()
