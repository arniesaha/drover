"""Retry tooling for failed ``summarize_session`` ledger jobs (#480).

By default this only requeues errors that are plausibly runtime/transient
(auth, rate-limit, backend availability) and deliberately skips schema/model
validation failures such as missing JSON keys unless explicitly requested.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from drover.server.ledger import (
    DEAD_LETTERED,
    FAILED_STATUSES,
    QUARANTINED,
    SUMMARIZE_SESSION,
    JobLedger,
)

#: How many failed rows one requeue pass inspects. Several generations of
#: one session can each have failed; only the newest per session is a
#: candidate, so this is a scan bound, not the requeue bound (``limit``).
_SCAN_LIMIT = 10_000

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
    "claude-code readiness",
    "cli exited",
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
    store_path: str | Path,
    *,
    apply: bool = False,
    include_validation: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Requeue failed ``summarize_session`` ledger jobs (operator escape hatch).

    ``store_path`` is the control-store registration path (the configured
    ``duckdb_path``). ``apply=False`` is a dry run and returns the sessions
    that would be requeued.

    Candidates are each session's newest summarize job, and only when it is
    failed: ``dead_lettered`` when its error classifies as retryable (auth,
    rate limit, runtime, repeated lease expiry), and ``quarantined`` --
    validation failures and
    sessions with no events -- only with ``include_validation``. A session
    whose newest job is live or succeeded is left alone, so a requeue can
    never replace newer work with an older source generation.

    Applying opens a fresh job for the same source version with ``force``:
    a new attempt budget, past the "this version already failed" check and
    the dead-letter streak cap. The streak is otherwise terminal -- once a
    session has burned its generations nothing re-enqueues it, so fixing the
    backend would never bring it back. The failed rows stay as history.

    Raises :class:`~drover.server.ledger.MemoryStoreUnavailable` when the
    control store is not PostgreSQL (there is no ledger to requeue).
    """
    ledger = JobLedger(store_path)
    newest_failed: dict[str, Any] = {}
    for row in ledger.jobs(
        SUMMARIZE_SESSION, statuses=FAILED_STATUSES, limit=_SCAN_LIMIT
    ):
        # Rows arrive newest first; keep the newest failure per session.
        newest_failed.setdefault(row.subject_key, row)

    matched = []
    for row in sorted(
        newest_failed.values(), key=lambda r: (r.enqueued_at, r.subject_key)
    ):
        if row.status == QUARANTINED:
            if not include_validation:
                continue
        elif row.status == DEAD_LETTERED:
            # A lease that kept expiring is a worker crash or timeout, which
            # is runtime trouble whatever the message says.
            if row.error_category != "lease_expired" and not classify_retryable_error(
                row.last_error, include_validation=include_validation
            ):
                continue
        latest = ledger.latest(SUMMARIZE_SESSION, row.subject_key)
        if latest is None or latest.job_id != row.job_id:
            continue
        matched.append(row)
    if limit is not None:
        matched = matched[: max(0, int(limit))]

    updated: list[str] = []
    if apply:
        for row in matched:
            outcome = ledger.enqueue(
                SUMMARIZE_SESSION,
                row.subject_key,
                source_version=row.source_version,
                force=True,
            )
            if outcome in ("queued", "requeued"):
                updated.append(row.subject_key)
    return {
        "dry_run": not apply,
        "include_validation": include_validation,
        "matched": [row.subject_key for row in matched],
        "updated": updated,
        "count": len(matched),
    }
