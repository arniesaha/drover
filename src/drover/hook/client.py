"""Tiny MCP-over-streamable-HTTP client tailored for the lifecycle hook.

The hook needs one synchronous, time-bounded ``call_tool``. We don't
keep a session open across calls — each invocation opens a fresh
session, calls one tool, and closes. Total budget defaults to 2 s per
spec §3.1.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger("drover.hook.client")


class HookTimeout(TimeoutError):
    """The MCP call exceeded the configured budget."""


async def _call_async(mcp_url: str, tool: str, args: dict) -> dict:
    async with streamablehttp_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
    if getattr(result, "isError", False):
        # MCP wraps tool errors in result.content; surface them.
        msg = "; ".join(getattr(c, "text", str(c)) for c in (result.content or []))
        raise RuntimeError(f"MCP tool {tool!r} returned error: {msg}")

    # Prefer structuredContent (object); fall back to first text content as JSON.
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        # FastMCP wraps non-object returns under "result"; unwrap.
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc

    for block in result.content or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
    return {}


def call_tool(
    *,
    mcp_url: str,
    tool: str,
    args: dict,
    timeout_s: float = 2.0,
) -> dict:
    """Call one MCP tool synchronously with a hard timeout."""
    try:
        return asyncio.run(
            asyncio.wait_for(_call_async(mcp_url, tool, args), timeout=timeout_s)
        )
    except asyncio.TimeoutError as e:
        raise HookTimeout(f"MCP {tool} exceeded {timeout_s}s budget") from e
