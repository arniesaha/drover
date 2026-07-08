"""Tiny client for Drover's streamable HTTP MCP endpoint.

This is intentionally small and dependency-light. It gives local agents a
first-party fallback when their harness has not mounted the Drover MCP tools as
native callable tools.
"""

from __future__ import annotations

import json
from typing import Any

import requests


class DroverMCPClientError(RuntimeError):
    """Raised when the MCP endpoint returns an invalid or error response."""


# Transition alias per docs/porting-and-cutover.md §7.6.
NexusMCPClientError = DroverMCPClientError


def _endpoint(url: str) -> str:
    return url.rstrip("/")


def _decode_message(response: requests.Response) -> dict[str, Any]:
    if not response.ok:
        raise DroverMCPClientError(
            f"MCP HTTP {response.status_code}: {response.text[:500]}"
        )

    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        try:
            payload = response.json()
        except ValueError as exc:
            raise DroverMCPClientError(
                f"MCP response was not JSON or SSE: {response.text[:500]}"
            ) from exc
        if isinstance(payload, dict):
            return payload
        raise DroverMCPClientError("MCP response JSON was not an object")

    messages: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for raw_line in response.text.splitlines():
        line = raw_line.rstrip("\r")
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line == "" and data_lines:
            data = "\n".join(data_lines)
            data_lines = []
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError as exc:
                raise DroverMCPClientError(
                    f"Invalid MCP SSE JSON: {data[:500]}"
                ) from exc
            if isinstance(parsed, dict):
                messages.append(parsed)
    if data_lines:
        data = "\n".join(data_lines)
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as exc:
            raise DroverMCPClientError(f"Invalid MCP SSE JSON: {data[:500]}") from exc
        if isinstance(parsed, dict):
            messages.append(parsed)

    if not messages:
        raise DroverMCPClientError("MCP SSE response contained no JSON messages")
    return messages[-1]


def _raise_jsonrpc_error(message: dict[str, Any]) -> None:
    if "error" in message:
        error = message["error"]
        if isinstance(error, dict):
            raise DroverMCPClientError(
                f"MCP error {error.get('code')}: {error.get('message')}"
            )
        raise DroverMCPClientError(f"MCP error: {error}")


def _post(
    *,
    url: str,
    payload: dict[str, Any],
    timeout: float,
    session_id: str | None = None,
) -> requests.Response:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return requests.post(
        _endpoint(url),
        headers=headers,
        json=payload,
        timeout=timeout,
        allow_redirects=True,
    )


def _session(url: str, *, timeout: float) -> str:
    response = _post(
        url=url,
        timeout=timeout,
        payload={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "drover-server-cli", "version": "0"},
            },
        },
    )
    message = _decode_message(response)
    _raise_jsonrpc_error(message)
    session_id = response.headers.get("mcp-session-id")
    if not session_id:
        raise DroverMCPClientError("MCP initialize response did not include session id")

    # Some FastMCP versions accept tool calls immediately after initialize, but
    # sending the initialized notification keeps this client protocol-correct.
    _post(
        url=url,
        timeout=timeout,
        session_id=session_id,
        payload={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    return session_id


def list_tools(url: str, *, timeout: float = 10) -> list[dict[str, Any]]:
    """Return tool metadata from a streamable HTTP MCP endpoint."""
    session_id = _session(url, timeout=timeout)
    response = _post(
        url=url,
        timeout=timeout,
        session_id=session_id,
        payload={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    message = _decode_message(response)
    _raise_jsonrpc_error(message)
    result = message.get("result") or {}
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        raise DroverMCPClientError("MCP tools/list response did not include tools")
    return tools


def call_tool(
    url: str,
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout: float = 30,
) -> dict[str, Any]:
    """Call a Drover MCP tool over streamable HTTP."""
    session_id = _session(url, timeout=timeout)
    response = _post(
        url=url,
        timeout=timeout,
        session_id=session_id,
        payload={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    message = _decode_message(response)
    _raise_jsonrpc_error(message)
    result = message.get("result")
    if not isinstance(result, dict):
        raise DroverMCPClientError("MCP tools/call response did not include a result")
    return result
