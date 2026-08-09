"""Frame vocabulary for the hub<->harnessd relay websocket.

One socket carries two families: request/response (hub-initiated API
calls, correlated by ``id``) and channels (terminal attach streams,
correlated by ``chan``). Terminal messages are JSON dicts already, so
``data`` frames carry them verbatim under ``message``.
"""

from __future__ import annotations

from typing import Any

FRAMED_RESPONSES_CAPABILITY = "framed_responses_v1"
RELAY_CONTROL_FRAME_BYTES = 64 * 1024

FRAME_KINDS = frozenset(
    {
        "hello",
        "req",
        "res",
        "res_start",
        "open",
        "opened",
        "open_error",
        "data",
        "close",
    }
)


class RelayProtocolError(ValueError):
    """A frame that does not conform to the relay vocabulary."""


def hello_frame(
    host_id: str, *, capabilities: list[str] | None = None
) -> dict[str, Any]:
    frame: dict[str, Any] = {"kind": "hello", "host_id": host_id}
    if capabilities is not None:
        if any(not isinstance(item, str) or not item for item in capabilities):
            raise ValueError("relay capabilities must be non-empty strings")
        frame["capabilities"] = capabilities
    return frame


def req_frame(
    request_id: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    *,
    max_response_bytes: int | None = None,
    response_framing: str | None = None,
) -> dict[str, Any]:
    frame = {
        "kind": "req",
        "id": request_id,
        "method": method,
        "path": path,
        "body": body,
    }
    if max_response_bytes is not None:
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        frame["max_response_bytes"] = max_response_bytes
    if response_framing is not None:
        if response_framing != FRAMED_RESPONSES_CAPABILITY:
            raise ValueError("unsupported relay response framing")
        frame["response_framing"] = response_framing
    return frame


def res_frame(request_id: str, status: int, body: str) -> dict[str, Any]:
    return {"kind": "res", "id": request_id, "status": status, "body": body}


def res_start_frame(request_id: str, status: int, body_bytes: int) -> dict[str, Any]:
    if type(body_bytes) is not int or body_bytes < 0:
        raise ValueError("body_bytes must be a non-negative integer")
    return {
        "kind": "res_start",
        "id": request_id,
        "status": status,
        "body_bytes": body_bytes,
    }


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
