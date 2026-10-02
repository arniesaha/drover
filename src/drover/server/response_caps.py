"""Byte budgets for bounded HTTP list responses.

A local stand-in for the MCP read caps on the unmerged ``mcp/caps-and-freshness``
branch (``drover.server.mcp.contract``). It counts bytes the same way --
``json.dumps(..., ensure_ascii=True)`` so escaped Unicode is paid for -- and
never emits partial JSON. Pages differ from MCP reads in one respect: dropping
an arbitrary field would break a cursor, so a page sheds whole trailing rows and
the caller resumes from the last row it actually sent. Fold this into the MCP
helper once that branch lands.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResponseCaps:
    rows: int
    response_bytes: int = 65536


def serialized_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def bound_text(value: str | None, max_chars: int) -> str | None:
    """Trim to ``max_chars`` at a word boundary when one is close, with an ellipsis."""
    if value is None:
        return None
    if len(value) <= max_chars:
        return value
    cut = value[: max_chars - 1]
    space = cut.rfind(" ")
    if space >= max_chars * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


def fit_page(
    envelope: Callable[[list[Any], bool], dict[str, Any]],
    items: Sequence[Any],
    caps: ResponseCaps,
) -> tuple[dict[str, Any], int]:
    """Largest prefix of ``items`` whose envelope fits ``caps.response_bytes``.

    ``envelope(prefix, truncated)`` builds the full response for a prefix.
    Returns the response and how many items it carries. Size grows
    monotonically with the prefix, so a binary search needs O(log n) renders.
    """
    items = list(items[: caps.rows])
    full = envelope(items, False)
    if serialized_bytes(full) <= caps.response_bytes:
        return full, len(items)
    lo, hi = 0, len(items) - 1  # the largest fitting prefix is in [lo, hi]
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if serialized_bytes(envelope(items[:mid], True)) <= caps.response_bytes:
            lo = mid
        else:
            hi = mid - 1
    body = envelope(items[:lo], True)
    if serialized_bytes(body) > caps.response_bytes:
        raise ValueError("response metadata exceeds its byte budget")
    return body, lo
