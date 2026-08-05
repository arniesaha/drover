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
    return {
        "kind": "req",
        "id": request_id,
        "method": method,
        "path": path,
        "body": body,
    }


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
