"""Retry tooling for failed summarize_jobs.

By default this only requeues errors that are plausibly runtime/transient
(auth, rate-limit, backend availability) and deliberately skips schema/model
validation failures such as missing JSON keys unless explicitly requested.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from drover.server.db import open_duckdb_connection

_AUTH_PATTERNS = (
    "401",
    "unauthorized",
    "invalid authentication",
    "invalid x-api-key",
    "authentication credentials",
    "stale anthropic credentials",
    "no credentials",
    "no api key",
    "no_api_key",
    "anthropic_api_key not configured",
    "anthropic_oauth_token",
)

_RUNTIME_PATTERNS = (
    "out of memory error",
    "failed to allocate data",
    "could not allocate block",
    "memory limit",
    "duckdb",
    "no backend configured",
    "backend selection failed",
    "connectionerror",
    "connection error",
    "connection refused",
    "failed to establish a new connection",
    "no route to host",
    "wol relay",
    "relay unreachable",
    "ollama",
    "timeout",
    "temporarily unavailable",
    "503",
    "502",
    "500",
    "empty response field",
)

_RATE_LIMIT_PATTERNS = (
    "429",
    "rate_limit",
    "rate limit",
    "too many requests",
    "overloaded",
)

_VALIDATION_PATTERNS = (
    "missing required keys",
    "must be a string",
    "must be a list",
    "invalid json",
    "json response",
    "failed to parse json",
    "not json",
    "schema",
)


def classify_retryable_error(
    message: str | None, *, include_validation: bool = False
) -> bool:
    """Return whether an errored summarize job is safe to requeue."""
    return bool(
        classify_summarize_error(message, include_validation=include_validation)[
            "retryable"
        ]
    )


def classify_summarize_error(
    message: str | None, *, include_validation: bool = False
) -> dict[str, Any]:
    """Classify a summarize job failure without exposing raw error details."""
    text = (message or "").lower()
    if not text:
        return {"category": "unknown", "retryable": False}
    if any(p in text for p in _VALIDATION_PATTERNS):
        return {"category": "validation", "retryable": bool(include_validation)}
    if any(p in text for p in _RATE_LIMIT_PATTERNS):
        return {"category": "rate_limit", "retryable": True}
    if any(p in text for p in _AUTH_PATTERNS):
        return {"category": "auth", "retryable": True}
    if any(p in text for p in _RUNTIME_PATTERNS):
        return {"category": "runtime", "retryable": True}
    return {"category": "unknown", "retryable": False}


def retry_errored_jobs(
    duckdb_path: str | Path,
    *,
    apply: bool = False,
    include_validation: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Reset matching errored summarize_jobs to pending.

    ``apply=False`` is a dry run and returns the jobs that would be reset.
    ``attempts`` is left intact so operators keep failure history.
    """
    con = open_duckdb_connection(duckdb_path)
    try:
        rows = con.execute(
            """SELECT session_id, last_error
               FROM summarize_jobs
               WHERE status='errored'
               ORDER BY updated_at NULLS LAST, enqueued_at ASC, session_id ASC"""
        ).fetchall()
        matched = [
            (sid, err)
            for sid, err in rows
            if classify_retryable_error(err, include_validation=include_validation)
        ]
        if limit is not None:
            matched = matched[: max(0, int(limit))]
        if apply and matched:
            con.executemany(
                """UPDATE summarize_jobs
                   SET status='pending', updated_at=now()
                   WHERE session_id=? AND status='errored'""",
                [(sid,) for sid, _ in matched],
            )
            updated = [sid for sid, _ in matched]
        else:
            updated = []
        return {
            "dry_run": not apply,
            "include_validation": include_validation,
            "matched": [sid for sid, _ in matched],
            "updated": updated,
            "count": len(matched),
        }
    finally:
        con.close()
