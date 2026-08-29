"""Real HTTP contract tests for the pinned Pond archive reader."""

from __future__ import annotations

import gc
import json
import logging
import socket
import threading
import time
import weakref
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field, fields, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from drover.config import ArchiveConfig
from drover.server.archive import (
    ArchiveMessage,
    ArchiveMessageNeighborhood,
    ArchiveMessageRequest,
    ArchivePartSummary,
    ArchiveProtocolError,
    ArchiveRequestRejected,
    ArchiveResponseTooLarge,
    ArchiveSearchHit,
    ArchiveSearchRequest,
    ArchiveSearchResult,
    ArchiveSession,
    ArchiveStorageUnavailable,
    ArchiveTimeout,
    ArchiveUnavailable,
)
from drover.server.archive.pond import PondArchiveClient


@dataclass
class _ResponsePlan:
    body: bytes
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    chunks: tuple[bytes, ...] | None = None
    delay_seconds: float = 0.0
    chunk_delay_seconds: float = 0.0
    on_request: Callable[[], None] | None = None


class _TestHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _PondHandler)
        self.requests: list[tuple[str, object]] = []
        self.plans: deque[_ResponsePlan] = deque()
        self.lock = threading.Lock()

    def handle_error(self, request, client_address) -> None:
        pass


class _PondHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        server = self.server
        assert isinstance(server, _TestHTTPServer)
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_request = self.rfile.read(content_length)
        try:
            parsed_request: object = json.loads(raw_request)
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed_request = None
        with server.lock:
            server.requests.append((self.path, parsed_request))
            plan = server.plans.popleft()

        if plan.on_request is not None:
            plan.on_request()
        if plan.delay_seconds:
            time.sleep(plan.delay_seconds)

        try:
            self.send_response(plan.status)
            for name, value in plan.headers.items():
                self.send_header(name, value)
            if plan.chunks is None:
                self.send_header("Content-Length", str(len(plan.body)))
            else:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if plan.chunks is None:
                self.wfile.write(plan.body)
            else:
                for index, chunk in enumerate(plan.chunks):
                    self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                    self.wfile.write(chunk + b"\r\n")
                    self.wfile.flush()
                    if index < len(plan.chunks) - 1 and plan.chunk_delay_seconds:
                        time.sleep(plan.chunk_delay_seconds)
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def pond_server():
    server = _TestHTTPServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _config(
    server: _TestHTTPServer | None = None,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 1.0,
    max_response_bytes: int = 64_000,
) -> ArchiveConfig:
    if base_url is None:
        assert server is not None
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
    return ArchiveConfig(
        enabled=True,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        search_limit=5,
        context_before=2,
        context_after=2,
        max_context_chars=24_000,
        max_response_bytes=max_response_bytes,
    )


def _enqueue_json(
    server: _TestHTTPServer,
    value: object,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    chunks: tuple[bytes, ...] | None = None,
    delay_seconds: float = 0.0,
    chunk_delay_seconds: float = 0.0,
    on_request: Callable[[], None] | None = None,
) -> bytes:
    body = json.dumps(value, separators=(",", ":")).encode()
    if chunks == ():
        midpoint = len(body) // 2
        chunks = (body[:midpoint], body[midpoint:])
    server.plans.append(
        _ResponsePlan(
            body=body,
            status=status,
            headers=headers or {},
            chunks=chunks,
            delay_seconds=delay_seconds,
            chunk_delay_seconds=chunk_delay_seconds,
            on_request=on_request,
        )
    )
    return body


def _search_response() -> dict[str, object]:
    return {
        "sessions": [
            {
                "session_id": "session-1",
                "project": "arniesaha/drover",
                "source_agent": "codex",
                "session_messages_count": 18,
                "matched_message_count": 2,
                "matches": [
                    {
                        "message_id": "message-12",
                        "role": "assistant",
                        "timestamp": "2026-08-28T12:02:00Z",
                        "text": "transitioned to retrying",
                        "score": 0.81,
                    },
                    {
                        "message_id": "message-11",
                        "role": "user",
                        "timestamp": "2026-08-28T12:01:00Z",
                        "text": "retry state machine",
                        "score": 0.98,
                        "parts_summary": [
                            {
                                "kind": "file",
                                "label": "retry-plan.md",
                            },
                        ],
                    },
                ],
            },
            {
                "session_id": "session-2",
                "project": "arniesaha/drover",
                "source_agent": "claude",
                "session_messages_count": 7,
                "matched_message_count": 1,
                "matches": [
                    {
                        "message_id": "message-21",
                        "role": "system",
                        "timestamp": "2026-08-27T09:30:00Z",
                        "text": "state-machine policy",
                        "score": 0.98,
                    }
                ],
            },
        ],
        "matched_total": 3,
        "searchable_in_scope": 25,
        "has_more": True,
    }


def _message_response() -> dict[str, object]:
    return {
        "session": {
            "id": "session-1",
            "source_agent": "codex",
            "project": "arniesaha/drover",
            "created_at": "2026-08-28T11:00:00Z",
        },
        "scope": "message",
        "target": {
            "id": "message-11",
            "role": "user",
            "timestamp": "2026-08-28T12:01:00Z",
        },
        "target_parts": [
            {"type": "text", "text": "retry state machine"},
            {"type": "tool_call", "name": "shell", "secret": "not retained"},
        ],
        "target_parts_remaining": 4,
        "siblings": [
            {
                "id": "message-10",
                "role": "system",
                "timestamp": "2026-08-28T12:00:00Z",
                "content": "system context",
            },
            {
                "id": "message-12",
                "role": "assistant",
                "timestamp": "2026-08-28T12:02:00Z",
                "text": "transitioned to retrying",
                "parts_summary": [
                    {"kind": "tool_call", "label": "shell", "call_id": "call-12"}
                ],
            },
        ],
        "context_before": 1,
        "context_after": 1,
    }


def test_search_posts_exact_contract_and_flattens_grouped_sessions_in_order(
    pond_server,
):
    _enqueue_json(pond_server, _search_response(), chunks=())
    client = PondArchiveClient(_config(pond_server))

    result = client.search(
        ArchiveSearchRequest(
            query="retry state machine",
            project="arniesaha/drover",
            since="2026-08-01",
            limit=3,
        )
    )

    assert pond_server.requests == [
        (
            "/v1/search",
            {
                "protocol_version": 1,
                "namespace": "local",
                "query": "retry state machine",
                "mode": "fts",
                "sort_by": "relevance",
                "filters": {
                    "project": {"contains": "arniesaha/drover"},
                    "from_date": "2026-08-01",
                },
                "limit": 3,
            },
        )
    ]
    assert result == ArchiveSearchResult(
        hits=(
            ArchiveSearchHit(
                rank=1,
                message_id="message-11",
                session_id="session-1",
                project="arniesaha/drover",
                source_agent="codex",
                role="user",
                timestamp="2026-08-28T12:01:00Z",
                text="retry state machine",
                score=0.98,
                parts_summary=(ArchivePartSummary(kind="file", label="retry-plan.md"),),
            ),
            ArchiveSearchHit(
                rank=2,
                message_id="message-21",
                session_id="session-2",
                project="arniesaha/drover",
                source_agent="claude",
                role="system",
                timestamp="2026-08-27T09:30:00Z",
                text="state-machine policy",
                score=0.98,
                parts_summary=(),
            ),
            ArchiveSearchHit(
                rank=3,
                message_id="message-12",
                session_id="session-1",
                project="arniesaha/drover",
                source_agent="codex",
                role="assistant",
                timestamp="2026-08-28T12:02:00Z",
                text="transitioned to retrying",
                score=0.81,
                parts_summary=(),
            ),
        ),
        matched_total=3,
        searchable_in_scope=25,
        has_more=True,
    )


def test_search_rejects_timestamp_shaped_from_date_before_http(pond_server):
    _enqueue_json(pond_server, _search_response())
    client = PondArchiveClient(_config(pond_server))

    with pytest.raises(ValueError, match="since.*YYYY-MM-DD"):
        client.search(
            ArchiveSearchRequest(
                query="retry state machine",
                since="2026-08-01T00:00:00Z",
            )
        )

    assert pond_server.requests == []


def test_get_message_posts_exact_contract_and_normalizes_without_raw_parts(
    pond_server,
):
    _enqueue_json(pond_server, _message_response())
    client = PondArchiveClient(_config(pond_server))

    result = client.get_message(
        ArchiveMessageRequest(
            message_id="message-11", context_before=1, context_after=1
        )
    )

    assert pond_server.requests == [
        (
            "/v1/get-message",
            {
                "protocol_version": 1,
                "namespace": "local",
                "id": "message-11",
                "context_before": 1,
                "context_after": 1,
            },
        )
    ]
    assert result == ArchiveMessageNeighborhood(
        session=ArchiveSession(
            session_id="session-1",
            project="arniesaha/drover",
            source_agent="codex",
            created_at="2026-08-28T11:00:00Z",
        ),
        target=ArchiveMessage(
            message_id="message-11",
            session_id="session-1",
            project="arniesaha/drover",
            source_agent="codex",
            role="user",
            timestamp="2026-08-28T12:01:00Z",
            text=None,
            parts=(),
        ),
        siblings=(
            ArchiveMessage(
                message_id="message-10",
                session_id="session-1",
                project="arniesaha/drover",
                source_agent="codex",
                role="system",
                timestamp="2026-08-28T12:00:00Z",
                text="system context",
            ),
            ArchiveMessage(
                message_id="message-12",
                session_id="session-1",
                project="arniesaha/drover",
                source_agent="codex",
                role="assistant",
                timestamp="2026-08-28T12:02:00Z",
                text="transitioned to retrying",
                parts=(
                    ArchivePartSummary(
                        kind="tool_call", label="shell", call_id="call-12"
                    ),
                ),
            ),
        ),
        target_part_count=2,
        target_parts_remaining=4,
        context_before=1,
        context_after=1,
    )
    assert "not retained" not in repr(result)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["sessions"][0].pop("session_id"),
        lambda body: body["sessions"][0]["matches"][0].pop("message_id"),
        lambda body: body["sessions"][0]["matches"][0].pop("text"),
        lambda body: body["sessions"][0].update(session_id=""),
        lambda body: body["sessions"][0]["matches"][0].update(message_id=""),
    ],
)
def test_search_rejects_missing_or_empty_identifiers(pond_server, mutate):
    body = _search_response()
    mutate(body)
    _enqueue_json(pond_server, body)

    with pytest.raises(ArchiveProtocolError):
        PondArchiveClient(_config(pond_server)).search(ArchiveSearchRequest("query"))


@pytest.mark.parametrize(
    ("operation", "mutate"),
    [
        ("search", lambda body: body.update(sessions={})),
        ("search", lambda body: body["sessions"][0].update(matches={})),
        (
            "search",
            lambda body: body["sessions"][0]["matches"][0].update(parts_summary={}),
        ),
        ("get", lambda body: body.update(siblings={})),
        ("get", lambda body: body.update(target_parts={})),
        ("get", lambda body: body["target"].update(parts_summary={})),
    ],
)
def test_wrong_collection_shapes_are_protocol_errors(pond_server, operation, mutate):
    body = _search_response() if operation == "search" else _message_response()
    mutate(body)
    _enqueue_json(pond_server, body)
    client = PondArchiveClient(_config(pond_server))

    with pytest.raises(ArchiveProtocolError):
        if operation == "search":
            client.search(ArchiveSearchRequest("query"))
        else:
            client.get_message(ArchiveMessageRequest("message-11"))


@pytest.mark.parametrize(
    ("operation", "mutate"),
    [
        ("search", lambda body: body.update(matched_total=True)),
        (
            "search",
            lambda body: body["sessions"][0]["matches"][0].update(score=True),
        ),
        ("get", lambda body: body.update(context_before=False)),
        ("get", lambda body: body.update(target_parts_remaining=True)),
    ],
)
def test_boolean_numeric_fields_are_protocol_errors(pond_server, operation, mutate):
    body = _search_response() if operation == "search" else _message_response()
    mutate(body)
    _enqueue_json(pond_server, body)
    client = PondArchiveClient(_config(pond_server))

    with pytest.raises(ArchiveProtocolError):
        if operation == "search":
            client.search(ArchiveSearchRequest("query"))
        else:
            client.get_message(ArchiveMessageRequest("message-11"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["session"].pop("id"),
        lambda body: body["target"].pop("id"),
        lambda body: body["siblings"][0].pop("id"),
        lambda body: body["siblings"][0].pop("content"),
        lambda body: body["target"].update(text="one", content="duplicate"),
    ],
)
def test_get_message_rejects_missing_identifiers_or_ambiguous_content(
    pond_server, mutate
):
    body = _message_response()
    mutate(body)
    _enqueue_json(pond_server, body)

    with pytest.raises(ArchiveProtocolError):
        PondArchiveClient(_config(pond_server)).get_message(
            ArchiveMessageRequest("message-11")
        )


def test_get_message_rejects_non_message_scope(pond_server):
    body = _message_response()
    body["scope"] = "session"
    _enqueue_json(pond_server, body)

    with pytest.raises(ArchiveProtocolError):
        PondArchiveClient(_config(pond_server)).get_message(
            ArchiveMessageRequest("message-11")
        )


def test_malformed_json_is_a_protocol_error(pond_server):
    pond_server.plans.append(_ResponsePlan(body=b'{"sessions":['))

    with pytest.raises(ArchiveProtocolError):
        PondArchiveClient(_config(pond_server)).search(ArchiveSearchRequest("query"))


def test_top_level_error_envelope_is_a_sanitized_request_failure(pond_server, caplog):
    secret = "UPSTREAM-ERROR-DETAIL-7819"
    _enqueue_json(
        pond_server,
        {"error": {"code": "bad_query", "message": secret, "details": secret}},
    )

    with caplog.at_level(logging.INFO, logger="drover.server.archive.pond"):
        with pytest.raises(ArchiveRequestRejected) as caught:
            PondArchiveClient(_config(pond_server)).search(
                ArchiveSearchRequest("query")
            )

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret not in caplog.text


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (302, ArchiveRequestRejected),
        (404, ArchiveRequestRejected),
        (500, ArchiveStorageUnavailable),
        (503, ArchiveStorageUnavailable),
    ],
)
def test_http_statuses_map_to_typed_sanitized_failures(pond_server, status, error_type):
    body = b"STATUS-BODY-SECRET-6421"
    pond_server.plans.append(_ResponsePlan(body=body, status=status))

    with pytest.raises(error_type) as caught:
        PondArchiveClient(_config(pond_server)).search(ArchiveSearchRequest("query"))

    assert caught.value.status_code == status
    assert caught.value.byte_count == len(body)
    assert "STATUS-BODY-SECRET-6421" not in repr(caught.value)


def test_redirect_is_rejected_without_contacting_decoy_server(pond_server, request):
    decoy = _TestHTTPServer()
    decoy_thread = threading.Thread(target=decoy.serve_forever, daemon=True)
    decoy_thread.start()
    request.addfinalizer(lambda: decoy_thread.join(timeout=2))
    request.addfinalizer(decoy.server_close)
    request.addfinalizer(decoy.shutdown)
    decoy.plans.append(_ResponsePlan(body=b"decoy"))
    host, port = decoy.server_address
    pond_server.plans.append(
        _ResponsePlan(
            body=b"redirect secret",
            status=307,
            headers={"Location": f"http://{host}:{port}/decoy"},
        )
    )

    with pytest.raises(ArchiveRequestRejected):
        PondArchiveClient(_config(pond_server)).search(ArchiveSearchRequest("query"))

    assert decoy.requests == []


def test_slow_response_raises_timeout(pond_server):
    _enqueue_json(
        pond_server,
        _search_response(),
        delay_seconds=0.3,
    )

    with pytest.raises(ArchiveTimeout):
        PondArchiveClient(_config(pond_server, timeout_seconds=0.1)).search(
            ArchiveSearchRequest("query")
        )


def test_slow_stream_chunk_raises_timeout(pond_server):
    _enqueue_json(
        pond_server,
        _search_response(),
        chunks=(),
        chunk_delay_seconds=0.3,
    )

    with pytest.raises(ArchiveTimeout):
        PondArchiveClient(_config(pond_server, timeout_seconds=0.1)).search(
            ArchiveSearchRequest("query")
        )


def test_connection_refused_raises_unavailable():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    with pytest.raises(ArchiveUnavailable):
        PondArchiveClient(
            _config(base_url=f"http://127.0.0.1:{port}", timeout_seconds=0.2)
        ).search(ArchiveSearchRequest("query"))


def test_stream_cap_closes_response_and_reports_observed_crossing_size(
    pond_server, monkeypatch
):
    body = b'{"blob":"' + (b"x" * 1_200) + b'"}'
    pond_server.plans.append(_ResponsePlan(body=body, chunks=(body[:800], body[800:])))
    close_calls: list[int] = []
    original_close = requests.Response.close

    def record_close(response):
        close_calls.append(1)
        return original_close(response)

    monkeypatch.setattr(requests.Response, "close", record_close)

    with pytest.raises(ArchiveResponseTooLarge) as caught:
        PondArchiveClient(_config(pond_server, max_response_bytes=1_024)).search(
            ArchiveSearchRequest("query")
        )

    assert caught.value.byte_count == len(body)
    assert close_calls == [1]


def test_response_context_closes_before_next_operation_and_is_not_retained(
    pond_server, monkeypatch
):
    response_closed = threading.Event()
    second_observations: list[bool] = []
    response_refs: list[weakref.ReferenceType[requests.Response]] = []
    original_close = requests.Response.close

    def record_close(response):
        response_refs.append(weakref.ref(response))
        response_closed.set()
        return original_close(response)

    monkeypatch.setattr(requests.Response, "close", record_close)
    _enqueue_json(pond_server, _search_response())
    _enqueue_json(
        pond_server,
        _message_response(),
        on_request=lambda: second_observations.append(response_closed.wait(timeout=1)),
    )
    client = PondArchiveClient(_config(pond_server))

    search_result = client.search(ArchiveSearchRequest("query"))
    message_result = client.get_message(ArchiveMessageRequest("message-11"))

    assert second_observations == [True]
    assert response_refs
    gc.collect()
    assert all(reference() is None for reference in response_refs)
    assert not _contains_response(search_result)
    assert not _contains_response(message_result)


def _contains_response(value: object) -> bool:
    if isinstance(value, requests.Response):
        return True
    if is_dataclass(value) and not isinstance(value, type):
        return any(
            _contains_response(getattr(value, item.name)) for item in fields(value)
        )
    if isinstance(value, (tuple, list)):
        return any(_contains_response(item) for item in value)
    return False


def test_default_session_ignores_proxy_environment(pond_server, monkeypatch):
    _enqueue_json(pond_server, _search_response())
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    result = PondArchiveClient(_config(pond_server)).search(
        ArchiveSearchRequest("query")
    )

    assert result.matched_total == 3
    assert len(pond_server.requests) == 1


def test_injected_session_has_proxy_and_retry_configuration_neutralized(pond_server):
    _enqueue_json(pond_server, _search_response())
    injected = requests.Session()
    injected.trust_env = True
    injected.proxies = {"http": "http://127.0.0.1:1"}
    host, port = pond_server.server_address
    server_prefix = f"http://{host}:{port}/"
    injected.mount(
        server_prefix,
        HTTPAdapter(
            max_retries=Retry(
                total=7,
                connect=6,
                read=5,
                redirect=4,
                status=3,
            )
        ),
    )

    result = PondArchiveClient(_config(pond_server), session=injected).search(
        ArchiveSearchRequest("query")
    )

    retries = injected.get_adapter(server_prefix).max_retries
    assert result.matched_total == 3
    assert injected.trust_env is False
    assert injected.proxies == {}
    assert retries.total == 0
    assert retries.connect == 0
    assert retries.read == 0
    assert retries.redirect == 0
    assert retries.status == 0


def test_v0163_get_message_target_body_is_only_in_target_parts(pond_server):
    body = {
        "session": {
            "id": "session-literal",
            "source_agent": "codex",
            "project": "arniesaha/drover",
            "created_at": "2026-08-28T11:00:00Z",
        },
        "scope": "message",
        "target": {
            "id": "message-literal",
            "role": "assistant",
            "timestamp": "2026-08-28T12:01:00Z",
        },
        "target_parts": [
            {
                "id": "part-1",
                "ordinal": 0,
                "provenance": "conversational",
                "type": "text",
                "text": "body lives here",
            }
        ],
        "target_parts_remaining": 0,
        "siblings": [
            {
                "id": "message-before",
                "role": "user",
                "timestamp": "2026-08-28T12:00:00Z",
                "text": "hydrate this sibling",
                "parts_summary": [{"kind": "file", "label": "input.txt"}],
            }
        ],
        "context_before": 1,
        "context_after": 0,
    }
    _enqueue_json(pond_server, body)

    result = PondArchiveClient(_config(pond_server)).get_message(
        ArchiveMessageRequest("message-literal", context_before=1)
    )

    assert result.target.text is None
    assert result.target.parts == ()
    assert result.target_part_count == 1
    assert result.siblings[0].text == "hydrate this sibling"
    assert result.siblings[0].parts == (
        ArchivePartSummary(kind="file", label="input.txt"),
    )


def test_get_message_rejects_mismatched_target_id_with_sanitized_diagnostic(
    pond_server, caplog
):
    body = _message_response()
    body["target"]["id"] = "MISMATCHED-TARGET-SECRET"
    raw = _enqueue_json(pond_server, body)

    with caplog.at_level(logging.INFO, logger="drover.server.archive.pond"):
        with pytest.raises(ArchiveProtocolError) as caught:
            PondArchiveClient(_config(pond_server)).get_message(
                ArchiveMessageRequest("message-11")
            )

    assert caught.value.status_code == 200
    assert caught.value.byte_count == len(raw)
    records = [
        record
        for record in caplog.records
        if record.name == "drover.server.archive.pond"
    ]
    assert len(records) == 1
    assert "operation=get_message" in records[0].getMessage()
    assert "category=protocol_error" in records[0].getMessage()
    assert "MISMATCHED-TARGET-SECRET" not in caplog.text
    assert "MISMATCHED-TARGET-SECRET" not in repr(caught.value)


@pytest.mark.parametrize(
    "parts_summary",
    [
        [{"kind": "text"}],
        [{"kind": "reasoning"}],
        [{"kind": "future_kind"}],
        [{"kind": "file", "label": "input.txt", "call_id": "impossible"}],
        [{"kind": "tool_approval_request"}],
        [{"kind": "tool_approval_request", "label": "approval-1", "call_id": "x"}],
        [{"kind": "tool_approval_response"}],
        [
            {
                "kind": "tool_approval_response",
                "label": "approval-1 (approved)",
                "call_id": "impossible",
            }
        ],
    ],
)
def test_v0163_rejects_invalid_part_summary_shapes(pond_server, parts_summary):
    body = _search_response()
    body["sessions"][0]["matches"][0]["parts_summary"] = parts_summary
    _enqueue_json(pond_server, body)

    with pytest.raises(ArchiveProtocolError):
        PondArchiveClient(_config(pond_server)).search(ArchiveSearchRequest("query"))


def test_search_rejects_non_empty_parts_summary_for_non_user_hit(pond_server):
    body = _search_response()
    body["sessions"][0]["matches"][0]["parts_summary"] = [
        {"kind": "tool_call", "label": "shell", "call_id": "call-12"}
    ]
    _enqueue_json(pond_server, body)

    with pytest.raises(ArchiveProtocolError):
        PondArchiveClient(_config(pond_server)).search(ArchiveSearchRequest("query"))


def test_huge_integer_score_is_a_typed_protocol_error_with_diagnostic(
    pond_server, caplog
):
    body = _search_response()
    body["sessions"][0]["matches"][0]["score"] = 10**4_000
    _enqueue_json(pond_server, body)

    with caplog.at_level(logging.INFO, logger="drover.server.archive.pond"):
        with pytest.raises(ArchiveProtocolError):
            PondArchiveClient(_config(pond_server)).search(
                ArchiveSearchRequest("query")
            )

    records = [
        record
        for record in caplog.records
        if record.name == "drover.server.archive.pond"
    ]
    assert len(records) == 1
    assert "category=protocol_error" in records[0].getMessage()


def test_diagnostic_is_single_sanitized_record_with_transport_measurements(
    pond_server, caplog
):
    query_secret = "QUERY-SECRET-91531"
    body_secret = "BODY-SECRET-48276"
    raw = f'{{"sessions":[],"secret":"{body_secret}"'.encode()
    pond_server.plans.append(_ResponsePlan(body=raw))

    with caplog.at_level(logging.INFO, logger="drover.server.archive.pond"):
        with pytest.raises(ArchiveProtocolError) as caught:
            PondArchiveClient(_config(pond_server)).search(
                ArchiveSearchRequest(query_secret)
            )

    records = [
        record
        for record in caplog.records
        if record.name == "drover.server.archive.pond"
    ]
    assert len(records) == 1
    diagnostic = records[0].getMessage()
    assert "operation=search" in diagnostic
    assert "category=protocol_error" in diagnostic
    assert "elapsed_ms=" in diagnostic
    assert "status_code=200" in diagnostic
    assert f"byte_count={len(raw)}" in diagnostic
    assert query_secret not in caplog.text
    assert body_secret not in caplog.text
    assert query_secret not in repr(caught.value)
    assert body_secret not in repr(caught.value)


def test_schema_protocol_failure_keeps_transport_metadata_and_error_category(
    pond_server, caplog
):
    body = _search_response()
    body["sessions"] = {}
    raw = _enqueue_json(pond_server, body)

    with caplog.at_level(logging.INFO, logger="drover.server.archive.pond"):
        with pytest.raises(ArchiveProtocolError) as caught:
            PondArchiveClient(_config(pond_server)).search(
                ArchiveSearchRequest("query")
            )

    assert caught.value.status_code == 200
    assert caught.value.byte_count == len(raw)
    records = [
        record
        for record in caplog.records
        if record.name == "drover.server.archive.pond"
    ]
    assert len(records) == 1
    assert "category=protocol_error" in records[0].getMessage()
