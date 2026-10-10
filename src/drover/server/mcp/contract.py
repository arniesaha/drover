"""Storage-independent bounds for MCP reads, including serialized metadata.

Deadlines bound caller latency, not execution: up to four admitted daemon threads
may finish after timeout. Disposable query cancellation belongs to Phase 4.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import threading
from contextvars import copy_context
from dataclasses import dataclass

from drover.server.mcp.freshness import with_freshness


@dataclass(frozen=True)
class ReadCaps:
    rows: int = 100
    text_bytes: int = 4096
    response_bytes: int = 65536
    deadline_seconds: float = 5.0


READ_CAPS = {
    f"drover_{name}": ReadCaps(rows=rows)
    for name, rows in {
        "profile": 100,
        "memory_acceptance": 25,
        "handoff": 20,
        "session_replay": 100,
        "session_summary": 100,
        "active_sessions": 100,
        "search": 100,
        "recall_bundle": 20,
        "files_touched": 100,
        "project_brief": 100,
        "recent_sessions": 20,
        "recent_contexts": 100,
        "context_brief": 100,
        "open_loops": 100,
        "resume_context": 20,
        "recall": 20,
        "task_status": 100,
        "project_activity": 200,
        "active_handoff": 100,
        "fleet_status": 100,
        "data_quality": 100,
        "pipeline_observatory": 20,
        "provider_quota": 50,
    }.items()
}
_LIMIT_ARGS = {
    "limit",
    "max_summaries",
    "last_n_turns",
    "max_artifacts",
    "max_projects",
}


def bounded_arguments(arguments, caps):
    arguments = dict(arguments)
    truncated = False
    for key in _LIMIT_ARGS & arguments.keys():
        value = arguments[key]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{key} must be a positive integer")
        arguments[key] = min(value, caps.rows)
        truncated |= value > caps.rows
    if "harness_ids" in arguments and len(arguments["harness_ids"]) > caps.rows:
        raise ValueError(f"at most {caps.rows} harness IDs per audit read")
    return arguments, truncated


def serialized_bytes(value):
    # Default JSON escaping is deliberately counted: MCP may escape Unicode.
    return len(json.dumps(value, ensure_ascii=True, indent=2).encode("utf-8"))


def bound_response(value, caps, *, truncated=False):
    if value is None:
        return None

    def bound(item):
        nonlocal truncated
        if isinstance(item, str):
            encoded = item.encode("utf-8")
            if len(encoded) > caps.text_bytes:
                truncated = True
                return encoded[: caps.text_bytes].decode("utf-8", errors="ignore")
        elif isinstance(item, list):
            truncated |= len(item) > caps.rows
            return [bound(v) for v in item[: caps.rows]]
        elif isinstance(item, dict):
            # Keys and scalar metadata count too. Preserve normal response keys.
            return {bound(str(k)): bound(v) for k, v in item.items()}
        return item

    result = bound(value)
    existing = result.get("truncated")
    if isinstance(existing, dict):
        result["truncation_details"] = existing
        existing = any(existing.values())
    result["truncated"] = truncated or bool(existing)
    # Shrink the largest remaining field. Never emit partial/invalid JSON.
    protected = {
        "truncated",
        "status",
        "store",
        "store_authoritative",
        "host",
        "data_watermark",
        "state_source",
    }
    while serialized_bytes(result) > caps.response_bytes:
        candidates = []

        def visit(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    if key in protected:
                        continue
                    candidates.append((serialized_bytes(child), item, key, child))
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)

        visit(result)
        if not candidates:
            raise ValueError("response metadata exceeds MCP byte budget")
        _, parent, key, child = max(candidates, key=lambda c: c[0])
        if isinstance(child, list) and child:
            parent[key] = child[: len(child) // 2]
        elif isinstance(child, str) and child:
            parent[key] = child[: len(child) // 2]
        else:
            del parent[key]
        result["truncated"] = True
    return result


def bounded_read(fn):
    caps = READ_CAPS[fn.__name__]
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs)
        arguments.apply_defaults()
        bounded, truncated = bounded_arguments(arguments.arguments, caps)
        return bound_response(
            with_freshness(
                fn(**bounded),
                path=bounded.get("duckdb_path"),
                allow_store_watermark=fn.__name__ != "drover_profile",
            ),
            caps,
            truncated=truncated,
        )

    return wrapped


class ReadAdmission:
    def __init__(self, concurrency=4, path=None):
        self.path = path
        self.slots = threading.BoundedSemaphore(concurrency)

    def wrap(self, fn):
        stamp = functools.partial(
            with_freshness,
            path=self.path,
            allow_store_watermark=fn.__name__ != "drover_profile",
        )
        caps = READ_CAPS[fn.__name__]

        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            if not self.slots.acquire(blocking=False):
                return stamp(
                    {
                        "status": "busy",
                        "reason": "MCP read admission full",
                        "truncated": False,
                    }
                )
            loop = asyncio.get_running_loop()
            future = loop.create_future()

            def deliver(value, error):
                if not future.done():
                    if error is not None:
                        future.set_exception(error)
                    else:
                        future.set_result(value)

            def run():
                value, error = None, None
                try:
                    value = bound_response(
                        stamp(bounded_read(fn)(*args, **kwargs), empty_envelope=True),
                        caps,
                    )
                except Exception as exc:
                    value = bound_response(
                        stamp(
                            {
                                "status": "error",
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                            }
                        ),
                        caps,
                    )
                finally:
                    self.slots.release()
                try:
                    loop.call_soon_threadsafe(deliver, value, error)
                except RuntimeError:
                    pass  # Caller's loop closed after timeout.

            # Preserve the verified request identity in the read worker.
            context = copy_context()
            threading.Thread(
                target=context.run, args=(run,), daemon=True, name="mcp-read"
            ).start()
            try:
                return await asyncio.wait_for(future, caps.deadline_seconds)
            except asyncio.TimeoutError:
                return stamp(
                    {
                        "status": "timeout",
                        "deadline_seconds": caps.deadline_seconds,
                        "truncated": False,
                    }
                )

        return wrapped
