"""Pinned, bounded HTTP reader for the Pond v0.16.3 protocol."""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import date
from typing import Any, cast

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import ReadTimeoutError
from urllib3.util.retry import Retry

from drover.config import ArchiveConfig
from drover.server.archive.errors import (
    ArchiveError,
    ArchiveProtocolError,
    ArchiveRequestRejected,
    ArchiveResponseTooLarge,
    ArchiveStorageUnavailable,
    ArchiveTimeout,
    ArchiveUnavailable,
)
from drover.server.archive.types import (
    ArchiveMessage,
    ArchiveMessageNeighborhood,
    ArchiveMessageRequest,
    ArchivePartSummary,
    ArchiveSearchHit,
    ArchiveSearchRequest,
    ArchiveSearchResult,
    ArchiveSession,
)

_LOG = logging.getLogger(__name__)
_PROTOCOL_VERSION = 1
_NAMESPACE = "local"
_STREAM_CHUNK_BYTES = 65_536
_SUMMARY_KINDS = frozenset(
    {
        "file",
        "tool_call",
        "tool_result",
        "tool_approval_request",
        "tool_approval_response",
    }
)
_SUMMARY_CALL_ID_KINDS = frozenset({"tool_call", "tool_result"})
_SUMMARY_APPROVAL_KINDS = frozenset({"tool_approval_request", "tool_approval_response"})


class PondArchiveClient:
    """Normalize Pond's HTTP response into Drover-owned archive values."""

    def __init__(
        self, config: ArchiveConfig, session: requests.Session | None = None
    ) -> None:
        self._config = config
        if session is None:
            session = requests.Session()
        session.trust_env = False
        session.proxies.clear()
        session.adapters.clear()
        session.mount("http://", _zero_retry_adapter())
        session.mount("https://", _zero_retry_adapter())
        self._session = session

    def search(self, request: ArchiveSearchRequest) -> ArchiveSearchResult:
        filters: dict[str, object] = {}
        if request.project is not None:
            filters["project"] = {"contains": request.project}
        if request.since is not None:
            filters["from_date"] = _validate_from_date(request.since)
        payload = {
            "protocol_version": _PROTOCOL_VERSION,
            "namespace": _NAMESPACE,
            "query": request.query,
            "mode": "fts",
            "sort_by": "relevance",
            "filters": filters,
            "limit": request.limit,
        }
        return cast(
            ArchiveSearchResult,
            self._post_json("search", "/v1/search", payload),
        )

    def get_message(self, request: ArchiveMessageRequest) -> ArchiveMessageNeighborhood:
        payload = {
            "protocol_version": _PROTOCOL_VERSION,
            "namespace": _NAMESPACE,
            "id": request.message_id,
            "context_before": request.context_before,
            "context_after": request.context_after,
        }
        return cast(
            ArchiveMessageNeighborhood,
            self._post_json(
                "get_message",
                "/v1/get-message",
                payload,
                expected_message_id=request.message_id,
            ),
        )

    def _post_json(
        self,
        operation: str,
        path: str,
        payload: dict[str, object],
        *,
        expected_message_id: str | None = None,
    ) -> object:
        started = time.monotonic()
        status_code: int | None = None
        byte_count = 0
        category = "unknown"
        try:
            try:
                response = self._session.post(
                    self._config.base_url + path,
                    json=payload,
                    timeout=self._config.timeout_seconds,
                    stream=True,
                    allow_redirects=False,
                )
                status_code = response.status_code
                with response:
                    mutable_body = bytearray()
                    for chunk in response.iter_content(chunk_size=_STREAM_CHUNK_BYTES):
                        if not chunk:
                            continue
                        mutable_body.extend(chunk)
                        byte_count = len(mutable_body)
                        if byte_count > self._config.max_response_bytes:
                            raise ArchiveResponseTooLarge(
                                status_code=status_code, byte_count=byte_count
                            )
                    body = bytes(mutable_body)
            except requests.Timeout:
                raise ArchiveTimeout(
                    status_code=status_code, byte_count=byte_count
                ) from None
            except requests.ConnectionError as exc:
                if any(isinstance(item, ReadTimeoutError) for item in exc.args):
                    raise ArchiveTimeout(
                        status_code=status_code, byte_count=byte_count
                    ) from None
                raise ArchiveUnavailable(
                    status_code=status_code, byte_count=byte_count
                ) from None
            except requests.RequestException:
                raise ArchiveUnavailable(
                    status_code=status_code, byte_count=byte_count
                ) from None

            if 300 <= status_code < 500:
                raise ArchiveRequestRejected(
                    status_code=status_code, byte_count=byte_count
                )
            if status_code >= 500:
                raise ArchiveStorageUnavailable(
                    status_code=status_code, byte_count=byte_count
                )
            if not 200 <= status_code < 300:
                raise ArchiveRequestRejected(
                    status_code=status_code, byte_count=byte_count
                )

            try:
                decoded = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ArchiveProtocolError(
                    status_code=status_code, byte_count=byte_count
                ) from None
            if type(decoded) is dict and "error" in decoded:
                raise ArchiveRequestRejected(
                    status_code=status_code, byte_count=byte_count
                )
            if operation == "search":
                normalized = _normalize_search(decoded)
            elif operation == "get_message":
                if expected_message_id is None:
                    raise ArchiveProtocolError()
                normalized = _normalize_message(
                    decoded, expected_message_id=expected_message_id
                )
            else:
                raise ArchiveProtocolError()
            category = "success"
            return normalized
        except ArchiveError as exc:
            category = exc.category
            raise type(exc)(status_code=status_code, byte_count=byte_count) from None
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1_000)
            _LOG.info(
                "archive operation=%s category=%s elapsed_ms=%d "
                "status_code=%s byte_count=%d",
                operation,
                category,
                elapsed_ms,
                status_code,
                byte_count,
            )


def _validate_from_date(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 10
        or value[4] != "-"
        or value[7] != "-"
        or not value.replace("-", "").isdigit()
    ):
        raise ValueError("since must be a valid YYYY-MM-DD date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("since must be a valid YYYY-MM-DD date") from exc
    return value


def _normalize_search(value: object) -> ArchiveSearchResult:
    root = _required_mapping(value)
    matches: list[dict[str, Any]] = []
    for session_value in _required_list(root, "sessions"):
        session = _required_mapping(session_value)
        session_id = _required_string(session, "session_id", identifier=True)
        project = _required_string(session, "project")
        source_agent = _required_string(session, "source_agent")
        _required_integer(session, "session_messages_count")
        _required_integer(session, "matched_message_count")
        for match_value in _required_list(session, "matches"):
            match = _required_mapping(match_value)
            role = _required_string(match, "role")
            parts_summary = _parts_summary(match)
            if role != "user" and parts_summary:
                raise ArchiveProtocolError()
            matches.append(
                {
                    "message_id": _required_string(
                        match, "message_id", identifier=True
                    ),
                    "session_id": session_id,
                    "project": project,
                    "source_agent": source_agent,
                    "role": role,
                    "timestamp": _required_string(match, "timestamp"),
                    "text": _required_string(match, "text"),
                    "score": _required_number(match, "score"),
                    "parts_summary": parts_summary,
                }
            )
    matches.sort(
        key=lambda match: (
            -match["score"],
            match["session_id"],
            match["message_id"],
        )
    )
    hits = tuple(
        ArchiveSearchHit(rank=rank, **match)
        for rank, match in enumerate(matches, start=1)
    )
    return ArchiveSearchResult(
        hits=hits,
        matched_total=_required_integer(root, "matched_total"),
        searchable_in_scope=_required_integer(root, "searchable_in_scope"),
        has_more=_required_boolean(root, "has_more"),
    )


def _normalize_message(
    value: object, *, expected_message_id: str
) -> ArchiveMessageNeighborhood:
    root = _required_mapping(value)
    if _required_string(root, "scope") != "message":
        raise ArchiveProtocolError()
    session_value = _required_mapping(_required_value(root, "session"))
    session = ArchiveSession(
        session_id=_required_string(session_value, "id", identifier=True),
        source_agent=_required_string(session_value, "source_agent"),
        project=_required_string(session_value, "project"),
        created_at=_required_string(session_value, "created_at"),
    )
    target = _message_view(
        _required_value(root, "target"), session, allow_absent_text=True
    )
    if target.message_id != expected_message_id:
        raise ArchiveProtocolError()
    siblings = tuple(
        _message_view(item, session) for item in _required_list(root, "siblings")
    )
    target_parts = _required_list(root, "target_parts")
    return ArchiveMessageNeighborhood(
        session=session,
        target=target,
        siblings=siblings,
        target_part_count=len(target_parts),
        target_parts_remaining=_required_integer(root, "target_parts_remaining"),
        context_before=_required_integer(root, "context_before"),
        context_after=_required_integer(root, "context_after"),
    )


def _message_view(
    value: object,
    session: ArchiveSession,
    *,
    allow_absent_text: bool = False,
) -> ArchiveMessage:
    view = _required_mapping(value)
    has_text = "text" in view
    has_content = "content" in view
    if has_text and has_content:
        raise ArchiveProtocolError()
    if not has_text and not has_content:
        if not allow_absent_text:
            raise ArchiveProtocolError()
        text = None
    else:
        content_key = "text" if has_text else "content"
        text = _required_string(view, content_key)
    return ArchiveMessage(
        message_id=_required_string(view, "id", identifier=True),
        session_id=session.session_id,
        project=session.project,
        source_agent=session.source_agent,
        role=_required_string(view, "role"),
        timestamp=_required_string(view, "timestamp"),
        text=text,
        parts=_parts_summary(view),
    )


def _parts_summary(container: dict[str, Any]) -> tuple[ArchivePartSummary, ...]:
    if "parts_summary" not in container:
        return ()
    values = _required_list(container, "parts_summary")
    parts: list[ArchivePartSummary] = []
    for value in values:
        part = _required_mapping(value)
        kind = _required_string(part, "kind")
        if kind not in _SUMMARY_KINDS:
            raise ArchiveProtocolError()
        label = _optional_string(part, "label")
        call_id = _optional_string(part, "call_id")
        if kind in _SUMMARY_APPROVAL_KINDS and not label:
            raise ArchiveProtocolError()
        if call_id is not None and kind not in _SUMMARY_CALL_ID_KINDS:
            raise ArchiveProtocolError()
        parts.append(
            ArchivePartSummary(
                kind=kind,
                label=label,
                call_id=call_id,
            )
        )
    return tuple(parts)


def _required_mapping(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise ArchiveProtocolError()
    return value


def _required_value(container: dict[str, Any], key: str) -> object:
    if key not in container:
        raise ArchiveProtocolError()
    return container[key]


def _required_list(container: dict[str, Any], key: str) -> list[object]:
    value = _required_value(container, key)
    if type(value) is not list:
        raise ArchiveProtocolError()
    return value


def _required_string(
    container: dict[str, Any], key: str, *, identifier: bool = False
) -> str:
    value = _required_value(container, key)
    if type(value) is not str or (identifier and not value):
        raise ArchiveProtocolError()
    return value


def _optional_string(container: dict[str, Any], key: str) -> str | None:
    if key not in container:
        return None
    value = container[key]
    if type(value) is not str:
        raise ArchiveProtocolError()
    return value


def _required_integer(container: dict[str, Any], key: str) -> int:
    value = _required_value(container, key)
    if type(value) is not int or value < 0:
        raise ArchiveProtocolError()
    return value


def _required_number(container: dict[str, Any], key: str) -> float:
    value = _required_value(container, key)
    if type(value) not in (int, float):
        raise ArchiveProtocolError()
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ArchiveProtocolError() from None
    if not math.isfinite(number):
        raise ArchiveProtocolError()
    return number


def _required_boolean(container: dict[str, Any], key: str) -> bool:
    value = _required_value(container, key)
    if type(value) is not bool:
        raise ArchiveProtocolError()
    return value


def _zero_retry_adapter() -> HTTPAdapter:
    return HTTPAdapter(
        max_retries=Retry(
            total=0,
            connect=0,
            read=0,
            redirect=0,
            status=0,
        )
    )
