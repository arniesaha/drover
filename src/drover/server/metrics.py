"""Prometheus metrics surface for the Drover runtime."""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import socket
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence
from urllib.parse import quote, urlencode, urlparse

from drover.config import FavoriteCwd
from drover.server.db import (
    control_plane_connection,
    control_plane_path,
    copy_duckdb_store,
    open_duckdb_connection,
    snapshot_scratch_root,
)
from drover.server.harness.daemon import (
    _STRUCTURED_DEFAULT_COMMANDS,
    native_transcript_for_session,
)
from drover.server.harness.model_catalog import (
    MAX_CATALOG_WIRE_BYTES,
    CatalogEnvelope,
    catalog_wire_bytes,
)
from drover.server.harness.model_catalog.models import MAX_ID_LENGTH
from drover.server.harness.models import HARNESS_STALE_AFTER_SECONDS
from drover.server.harness.recap_jobs import LiveRecap
from drover.server.harness.recap_prompt import drop_user_subject
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.schema import (
    audit_legacy_harness_event_sequences,
    migrate_legacy_harness_event_sequences,
)
from drover.server.jobs import RedisJobStream
from drover.server.observatory import pipeline_observatory_snapshot
from drover.server.quality import format_prometheus, quality_snapshot
from drover.server.readiness import ReadinessProbe

if TYPE_CHECKING:
    from drover.server.advisory.service import InsightFilters, InsightsService
    from drover.server.cockpit.service import CockpitService
    from drover.server.harness.models import HarnessHost
    from drover.server.relay_manager import RelayManager

log = logging.getLogger("drover.metrics")

#: Ceiling on how many archived sessions a caller may ask a fleet render for.
#: Above this the payload stops being a fleet view and starts being a history
#: export, which belongs on a paged endpoint rather than the 5s poll.
MAX_ARCHIVED_SESSION_LIMIT = 100

#: Distinguishes "caller did not specify" from an explicit ``None`` (meaning
#: no cap at all), so the collector default can apply only to the former.
_UNSET_ARCHIVED_LIMIT: Any = object()


# Floor on any hub->harnessd budget that rides a relay connection.
#
# The tightest budgets in the system (1.0s reconcile, 2.0s native transcript)
# were chosen for a LAN dial, where they are generous. Over a funnel from
# cellular they are not budgets at all: they expire while the spoke's loopback
# call is still running, the hub 502s, and -- because the transcript endpoint
# is polled -- each expiry leaves another orphaned thread on the laptop.
#
# 5s rather than more: this floor stacks with RelayManager.open_channel's own
# 10s on the terminal-attach path (reconcile, then open), so it has to stay
# small enough that the worst case is still a wait a user will sit through.
# Presence is now trustworthy within a minute, so a live relay socket is
# decent evidence the host is really there and worth waiting for.
RELAY_MIN_TIMEOUT_S = 5.0

# How long the hub waits for a host to create a session.
#
# Every other hub->harnessd call reads state and answers in milliseconds. A
# create is the one that does work: it cuts a per-session worktree, starts a
# driver, and delivers the first turn before it can reply. Measured on the hub
# those cost 0.33s, and 3.87s for the whole create-with-prompt, so the 15s
# default is normally ample -- but a create that overruns it leaves the daemon
# writing its reply into a closed socket, and the session it made is then
# known only to the host.
#
# Deliberately generous rather than tuned: the cost of waiting is a slow
# handoff, and the cost of not waiting is a session the hub cannot see.
CREATE_SESSION_TIMEOUT_S = 120.0
# How long creating a session waits for a freshly attached spoke to prove it is
# reading its socket. Only ever spent on a connection that has not yet sent a
# frame; the hub pings on attach, so a working spoke costs a round trip and a
# reconnect does not become a spurious refusal.
RELAY_PROOF_TIMEOUT_S = 2.0

# How long the hub waits for a host to complete a typed path.
#
# The opposite trade to a create: this fires on every keystroke in the working
# directory field, and the reply is a single `os.scandir` on the host. Waiting
# is worthless here -- a completion that arrives after the next keystroke is
# already stale -- so an unreachable or wedged host must fail fast enough that
# the field can show an inline hint and the user can keep typing. Explicitly
# not CREATE_SESSION_TIMEOUT_S: 120s of held connection per keystroke is how a
# suggestion list turns into an outage.
FS_COMPLETE_TIMEOUT_S = 3.0
_MAX_CONTENT_BUNDLE_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_CONTENT_VERSION_RESPONSE_BYTES = 256 * 1024

# Harnesses harnessd can drive as structured sessions (claude-code, codex,
# agy). A nexus handoff to one of these launches mode="structured" and
# delivers the handoff text as the first turn -- strictly more reliable than
# typing it into a cold PTY (no startup-gate race).
_STRUCTURED_HANDOFF_HARNESSES = frozenset(_STRUCTURED_DEFAULT_COMMANDS)
_RECOVERABLE_STRUCTURED_HARNESSES = frozenset(
    {"claude-code", "codex", "deepseek-harness"}
)
_RECOVERY_UNAVAILABLE = (
    "Session cannot be resumed after the harness restart. "
    "Continue it in a new session."
)

_SUMMARIZE_JOB_STATUSES = (
    "pending",
    "running",
    "retry_wait",
    "done",
    "errored",
    "dead_lettered",
)


def _valid_advisory_target_ids(value: Any) -> bool:
    if not isinstance(value, list) or not value or len(value) > 256:
        return False
    if any(
        not isinstance(item, str)
        or not item
        or len(item) > 256
        or item.strip() != item
        or item in {".", ".."}
        or "/" in item
        or "\\" in item
        for item in value
    ):
        return False
    return len(set(value)) == len(value)


def _validate_advisory_content_bundle(
    payload: Any, *, requested_ids: list[str]
) -> None:
    if not isinstance(payload, dict) or set(payload) != {
        "bundle_hash",
        "created_at",
        "targets",
    }:
        raise ValueError("content bundle response has invalid fields")
    if not _is_sha256(payload["bundle_hash"]):
        raise ValueError("content bundle response has invalid bundle_hash")
    try:
        created_at = datetime.fromisoformat(payload["created_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("content bundle response has invalid created_at") from exc
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("content bundle response has invalid created_at")
    targets = payload["targets"]
    if not isinstance(targets, list) or len(targets) != len(requested_ids):
        raise ValueError("content bundle response has invalid targets")
    returned_ids: list[str] = []
    hash_pairs: list[tuple[str, str]] = []
    for target in targets:
        if not isinstance(target, dict) or set(target) != {
            "target_id",
            "content_hash",
            "redacted_content",
        }:
            raise ValueError("content bundle response has invalid target fields")
        if not isinstance(target["target_id"], str):
            raise ValueError("content bundle response has invalid target ID")
        if not _is_sha256(target["content_hash"]):
            raise ValueError("content bundle response has invalid content_hash")
        if not isinstance(target["redacted_content"], str):
            raise ValueError("content bundle response has invalid redacted content")
        computed_content_hash = hashlib.sha256(
            target["redacted_content"].encode("utf-8")
        ).hexdigest()
        if target["content_hash"] != computed_content_hash:
            raise ValueError("content bundle response content_hash does not match")
        returned_ids.append(target["target_id"])
        hash_pairs.append((target["target_id"], target["content_hash"]))
    if returned_ids != requested_ids:
        raise ValueError("content bundle response target IDs do not match request")
    computed_bundle_hash = hashlib.sha256(
        json.dumps(
            hash_pairs,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if payload["bundle_hash"] != computed_bundle_hash:
        raise ValueError("content bundle response bundle_hash does not match")


def _validate_advisory_content_version(
    payload: Any, *, requested_ids: list[str]
) -> None:
    if not isinstance(payload, dict) or set(payload) != {"bundle_hash", "targets"}:
        raise ValueError("content version response has invalid fields")
    if not _is_sha256(payload["bundle_hash"]):
        raise ValueError("content version response has invalid bundle_hash")
    targets = payload["targets"]
    if not isinstance(targets, list) or len(targets) != len(requested_ids):
        raise ValueError("content version response has invalid targets")
    returned_ids: list[str] = []
    hash_pairs: list[tuple[str, str]] = []
    for target in targets:
        if not isinstance(target, dict) or set(target) != {
            "target_id",
            "content_hash",
        }:
            raise ValueError("content version response has invalid target fields")
        if not isinstance(target["target_id"], str):
            raise ValueError("content version response has invalid target ID")
        if not _is_sha256(target["content_hash"]):
            raise ValueError("content version response has invalid content_hash")
        returned_ids.append(target["target_id"])
        hash_pairs.append((target["target_id"], target["content_hash"]))
    if returned_ids != requested_ids:
        raise ValueError("content version response target IDs do not match request")
    computed_bundle_hash = hashlib.sha256(
        json.dumps(
            hash_pairs,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if payload["bundle_hash"] != computed_bundle_hash:
        raise ValueError("content version response bundle_hash does not match")


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _read_bounded_http_body(response: Any, *, max_response_bytes: int | None) -> str:
    if max_response_bytes is None:
        return response.read().decode("utf-8")
    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")
    content_length = response.getheader("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError as exc:
            raise ValueError("harness response has invalid Content-Length") from exc
        if declared_bytes < 0:
            raise ValueError("harness response has invalid Content-Length")
        if declared_bytes > max_response_bytes:
            raise ValueError("harness response exceeds byte limit")
    body = response.read(max_response_bytes + 1)
    if len(body) > max_response_bytes:
        raise ValueError("harness response exceeds byte limit")
    return body.decode("utf-8")


def _label_value(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**labels: object) -> str:
    if not labels:
        return ""
    rendered = ",".join(
        f'{key}="{_label_value(value)}"' for key, value in labels.items()
    )
    return f"{{{rendered}}}"


def _metric(name: str, value: object, **labels: object) -> str:
    if isinstance(value, bool):
        metric_value = 1 if value else 0
    elif value is None:
        metric_value = 0
    else:
        metric_value = value
    return f"{name}{_labels(**labels)} {metric_value}"


def _append_details_metrics(lines: list[str], snapshot: dict) -> None:
    summary = (
        snapshot.get("categories", {}).get("summary_coverage", {}).get("details", {})
    )
    embedding = (
        snapshot.get("categories", {}).get("embedding_coverage", {}).get("details", {})
    )
    bundle = snapshot.get("categories", {}).get("bundle_quality", {}).get("details", {})
    span_linkability = (
        snapshot.get("categories", {}).get("span_linkability", {}).get("details", {})
    )
    span_coverage = embedding.get("span_embedding_coverage", {})

    lines.extend(
        [
            "# HELP drover_summary_coverage_percent Percent of event sessions with generated summaries.",
            "# TYPE drover_summary_coverage_percent gauge",
            _metric(
                "drover_summary_coverage_percent",
                summary.get("coverage_percent"),
            ),
            "# HELP drover_event_sessions_without_summary Event sessions missing summaries.",
            "# TYPE drover_event_sessions_without_summary gauge",
            _metric(
                "drover_event_sessions_without_summary",
                summary.get("event_sessions_without_summary"),
            ),
            "# HELP drover_session_embedding_coverage_percent Percent of summaries with session embeddings.",
            "# TYPE drover_session_embedding_coverage_percent gauge",
            _metric(
                "drover_session_embedding_coverage_percent",
                embedding.get("session_embedding_coverage_percent"),
            ),
            "# HELP drover_span_embedding_coverage_percent Percent of spans with semantic embeddings.",
            "# TYPE drover_span_embedding_coverage_percent gauge",
            _metric(
                "drover_span_embedding_coverage_percent",
                span_coverage.get("coverage_percent"),
            ),
            "# HELP drover_bundle_ready_percent Percent of generated summaries ready for handoff bundles.",
            "# TYPE drover_bundle_ready_percent gauge",
            _metric(
                "drover_bundle_ready_percent",
                bundle.get("bundle_ready_percent"),
            ),
            "# HELP drover_openclaw_unmatched_spans OpenClaw/AgentWeave-like spans not linked to native sessions.",
            "# TYPE drover_openclaw_unmatched_spans gauge",
            _metric(
                "drover_openclaw_unmatched_spans",
                span_linkability.get("unmatched_spans", 0),
            ),
        ]
    )


def sequence_health_report(db_path: Path, *, apply: bool = False) -> dict[str, int]:
    """Return aggregate legacy-sequence health, optionally applying migration.

    Dry-runs reuse the migration's read-only classification helper. This
    preserves its exact eligibility rules without writing the source database
    or selecting event content.

    ``harness_events`` moved to the control-plane store in #95, so ``db_path``
    is resolved there and read through the control plane's own connection.
    ``/metrics`` calls this in the server process; opening that file with an
    analytical role would reset the control-plane instance's ``memory_limit``
    and ``threads``, which is precisely the coupling the split removes. The
    connection is read-write either way now -- only the ``apply`` branch
    writes, as before.
    """
    source = control_plane_path(db_path)
    if not source.exists():
        return {
            "null_event_count": 0,
            "all_null_sessions": 0,
            "mixed_sessions": 0,
        }

    with control_plane_connection(source) as con:
        audit = audit_legacy_harness_event_sequences(con)
        if apply:
            report = migrate_legacy_harness_event_sequences(con)
            all_null_sessions = report.migrated_sessions
            mixed_sessions = len(report.mixed_sessions)
        else:
            all_null_sessions = len(audit.all_null_sessions)
            mixed_sessions = len(audit.mixed_sessions)
    return {
        "null_event_count": audit.null_event_count,
        "all_null_sessions": all_null_sessions,
        "mixed_sessions": mixed_sessions,
    }


def _append_operational_health_metrics(
    lines: list[str], db_path: Path, snapshot: Mapping[str, Any]
) -> None:
    summary = (
        snapshot.get("categories", {}).get("summary_coverage", {}).get("details", {})
    )
    statuses = {status: 0 for status in _SUMMARIZE_JOB_STATUSES}
    statuses["pending"] = int(summary.get("pending_summarize_jobs", 0) or 0)
    statuses["errored"] = int(summary.get("errored_summarize_jobs", 0) or 0)
    max_attempts = 0
    oldest_retry_seconds = 0.0
    source = Path(db_path)
    if source.exists():
        con = open_duckdb_connection(source, read_only=True, role="diagnostic")
        try:
            rows = con.execute(
                "SELECT status, count(*) FROM summarize_jobs "
                "WHERE status IN (?, ?, ?, ?, ?, ?) GROUP BY status",
                list(_SUMMARIZE_JOB_STATUSES),
            ).fetchall()
            statuses.update({str(status): int(count) for status, count in rows})
            max_attempts, oldest_retry_seconds = con.execute("""
                SELECT
                    COALESCE(max(max_attempts), 0),
                    COALESCE(max(epoch((now() AT TIME ZONE 'UTC')
                        - COALESCE(updated_at, enqueued_at)))
                        FILTER (WHERE status = 'retry_wait'), 0)
                FROM summarize_jobs
                """).fetchone()
        finally:
            con.close()

    try:
        sequences = sequence_health_report(source)
    except Exception as exc:  # noqa: BLE001
        log.warning("failed to render sequence health metrics: %s", exc)
        sequences = {
            "null_event_count": 0,
            "all_null_sessions": 0,
            "mixed_sessions": 0,
        }
    lines.extend(
        [
            "# HELP drover_harness_legacy_unsequenced_events Harness events with no sequence number.",
            "# TYPE drover_harness_legacy_unsequenced_events gauge",
            _metric(
                "drover_harness_legacy_unsequenced_events",
                sequences["null_event_count"],
            ),
            "# HELP drover_harness_mixed_sequence_sessions Sessions mixing sequenced and unsequenced events.",
            "# TYPE drover_harness_mixed_sequence_sessions gauge",
            _metric(
                "drover_harness_mixed_sequence_sessions",
                sequences["mixed_sessions"],
            ),
            "# HELP drover_summarize_jobs Summarization jobs by bounded status.",
            "# TYPE drover_summarize_jobs gauge",
        ]
    )
    for status in _SUMMARIZE_JOB_STATUSES:
        lines.append(_metric("drover_summarize_jobs", statuses[status], status=status))
    lines.extend(
        [
            "# HELP drover_summarize_max_attempts Maximum configured summarize-job attempt ceiling.",
            "# TYPE drover_summarize_max_attempts gauge",
            _metric("drover_summarize_max_attempts", int(max_attempts or 0)),
            "# HELP drover_summarize_oldest_retry_seconds Age of the oldest waiting summarize retry.",
            "# TYPE drover_summarize_oldest_retry_seconds gauge",
            _metric(
                "drover_summarize_oldest_retry_seconds",
                max(float(oldest_retry_seconds or 0), 0.0),
            ),
        ]
    )


def _append_summarizer_metrics(lines: list[str], report: Mapping[str, Any]) -> None:
    policy = str(report.get("backend_policy") or "unknown")
    allows_anthropic = policy in {"hybrid", "cloud"}
    allows_harness = policy in {"harness", "hybrid"}
    lines.extend(
        [
            "# HELP drover_summarizer_policy Current summarizer backend policy.",
            "# TYPE drover_summarizer_policy gauge",
        ]
    )
    for candidate in ("harness", "hybrid", "cloud", "unknown"):
        lines.append(
            _metric(
                "drover_summarizer_policy",
                1 if candidate == policy else 0,
                policy=candidate,
            )
        )
    lines.extend(
        [
            "# HELP drover_summarizer_backend_ready Whether each summarizer backend is configured and usable.",
            "# TYPE drover_summarizer_backend_ready gauge",
            _metric(
                "drover_summarizer_backend_ready",
                bool(report.get("anthropic_ready")),
                backend="anthropic",
            ),
            _metric(
                "drover_summarizer_backend_ready",
                bool(report.get("harness_ready")),
                backend="claude-code",
            ),
            "# HELP drover_summarizer_backend_allowed Whether policy allows each summarizer backend.",
            "# TYPE drover_summarizer_backend_allowed gauge",
            _metric(
                "drover_summarizer_backend_allowed",
                allows_anthropic,
                backend="anthropic",
            ),
            _metric(
                "drover_summarizer_backend_allowed",
                allows_harness,
                backend="claude-code",
            ),
        ]
    )


def _append_harness_metrics(lines: list[str]) -> None:
    # Two distinct losses, deliberately counted apart. The first is a local
    # registry write that failed permanently; the second is an event this host
    # recorded fine but could never hand to the hub, which the first counter
    # can by definition never see (#99). Gaps are marked in-band with
    # transcript.gap events.
    from drover.server.harness.daemon import (
        dropped_event_count,
        undelivered_event_count,
    )

    lines.extend(
        [
            "# HELP drover_harness_dropped_events_total "
            "Harness events permanently lost after write retries.",
            "# TYPE drover_harness_dropped_events_total counter",
            f"drover_harness_dropped_events_total {dropped_event_count()}",
            "# HELP drover_harness_undelivered_events_total "
            "Harness events the pusher could never deliver to the hub.",
            "# TYPE drover_harness_undelivered_events_total counter",
            f"drover_harness_undelivered_events_total {undelivered_event_count()}",
        ]
    )


def _append_advisory_metrics(lines: list[str]) -> None:
    # #303: the control-plane window (the findings-commit phase of
    # AdvisoryWorker._execute / ContentAnalysisWorker._record_success) holds
    # control_plane_lock for its whole duration, and every other window on
    # the process queues behind it -- but nothing exported how long it
    # actually takes.
    from drover.server.advisory.worker import plane_window_stats

    last_seconds, max_seconds, windows_total = plane_window_stats()
    lines.extend(
        [
            "# HELP drover_advisory_plane_window_seconds "
            "Duration of the most recent advisory control-plane window.",
            "# TYPE drover_advisory_plane_window_seconds gauge",
            f"drover_advisory_plane_window_seconds {last_seconds}",
            "# HELP drover_advisory_plane_window_max_seconds "
            "Longest advisory control-plane window observed so far.",
            "# TYPE drover_advisory_plane_window_max_seconds gauge",
            f"drover_advisory_plane_window_max_seconds {max_seconds}",
            "# HELP drover_advisory_plane_windows_total "
            "Advisory control-plane windows completed, successful or not.",
            "# TYPE drover_advisory_plane_windows_total counter",
            f"drover_advisory_plane_windows_total {windows_total}",
        ]
    )


def _append_analytics_gate_metrics(lines: list[str]) -> None:
    # Without these, "the rollups stood aside" and "the rollups are broken"
    # look identical from outside the process (#331).
    from drover.server.analytics_maintenance import latest_maintenance_gate_stats

    stats = latest_maintenance_gate_stats()
    lines.extend(
        [
            "# HELP drover_analytics_foreground_requests "
            "Cockpit analytics builds currently in flight.",
            "# TYPE drover_analytics_foreground_requests gauge",
            f"drover_analytics_foreground_requests {stats.foreground_waiters}",
            "# HELP drover_analytics_maintenance_active "
            "Whether a background analytical pass holds the maintenance slot.",
            "# TYPE drover_analytics_maintenance_active gauge",
            f"drover_analytics_maintenance_active {int(stats.maintenance_active)}",
        ]
    )


def _append_usage_rollup_metrics(lines: list[str]) -> None:
    # Track 3 slice 1. The counters live on the rollup module so the worker
    # and this scrape never share a connection; the gauge is the last pass's
    # control-plane window, the number #303 asks every new writer to expose.
    from drover.server.harness.usage_rollup import (
        last_pass_seconds,
        malformed_payload_count,
        rolled_session_count,
    )

    last_pass = last_pass_seconds()
    lines.extend(
        [
            "# HELP drover_usage_rollup_sessions_total "
            "Sessions re-rolled into session_usage since the server started.",
            "# TYPE drover_usage_rollup_sessions_total counter",
            f"drover_usage_rollup_sessions_total {rolled_session_count()}",
            "# HELP drover_usage_rollup_malformed_payloads_total "
            "harness_events payloads the rollup skipped as unparseable.",
            "# TYPE drover_usage_rollup_malformed_payloads_total counter",
            f"drover_usage_rollup_malformed_payloads_total {malformed_payload_count()}",
            "# HELP drover_usage_rollup_last_pass_seconds "
            "Duration of the most recent rollup pass (control-plane window).",
            "# TYPE drover_usage_rollup_last_pass_seconds gauge",
            f"drover_usage_rollup_last_pass_seconds {last_pass if last_pass is not None else 0}",
        ]
    )


def _append_redis_metrics(
    lines: list[str], job_streams: Mapping[str, RedisJobStream]
) -> None:
    lines.extend(
        [
            "# HELP drover_redis_job_stream_length Redis Stream length by Drover derived queue.",
            "# TYPE drover_redis_job_stream_length gauge",
            "# HELP drover_redis_job_stream_pending Redis consumer-group pending entries by Drover derived queue.",
            "# TYPE drover_redis_job_stream_pending gauge",
            "# HELP drover_redis_job_stream_undelivered Redis Stream undelivered entries by Drover derived queue.",
            "# TYPE drover_redis_job_stream_undelivered gauge",
            "# HELP drover_redis_job_stream_dead Redis dead-letter entries by Drover derived queue.",
            "# TYPE drover_redis_job_stream_dead gauge",
            "# HELP drover_redis_job_stream_should_shed Whether Redis queue backlog is past the configured high-water mark.",
            "# TYPE drover_redis_job_stream_should_shed gauge",
            "# HELP drover_redis_job_stream_high_water Redis queue high-water mark.",
            "# TYPE drover_redis_job_stream_high_water gauge",
            "# HELP drover_redis_job_stream_scrape_error Whether Redis stream metrics failed to scrape.",
            "# TYPE drover_redis_job_stream_scrape_error gauge",
        ]
    )
    for queue, stream in sorted(job_streams.items()):
        try:
            backpressure = stream.backpressure()
            lines.extend(
                [
                    _metric(
                        "drover_redis_job_stream_length", stream.length(), queue=queue
                    ),
                    _metric(
                        "drover_redis_job_stream_pending",
                        backpressure.get("pending", 0),
                        queue=queue,
                    ),
                    _metric(
                        "drover_redis_job_stream_undelivered",
                        backpressure.get("undelivered", 0),
                        queue=queue,
                    ),
                    _metric(
                        "drover_redis_job_stream_dead",
                        backpressure.get("dead", 0),
                        queue=queue,
                    ),
                    _metric(
                        "drover_redis_job_stream_should_shed",
                        backpressure.get("should_shed", False),
                        queue=queue,
                    ),
                    _metric(
                        "drover_redis_job_stream_high_water",
                        backpressure.get("high_water", 0),
                        queue=queue,
                    ),
                    _metric("drover_redis_job_stream_scrape_error", 0, queue=queue),
                ]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to scrape Redis stream metrics for %s: %s", queue, exc)
            lines.append(
                _metric("drover_redis_job_stream_scrape_error", 1, queue=queue)
            )


def _redis_stream_snapshots(
    job_streams: Mapping[str, RedisJobStream],
) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for queue, stream in sorted(job_streams.items()):
        try:
            length = int(stream.length())
            backpressure = stream.backpressure()
            snapshots[queue] = {
                "stream": getattr(stream, "name", queue),
                "length": length,
                "backlog": int(backpressure.get("backlog", 0) or 0),
                "pending": int(backpressure.get("pending", 0) or 0),
                "undelivered": int(backpressure.get("undelivered", 0) or 0),
                "dead": int(backpressure.get("dead", 0) or 0),
                "high_water": int(backpressure.get("high_water", 0) or 0),
                "should_shed": bool(backpressure.get("should_shed", False)),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to render Redis stream snapshot for %s: %s", queue, exc)
            snapshots[queue] = {"error": str(exc)}
    return snapshots


def _harness_cwd_suggestions(
    sessions: list[Any], favorite_cwds: Sequence[FavoriteCwd | str] = ()
) -> list[dict[str, str]]:
    suggestions: list[dict[str, str]] = []
    seen: set[tuple[str | None, str]] = set()

    def add(path: str | None, *, source: str, host_id: str | None = None) -> None:
        path = (path or "").strip()
        if not path:
            return
        key = (host_id, path)
        if key in seen:
            return
        seen.add(key)
        item = {"path": path, "source": source}
        if host_id:
            item["host_id"] = host_id
        suggestions.append(item)

    for session in sessions:
        add(
            getattr(session, "cwd", None),
            source="recent session",
            host_id=getattr(session, "host_id", None),
        )
    for favorite in favorite_cwds:
        # A bare string is still accepted here (and still means every host):
        # the config file allows one, and so does anything constructing a
        # collector directly.
        if isinstance(favorite, str):
            favorite = FavoriteCwd(favorite)
        add(favorite.path, source="favorite", host_id=favorite.host_id)
    return suggestions[:24]


def _harness_endpoint(host: Any) -> str:
    return (
        getattr(host, "local_url", None) or getattr(host, "tailscale_url", None) or ""
    ).rstrip("/")


def _relay_silence_reason(relay: Any, host_id: str) -> str:
    """Say which kind of silence this is, so the refusal is actionable."""
    if relay is None or not relay.is_live(host_id):
        return "no relay connection is attached"
    silent_for = relay.silent_for(host_id)
    if silent_for is None:
        return "the spoke attached but has never sent a frame"
    return f"the spoke has sent no frames for {silent_for:.0f}s"


def _json_response(status: int, payload: Mapping[str, Any]) -> tuple[int, str]:
    return status, json.dumps(dict(payload), sort_keys=True, default=str) + "\n"


def _model_catalog_failure_reason(status: int, body: str) -> str:
    if status in {401, 403}:
        return "not_authenticated"
    if status == 404:
        return "unsupported"
    lowered = body.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if any(
        marker in lowered
        for marker in (
            "byte limit",
            "content-length",
            "framed response capability",
            "decode",
            "utf-8",
        )
    ):
        return "protocol_error"
    if status == 502 or status >= 500:
        return "offline"
    return "protocol_error"


def _model_catalog_exception_reason(exc: Exception) -> str:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, (TypeError, ValueError, UnicodeError)):
        return "protocol_error"
    lowered = str(exc).lower()
    return "timeout" if "timed out" in lowered or "timeout" in lowered else "offline"


def _insight_response(render) -> tuple[int, str]:
    try:
        return _json_response(200, render())
    except Exception as exc:  # noqa: BLE001 - advisory failure stays section-local
        return _insight_error_response(exc)


def _insight_error_response(exc: Exception) -> tuple[int, str]:
    from drover.server.advisory.service import (
        InvalidInsightRequest,
        InvalidInsightTransition,
    )

    if isinstance(exc, InvalidInsightRequest):
        return _json_response(400, {"error": str(exc)})
    if isinstance(exc, InvalidInsightTransition):
        return _json_response(409, {"error": str(exc)})
    if isinstance(exc, KeyError):
        return _json_response(404, {"error": "insight not found"})
    log.warning("advisory API failed: %s", exc)
    return _json_response(503, {"error": "insights temporarily unavailable"})


def _error_text(body: str) -> str:
    try:
        error = json.loads(body).get("error")
    except (AttributeError, json.JSONDecodeError, TypeError):
        return ""
    return error if isinstance(error, str) else ""


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_event_timestamp(value: Any) -> datetime | None:
    text = _optional_str(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _wire_datetime(value: Any) -> str | None:
    if value is None:
        return None

    if not isinstance(value, datetime):
        parsed = _parse_event_timestamp(value)
        if parsed is None:
            return str(value)
        value = parsed
    # DuckDB TIMESTAMP columns round-trip aware datetimes as naive local wall
    # time. Attach the process timezone before normalizing to UTC so clients do
    # not reinterpret local Pacific times as UTC and render fresh sessions as
    # seven hours old.
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc).isoformat()


def _wire_datetimes(item: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    for key in keys:
        if key in item:
            item[key] = _wire_datetime(item.get(key))
    return item


def _command_label(command: Any) -> str:
    if isinstance(command, str):
        return command
    if isinstance(command, list | tuple):
        return " ".join(str(part) for part in command)
    return str(command)


def _native_session_id(native_resume: Any) -> str | None:
    if not isinstance(native_resume, dict):
        return None
    return _optional_str(native_resume.get("session_id"))


def _native_resume_label(native_resume: Any) -> str | None:
    if not isinstance(native_resume, dict):
        return None
    if label := _optional_str(native_resume.get("label")):
        return label
    if session_id := _optional_str(native_resume.get("session_id")):
        return session_id
    if native_resume.get("latest"):
        return "latest"
    return _optional_str(native_resume.get("mode"))


def _harness_host_dict(
    host: Any, relay_manager: "RelayManager | None" = None
) -> dict[str, Any]:
    item = dict(host.__dict__)
    if item.get("connection_kind") == "relay":
        # A relay host has no daemon-reported heartbeat to trust -- the hub's
        # own socket is ground truth, so it always wins over whatever status
        # happens to be stored in the row (never leak a stale "online" once
        # the socket has dropped, and vice versa).
        #
        # Attachment is not the test, though. A spoke can attach and then send
        # nothing, and because it reconnects the moment the silence watchdog
        # drops it, `is_live` reads true almost continuously for a host that
        # has been mute for an hour. Ask whether it is talking back instead.
        responsive = (
            relay_manager.is_responsive(host.host_id) if relay_manager else False
        )
        item["status"] = "online" if responsive else "offline"
        return _wire_datetimes(item, ("last_seen_at", "created_at", "updated_at"))
    last_seen_at = getattr(host, "last_seen_at", None)
    if last_seen_at is not None:
        if last_seen_at.tzinfo is None:
            age_s = (datetime.now() - last_seen_at).total_seconds()
        else:
            age_s = (datetime.now(timezone.utc) - last_seen_at).total_seconds()
        if age_s > HARNESS_STALE_AFTER_SECONDS and item.get("status") == "online":
            item["status"] = "stale"
            item["stale_after_seconds"] = HARNESS_STALE_AFTER_SECONDS
    return _wire_datetimes(item, ("last_seen_at", "created_at", "updated_at"))


def _harness_session_dict(
    session: Any,
    preview: str | None = None,
    recap: LiveRecap | None = None,
) -> dict[str, Any]:
    item = dict(session.__dict__)
    _wire_datetimes(
        item,
        ("started_at", "updated_at", "ended_at", "last_activity"),
    )
    item["preview"] = _optional_str(preview)
    # Cleaned on the way out as well as on write, so recaps stored before the
    # subject was dropped do not keep narrating "The user is ..." until every
    # session happens to be re-recapped.
    item["recap"] = _optional_str(
        drop_user_subject(recap.text) if recap and recap.text else None
    )
    item["recap_source_seq"] = recap.source_seq if recap else None
    return item


def _append_adoption_metrics(lines: list[str], snapshot: dict) -> None:
    adoption = (
        snapshot.get("categories", {}).get("agent_adoption", {}).get("details", {})
    )
    runtimes = adoption.get("runtimes", []) or []
    lines.extend(
        [
            "# HELP drover_agent_adoption_ready Whether an agent runtime has Drover events, MCP, and skill coverage.",
            "# TYPE drover_agent_adoption_ready gauge",
            "# HELP drover_agent_adoption_observed_events Observed event volume by registered agent runtime.",
            "# TYPE drover_agent_adoption_observed_events gauge",
            "# HELP drover_agent_adoption_unmatched_high_volume_agents High-volume observed agents missing from the adoption registry.",
            "# TYPE drover_agent_adoption_unmatched_high_volume_agents gauge",
        ]
    )
    for row in runtimes:
        runtime = row.get("runtime", "unknown")
        lines.append(
            _metric(
                "drover_agent_adoption_ready",
                bool(row.get("ready")),
                runtime=runtime,
                status=row.get("status", "unknown"),
            )
        )
        lines.append(
            _metric(
                "drover_agent_adoption_observed_events",
                int(row.get("observed_events", 0) or 0),
                runtime=runtime,
            )
        )
    lines.append(
        _metric(
            "drover_agent_adoption_unmatched_high_volume_agents",
            len(adoption.get("unmatched_high_volume_agent_ids", []) or []),
        )
    )


@dataclass
class MetricsCollector:
    """Cached Drover metrics renderer for Prometheus scrapes."""

    duckdb_path: Path
    incoming_dir: Path
    summarizer_report: Mapping[str, Any]
    job_streams: Mapping[str, RedisJobStream] = field(default_factory=dict)
    ttl_seconds: float = 60.0
    api_token: str = ""
    # New Session sheet "favorite" cwd suggestions, from [harness].favorite_cwds
    # in ~/.drover/config.toml (empty by default — no hardcoded paths). Each
    # carries the host it belongs to, or None for every host.
    favorite_cwds: tuple[FavoriteCwd | str, ...] = ()
    # Set by start_metrics_server; owns live hub<->harnessd relay connections.
    relay_manager: "RelayManager | None" = None
    # Separate from ttl_seconds (60s, Prometheus): a fleet view must feel
    # live, but N polling clients should still share one render.
    harness_ttl_seconds: float = 2.0
    # Newest finished sessions kept in a fleet render. Unbounded, this grew
    # with every session ever run -- 115 of 120 were terminated when the cap
    # was added, at 117KB per poll per client. Live sessions are never capped.
    # Callers may ask for more, up to MAX_ARCHIVED_SESSION_LIMIT.
    archived_session_limit: int = 20
    # How long past ttl_seconds an expired render may still be served while
    # its replacement is built in the background. Serving stale forever would
    # freeze the numbers whenever refreshes keep failing, and frozen metrics
    # read as healthy ones; past this the caller waits and the error surfaces.
    max_stale_seconds: float = 300.0
    cockpit_service: "CockpitService | None" = None
    advisory_service: "InsightsService | None" = None
    # Where InsightsService reads config and, beside it, the durable
    # content-consent state. None means the real user config path, which is
    # right in production and untestable everywhere else: without an override
    # a test reads the running server's own consent epoch off the machine.
    config_path: Path | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _cached_text: str | None = field(default=None, init=False)
    _cached_json: str | None = field(default=None, init=False)
    _cached_until: float = field(default=0.0, init=False)
    #: Rendered harness snapshots by variant, each with its expiry. Keyed
    #: because `/harness/hosts` asks for `include_sessions=False` and was
    #: therefore never cached -- while being the endpoint the fleet actually
    #: polls. Bounded: only the default archived cap is cached, so the key
    #: space is the two booleans and no client can grow it.
    _harness_cache: dict[tuple[bool, bool], tuple[float, str]] = field(
        default_factory=dict, init=False
    )
    _refreshing: bool = field(default=False, init=False)
    _refresh_guard: threading.Lock = field(default_factory=threading.Lock, init=False)
    _session_locks: dict[str, threading.Lock] = field(default_factory=dict, init=False)
    _session_locks_guard: threading.Lock = field(
        default_factory=threading.Lock, init=False
    )
    _readiness: "ReadinessProbe | None" = field(default=None, init=False)
    _readiness_guard: threading.Lock = field(default_factory=threading.Lock, init=False)

    def render_readiness(self, *, include_detail: bool = True) -> tuple[int, str]:
        """Answer ``/readyz``: 200 only while both stores still serve (#175).

        The probe is built once and kept, because it owns the brief cache that
        stops a hot poller turning readiness into load. ``include_detail`` is
        false for an unauthenticated caller: the verdict is public, the
        DuckDB error behind it is not.
        """
        with self._readiness_guard:
            if self._readiness is None:
                self._readiness = ReadinessProbe(self.duckdb_path)
            probe = self._readiness
        return probe.check().as_response(include_detail=include_detail)

    def render_prometheus(self) -> str:
        self._refresh_if_needed()
        return self._cached_text or ""

    def render_json(self) -> str:
        self._refresh_if_needed()
        return self._cached_json or "{}\n"

    def render_harness_json(
        self,
        *,
        include_hosts: bool = True,
        include_sessions: bool = True,
        archived_limit: int | None = _UNSET_ARCHIVED_LIMIT,
    ) -> str:
        # Only the default full render is cached; partial renders are rare
        # and caching them would need a per-variant key for no real gain.
        # A caller asking for a non-default archived cap counts as partial for
        # the same reason -- serving it the cached default would silently hand
        # back 20 sessions to someone who asked for 100.
        # A caller-supplied archived cap is not cached: serving it the cached
        # default would silently hand back the default number of sessions to
        # someone who asked for 100, and caching per cap would let any client
        # grow the dictionary without bound.
        default_cap = archived_limit is _UNSET_ARCHIVED_LIMIT
        key = (include_hosts, include_sessions)
        now = time.monotonic()
        if default_cap:
            entry = self._harness_cache.get(key)
            if entry is not None and now < entry[0]:
                return entry[1]
        snapshot = self.harness_snapshot(
            include_hosts=include_hosts,
            include_sessions=include_sessions,
            archived_limit=archived_limit,
        )
        rendered = json.dumps(snapshot, sort_keys=True, default=str) + "\n"
        if default_cap:
            self._harness_cache[key] = (now + self.harness_ttl_seconds, rendered)
        return rendered

    def invalidate_harness_cache(self) -> None:
        """Drop every rendered variant.

        All of them, not just the full render: a stale hosts-only answer is
        exactly as wrong as a stale full one, and it is the one being polled.
        """
        self._harness_cache.clear()

    def render_harness_session_json(self, session_id: str) -> tuple[int, str]:
        snapshot = self.harness_session_snapshot(session_id)
        status = 404 if snapshot.get("error") else 200
        return status, json.dumps(snapshot, sort_keys=True, default=str) + "\n"

    def render_cockpit_overview_json(self, filters: Any) -> tuple[int, str]:
        if self.cockpit_service is None:
            return _json_response(503, {"error": "cockpit service unavailable"})
        return _json_response(200, self.cockpit_service.overview(filters))

    def render_analytics_json(self, filters: Any) -> tuple[int, str]:
        from drover.server.cockpit.analytics import AnalyticsSnapshotChangedError

        if self.cockpit_service is None:
            return _json_response(503, {"error": "cockpit service unavailable"})
        try:
            return _json_response(200, self.cockpit_service.analytics(filters))
        except AnalyticsSnapshotChangedError:
            return _json_response(
                409,
                {
                    "error": "snapshot_changed",
                    "detail": "Activity changed; reload analytics from the first page.",
                },
            )
        except ValueError as exc:
            return _json_response(400, {"error": str(exc)})

    def _insights(self) -> "InsightsService":
        if self.advisory_service is None:
            from drover.server.advisory.service import InsightsService

            self.advisory_service = InsightsService(
                self.duckdb_path, config_path=self.config_path
            )
        self.advisory_service.set_content_consent_propagator(
            self._propagate_content_consent
        )
        return self.advisory_service

    def render_insights_json(self, filters: "InsightFilters") -> tuple[int, str]:
        return _insight_response(lambda: self._insights().list_insights(filters))

    def render_insight_json(self, finding_id: str) -> tuple[int, str]:
        return _insight_response(lambda: self._insights().get_insight(finding_id))

    def render_content_analysis_status_json(self) -> tuple[int, str]:
        try:
            payload = self._insights().content_analysis_status()
            propagation = payload.get("propagation")
            if propagation == "failed":
                status = 503
            elif propagation in {None, "complete"}:
                status = 200
            else:
                status = 207
            return _json_response(status, payload)
        except Exception as exc:
            return _insight_error_response(exc)

    def consent_content_analysis(self, body: Mapping[str, Any]) -> tuple[int, str]:
        from drover.server.advisory.service import (
            InvalidInsightRequest,
            validate_action_body,
        )

        try:
            validate_action_body(
                body, allowed={"backend", "external_disclosure_accepted"}
            )
            backend = body.get("backend")
            if not isinstance(backend, str):
                raise InvalidInsightRequest("backend must be local or cloud")
            disclosure = body.get("external_disclosure_accepted", False)
            payload = self._insights().consent_content_analysis(
                backend=backend,
                external_disclosure_accepted=disclosure,
            )
            return _json_response(
                200 if payload.get("propagation") == "complete" else 207,
                payload,
            )
        except Exception as exc:
            return _insight_error_response(exc)

    def revoke_content_analysis(self, body: Mapping[str, Any]) -> tuple[int, str]:
        from drover.server.advisory.service import validate_action_body

        try:
            validate_action_body(body, allowed=set())
            payload = self._insights().revoke_content_analysis()
            propagation = payload.get("propagation")
            if propagation == "failed":
                status = 503
            elif propagation == "complete":
                status = 200
            else:
                status = 207
            return _json_response(status, payload)
        except Exception as exc:
            return _insight_error_response(exc)

    def purge_content_excerpts(self) -> tuple[int, str]:
        return _insight_response(
            lambda: {"purged_excerpt_count": self._insights().purge_content_excerpts()}
        )

    def act_on_insight(
        self, finding_id: str, action: str, body: Mapping[str, Any]
    ) -> tuple[int, str]:
        from drover.server.advisory.service import (
            InvalidInsightRequest,
            validate_action_body,
        )

        try:
            if action == "acknowledge":
                validate_action_body(body, allowed=set())
                payload = self._insights().acknowledge(finding_id)
                status = 200
            elif action == "dismiss":
                validate_action_body(body, allowed={"reason"})
                payload = self._insights().dismiss(
                    finding_id, reason=body.get("reason")
                )
                status = 200
            elif action == "check":
                validate_action_body(body, allowed=set())
                payload = self._insights().check_again(finding_id)
                status = 202
            else:
                raise InvalidInsightRequest("invalid insight action")
            return _json_response(status, payload)
        except Exception as exc:  # normalized below, isolated from other APIs
            return _insight_error_response(exc)

    def register_harness_host(self, payload: Mapping[str, Any]) -> tuple[int, str]:
        host_id = str(payload.get("host_id") or "").strip()
        if not host_id:
            return _json_response(400, {"error": "missing host_id"})
        display_name = str(payload.get("display_name") or host_id)
        kind = str(payload.get("kind") or "unknown")
        status = str(payload.get("status") or "online")
        capabilities = payload.get("capabilities")
        if capabilities is not None and not isinstance(capabilities, dict):
            return _json_response(400, {"error": "capabilities must be an object"})
        # Only a real string counts. `_optional_str` would happily turn a dict
        # into its repr and store that as the fleet's idea of a version, and a
        # malformed heartbeat should cost the version field, not the whole
        # registration -- a host that cannot register is a host you lose.
        raw_version = payload.get("agent_version")
        agent_version = (
            raw_version.strip() or None if isinstance(raw_version, str) else None
        )
        raw_update = payload.get("update")
        update = raw_update if isinstance(raw_update, dict) else None
        try:
            registry = HarnessRegistry(self.duckdb_path)
            host = registry.register_host(
                host_id=host_id,
                display_name=display_name,
                kind=kind,
                local_url=_optional_str(payload.get("local_url")),
                tailscale_url=_optional_str(payload.get("tailscale_url")),
                connection_kind=str(payload.get("connection_kind") or "direct"),
                status=status,
                capabilities=capabilities,
                agent_version=agent_version,
                update=update,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to register harness host %s: %s", host_id, exc)
            return _json_response(500, {"error": str(exc)})
        body = {
            "host": host.__dict__,
            "content_consent": self._insights().content_consent_state(),
        }
        # Every harnessd already polls this endpoint every 15 seconds, so the
        # fleet's target version rides this response rather than needing a
        # channel of its own. No planner, or no target, means no extra keys,
        # and a host that sees none simply stays where it is.
        planner = getattr(self, "update_planner", None)
        if planner is not None:
            try:
                body.update(planner.as_heartbeat_payload())
            except Exception:  # noqa: BLE001 - never fail a heartbeat over this
                log.exception("could not attach an update target to a heartbeat")
        return _json_response(200, body)

    def proxy_create_harness_session(
        self, host_id: str, payload: Mapping[str, Any]
    ) -> tuple[int, str]:
        host = self._harness_host(host_id)
        if host is None:
            return _json_response(404, {"error": f"unknown harness host: {host_id}"})
        # A silent spoke accepts nothing. Creating anyway produced the worst
        # possible outcome for the phone: a session row that says `running`,
        # records zero events, and reports no error anywhere, because the
        # create was delivered to a socket the host was not reading.
        if getattr(host, "connection_kind", "direct") == "relay":
            relay = self.relay_manager
            if relay is None or not relay.wait_until_responsive(
                host_id, RELAY_PROOF_TIMEOUT_S
            ):
                return _json_response(
                    502,
                    {
                        "error": f"relay host is not responding: {host_id}",
                        "reason": _relay_silence_reason(relay, host_id),
                    },
                )
        status, body = self._harness_request(
            host,
            "/sessions",
            method="POST",
            payload=payload,
            timeout_s=CREATE_SESSION_TIMEOUT_S,
        )
        if 200 <= status < 300:
            self._sync_created_harness_session(host_id, payload, body)
        return status, body

    def fetch_harness_provider_usage(self, host: Any) -> Mapping[str, Any]:
        """Fetch host-local provider facts through direct or relay routing."""
        status, body = self._harness_request(
            host, "/providers/usage", method="GET", timeout_s=10.0
        )
        if not 200 <= status < 300:
            raise RuntimeError("unavailable")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError("provider usage response must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("provider usage response must be an object")
        return payload

    def fetch_advisory_content_bundle(
        self, host_id: str, target_ids: list[str]
    ) -> Mapping[str, Any]:
        """Fetch one ephemeral, redacted bundle through normal host routing."""
        if not _valid_advisory_target_ids(target_ids):
            raise ValueError("target_ids must be a non-empty list of unique IDs")
        host = self._harness_host(host_id)
        if host is None:
            raise ValueError(f"unknown harness host: {host_id}")
        consent = self._insights().content_consent_state()
        if not consent["enabled"] or int(consent["epoch"]) <= 0:
            raise RuntimeError("content analysis is disabled")
        reconciliation = self._push_content_consent(host, consent)
        if reconciliation["state"] != "acknowledged":
            raise RuntimeError("content consent is not reconciled on host")
        status, body = self._harness_request(
            host,
            "/advisory/content-bundle",
            method="POST",
            payload={"target_ids": target_ids},
            timeout_s=15.0,
            max_response_bytes=_MAX_CONTENT_BUNDLE_RESPONSE_BYTES,
        )
        body_bytes = body.encode("utf-8")
        if len(body_bytes) > _MAX_CONTENT_BUNDLE_RESPONSE_BYTES:
            raise ValueError("content bundle response exceeds byte limit")
        if not 200 <= status < 300:
            raise RuntimeError(f"content bundle request failed with status {status}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError("content bundle response must be valid JSON") from exc
        _validate_advisory_content_bundle(payload, requested_ids=target_ids)
        log.info(
            "fetched advisory content bundle host=%s targets=%d bytes=%d bundle_hash=%s",
            host_id,
            len(payload["targets"]),
            len(body_bytes),
            payload["bundle_hash"],
        )
        return payload

    def fetch_advisory_content_version(
        self, host_id: str, target_ids: list[str]
    ) -> Mapping[str, Any]:
        """Fetch one bounded hashes-only version through normal host routing."""

        if not _valid_advisory_target_ids(target_ids):
            raise ValueError("target_ids must be a non-empty list of unique IDs")
        host = self._harness_host(host_id)
        if host is None:
            raise ValueError(f"unknown harness host: {host_id}")
        consent = self._insights().content_consent_state()
        if not consent["enabled"] or int(consent["epoch"]) <= 0:
            raise RuntimeError("content analysis is disabled")
        reconciliation = self._push_content_consent(host, consent)
        if reconciliation["state"] != "acknowledged":
            raise RuntimeError("content consent is not reconciled on host")
        status, body = self._harness_request(
            host,
            "/advisory/content-version",
            method="POST",
            payload={"target_ids": target_ids},
            timeout_s=10.0,
            max_response_bytes=_MAX_CONTENT_VERSION_RESPONSE_BYTES,
        )
        body_bytes = body.encode("utf-8")
        if len(body_bytes) > _MAX_CONTENT_VERSION_RESPONSE_BYTES:
            raise ValueError("content version response exceeds byte limit")
        if not 200 <= status < 300:
            raise RuntimeError(f"content version request failed with status {status}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError("content version response must be valid JSON") from exc
        _validate_advisory_content_version(payload, requested_ids=target_ids)
        log.info(
            "fetched advisory content version host=%s targets=%d bundle_hash=%s",
            host_id,
            len(payload["targets"]),
            payload["bundle_hash"],
        )
        return payload

    def _propagate_content_consent(
        self, enabled: bool, epoch: int
    ) -> list[dict[str, str]]:
        try:
            hosts = HarnessRegistry(self.duckdb_path).list_hosts()
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to enumerate hosts for content consent: %s", exc)
            return [{"host_id": "fleet", "state": "failed"}]
        consent = {"enabled": enabled, "epoch": epoch}
        results: list[dict[str, str]] = []
        for host in hosts:
            if str(getattr(host, "status", "offline")) != "online":
                results.append({"host_id": host.host_id, "state": "disconnected"})
                continue
            results.append(self._push_content_consent(host, consent))
        return results

    def _push_content_consent(
        self, host: Any, consent: Mapping[str, Any]
    ) -> dict[str, str]:
        status, body = self._harness_request(
            host,
            "/advisory/content-consent",
            method="POST",
            payload={
                "enabled": bool(consent["enabled"]),
                "epoch": int(consent["epoch"]),
            },
            timeout_s=10.0,
        )
        if status == 502:
            return {"host_id": host.host_id, "state": "disconnected"}
        if not 200 <= status < 300:
            return {"host_id": host.host_id, "state": "failed"}
        try:
            acknowledged = json.loads(body)
        except json.JSONDecodeError:
            acknowledged = None
        expected = {
            "enabled": bool(consent["enabled"]),
            "epoch": int(consent["epoch"]),
        }
        if acknowledged != expected:
            return {"host_id": host.host_id, "state": "failed"}
        return {"host_id": host.host_id, "state": "acknowledged"}

    def proxy_terminate_harness_session(self, session_id: str) -> tuple[int, str]:
        with self._session_lock_for(session_id):
            return self._proxy_terminate_harness_session(session_id)

    def _proxy_terminate_harness_session(self, session_id: str) -> tuple[int, str]:
        session = self._harness_session(session_id)
        if session is None:
            return _json_response(
                404, {"error": f"unknown harness session: {session_id}"}
            )
        host = self._harness_host(session.host_id)
        if host is None:
            return _json_response(
                404, {"error": f"unknown harness host: {session.host_id}"}
            )
        status, body = self._harness_request(
            host,
            f"/sessions/{session_id}/terminate",
            method="POST",
            payload={},
        )
        if 200 <= status < 300:
            self._sync_terminated_harness_session(session_id, body)
            return status, body
        if status in (404, 502):
            # The daemon no longer knows this session (restart lost it) or
            # the host is unreachable entirely. Either way there is nothing
            # left to terminate on the host -- tombstone the registry row so
            # it stops looking alive, and report success to the client.
            self._tombstone_stale_harness_session(session_id)
            return _json_response(
                200,
                {
                    "session_id": session_id,
                    "status": "terminated",
                    "stale": True,
                },
            )
        return status, body

    def proxy_harness_session_action(
        self,
        session_id: str,
        action: str,
        payload: Mapping[str, Any],
    ) -> tuple[int, str]:
        """Proxy a structured-session action (turns/permission/interrupt) to
        the owning host's harnessd, forwarding the JSON body verbatim."""
        with self._session_lock_for(session_id):
            return self._proxy_harness_session_action(session_id, action, payload)

    def _proxy_harness_session_action(
        self,
        session_id: str,
        action: str,
        payload: Mapping[str, Any],
    ) -> tuple[int, str]:
        session = self._harness_session(session_id)
        if session is None:
            return _json_response(
                404, {"error": f"unknown harness session: {session_id}"}
            )
        host = self._harness_host(session.host_id)
        if host is None:
            return _json_response(
                404, {"error": f"unknown harness host: {session.host_id}"}
            )
        status, body = self._harness_request(
            host,
            f"/sessions/{session_id}/{action}",
            method="POST",
            payload=payload,
            # Turn bodies can carry base64 images — orders of magnitude
            # larger than any other proxied payload.
            timeout_s=60.0 if action == "turns" else 15.0,
        )
        if (
            action == "turns"
            and status == 404
            and _error_text(body) == f"unknown structured session: {session_id}"
        ):
            native_session_id = self._native_session_id_for_recovery(session_id)
            if (
                session.harness not in _RECOVERABLE_STRUCTURED_HARNESSES
                or native_session_id is None
            ):
                return _json_response(409, {"error": _RECOVERY_UNAVAILABLE})
            recovery_status, _recovery_body = self._harness_request(
                host,
                f"/sessions/{session_id}/recover",
                method="POST",
                payload={"native_session_id": native_session_id},
                timeout_s=15.0,
            )
            if recovery_status == 409:
                return _json_response(409, {"error": _RECOVERY_UNAVAILABLE})
            if not 200 <= recovery_status < 300:
                return recovery_status, _recovery_body
            try:
                HarnessRegistry(self.duckdb_path).mark_session_recovered(
                    session_id, native_session_id
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "failed to sync recovered harness session %s: %s",
                    session_id,
                    exc,
                )
            self.invalidate_harness_cache()
            status, body = self._harness_request(
                host,
                f"/sessions/{session_id}/{action}",
                method="POST",
                payload=payload,
                timeout_s=60.0,
            )
        if action == "turns" and 200 <= status < 300:
            self._sync_harness_session_preferences(session_id, payload)
        return status, body

    def _native_session_id_for_recovery(self, session_id: str) -> str | None:
        try:
            registry = HarnessRegistry(self.duckdb_path)
            session = registry.get_session(session_id)
            if session is not None and session.native_session_id:
                return session.native_session_id
            for event in reversed(registry.list_events(session_id)):
                payload = event.payload or {}
                candidates = [payload.get("native_session_id")]
                nested = payload.get("payload")
                if isinstance(nested, dict):
                    candidates.append(nested.get("native_session_id"))
                for candidate in candidates:
                    if isinstance(candidate, str) and candidate.strip():
                        return candidate.strip()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "failed to resolve native session id for %s: %s", session_id, exc
            )
        return None

    def _session_lock_for(self, session_id: str) -> threading.Lock:
        with self._session_locks_guard:
            return self._session_locks.setdefault(session_id, threading.Lock())

    def proxy_harness_native_sessions(
        self,
        host_id: str,
        query: Mapping[str, Any],
    ) -> tuple[int, str]:
        host = self._harness_host(host_id)
        if host is None:
            return _json_response(404, {"error": f"unknown harness host: {host_id}"})
        params = {
            key: value
            for key, value in {
                "harness": query.get("harness"),
                "cwd": query.get("cwd"),
                "limit": query.get("limit") or 20,
            }.items()
            if value not in {None, ""}
        }
        path = "/native-sessions"
        if params:
            path = f"{path}?{urlencode(params)}"
        return self._harness_request(
            host,
            path,
            method="GET",
            payload={},
        )

    def proxy_harness_fs_complete(
        self, host_id: str, path_text: str
    ) -> tuple[int, str]:
        """Proxy one keystroke's worth of path completion to a host."""
        host = self._harness_host(host_id)
        if host is None:
            return _json_response(404, {"error": f"unknown harness host: {host_id}"})
        return self._harness_request(
            host,
            "/fs/complete?" + urlencode({"path": path_text}),
            method="GET",
            payload={},
            timeout_s=FS_COMPLETE_TIMEOUT_S,
        )

    def proxy_harness_fs_exists(
        self, host_id: str, payload: Mapping[str, Any]
    ) -> tuple[int, str]:
        """Proxy a bounded "are these still directories?" batch to a host."""
        host = self._harness_host(host_id)
        if host is None:
            return _json_response(404, {"error": f"unknown harness host: {host_id}"})
        return self._harness_request(
            host,
            "/fs/exists",
            method="POST",
            payload=payload,
            timeout_s=FS_COMPLETE_TIMEOUT_S,
        )

    def proxy_harness_model_catalog(
        self, host_id: str, harness: str, *, refresh: bool = False
    ) -> tuple[int, str]:
        """Fetch one host catalog and degrade safely to the central LKG."""
        if (
            not isinstance(host_id, str)
            or not host_id
            or len(host_id) > MAX_ID_LENGTH
            or not isinstance(harness, str)
            or not harness
            or len(harness) > MAX_ID_LENGTH
            or not isinstance(refresh, bool)
        ):
            return _json_response(400, {"error": "invalid model catalog request"})

        host = self._harness_host(host_id)
        if host is None:
            return _json_response(404, {"error": "unknown harness host"})
        advertised = host.capabilities.get("harnesses")
        if not isinstance(advertised, list) or not any(
            isinstance(item, dict)
            and item.get("name") == harness
            and item.get("enabled") is True
            for item in advertised
        ):
            return _json_response(404, {"error": "harness is not enabled"})

        path = "/model-catalog?" + urlencode(
            {"harness": harness, "refresh": "1" if refresh else "0"}
        )
        try:
            status, body = self._harness_request(
                host,
                path,
                method="GET",
                payload={},
                timeout_s=7.0,
                max_response_bytes=MAX_CATALOG_WIRE_BYTES,
            )
        except Exception as exc:  # noqa: BLE001 - normalized to safe metadata
            reason = _model_catalog_exception_reason(exc)
            return self._stale_model_catalog_response(host_id, harness, reason)

        if not isinstance(body, str):
            return self._stale_model_catalog_response(
                host_id, harness, "protocol_error"
            )
        try:
            body_bytes = body.encode("utf-8")
        except UnicodeEncodeError:
            return self._stale_model_catalog_response(
                host_id, harness, "protocol_error"
            )
        if len(body_bytes) > MAX_CATALOG_WIRE_BYTES:
            return self._stale_model_catalog_response(
                host_id, harness, "protocol_error"
            )
        if not 200 <= status < 300:
            reason = _model_catalog_failure_reason(status, body)
            return self._stale_model_catalog_response(host_id, harness, reason)

        try:
            decoded = json.loads(body)
            envelope = CatalogEnvelope.from_wire(decoded, host_id, harness)
            normalized_body = catalog_wire_bytes(envelope).decode("utf-8")
        except (TypeError, json.JSONDecodeError, ValueError):
            return self._stale_model_catalog_response(
                host_id, harness, "protocol_error"
            )

        if envelope.stale:
            return self._stale_model_catalog_response(
                host_id, harness, envelope.stale_reason or "protocol_error"
            )

        if envelope.account_scope_id is not None:
            try:
                HarnessRegistry(self.duckdb_path).save_model_catalog(
                    host_id,
                    harness,
                    envelope.account_scope_id,
                    envelope.to_wire(),
                )
            except Exception as exc:  # noqa: BLE001 - live result remains usable
                log.warning(
                    "failed to persist model catalog host=%s harness=%s: %s",
                    host_id,
                    harness,
                    exc,
                )
        return 200, normalized_body

    def _stale_model_catalog_response(
        self, host_id: str, harness: str, reason: str
    ) -> tuple[int, str]:
        cached = HarnessRegistry(self.duckdb_path).latest_model_catalog(
            host_id, harness
        )
        try:
            if cached is None:
                envelope = CatalogEnvelope.empty_failure(host_id, harness, reason)
            else:
                cached = dict(cached)
                cached["stale"] = True
                cached["stale_reason"] = reason
                envelope = CatalogEnvelope.from_wire(cached, host_id, harness)
            body = catalog_wire_bytes(envelope).decode("utf-8")
        except (TypeError, ValueError):
            envelope = CatalogEnvelope.empty_failure(host_id, harness, "protocol_error")
            body = catalog_wire_bytes(envelope).decode("utf-8")
        return 200, body

    def proxy_harness_auth(
        self,
        host_id: str,
        harness: str,
        action: str,
        *,
        flow_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, str]:
        host = self._harness_host(host_id)
        if host is None:
            return _json_response(404, {"error": f"unknown harness host: {host_id}"})

        if action in {"status", "start"}:
            path = f"/auth/{quote(harness, safe='')}/{action}"
        elif action in {"flow", "cancel", "input"} and flow_id:
            suffix = {"flow": "", "cancel": "/cancel", "input": "/input"}[action]
            path = f"/auth/{quote(harness, safe='')}/flows/{quote(flow_id, safe='')}{suffix}"
        else:
            return _json_response(400, {"error": "invalid auth action"})

        status, body = self._harness_request(
            host,
            path,
            method="GET" if action in {"status", "flow"} else "POST",
            # Only the input action carries one; the rest are bodyless POSTs.
            payload=payload or {},
        )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return status, body
        if isinstance(payload, dict):
            payload.setdefault("host_id", host_id)
            payload.setdefault("harness", harness)
            return _json_response(status, payload)
        return status, body

    def proxy_harness_native_transcript(self, session_id: str) -> tuple[int, str]:
        session = self._harness_session(session_id)
        if session is None:
            return _json_response(
                404, {"error": f"unknown harness session: {session_id}"}
            )
        host = self._harness_host(session.host_id)
        if host is None:
            return _json_response(
                404, {"error": f"unknown harness host: {session.host_id}"}
            )
        params = {
            key: value
            for key, value in {
                "native_session_id": session.native_session_id,
                "limit": 100,
            }.items()
            if value not in {None, ""}
        }
        path = f"/sessions/{session_id}/native-transcript"
        if params:
            path = f"{path}?{urlencode(params)}"
        # 2.0s: the transcript endpoint is polled, and the sub-second-only
        # rule for RelayManager.request permits any timeout >= 1s.
        status, body = self._harness_request(
            host,
            path,
            method="GET",
            payload={},
            timeout_s=2.0,
        )
        if status == 404 and session.host_id in {"mac-mini", "localhost"}:
            transcript = native_transcript_for_session(
                harness=session.harness,
                cwd=session.cwd,
                native_session_id=session.native_session_id,
                limit=100,
            )
            provider_session_id = transcript.get("session_id")
            transcript.update(
                {
                    "host_id": session.host_id,
                    "session_id": session_id,
                    "native_session_id": provider_session_id,
                    "harness": session.harness,
                    "cwd": session.cwd,
                }
            )
            return _json_response(200, transcript)
        return status, body

    def continue_harness_session(
        self,
        session_id: str,
        payload: Mapping[str, Any],
    ) -> tuple[int, str]:
        source = self._harness_session(session_id)
        if source is None:
            return _json_response(
                404, {"error": f"unknown harness session: {session_id}"}
            )
        target_host_id = str(payload.get("target_host_id") or source.host_id)
        target_harness = str(payload.get("target_harness") or source.harness)
        native_resume = payload.get("native_resume")
        handoff_mode = "native_resume" if native_resume else "nexus_handoff"
        if not native_resume and target_harness in _STRUCTURED_HANDOFF_HARNESSES:
            # Nexus handoff to a structured-capable harness: launch a
            # structured session and deliver the handoff text as the first
            # turn ("prompt"). The daemon sends it once the driver is up, so
            # there is no typed-seed race against the CLI's cold start (and
            # no rows/cols/initial_input -- those are PTY concepts).
            structured_payload: dict[str, Any] = {
                "mode": "structured",
                "harness": target_harness,
                "cwd": source.cwd,
                "repo_owner": source.repo_owner,
                "repo_name": source.repo_name,
                "branch": source.branch,
                "source_session_id": source.session_id,
                "handoff_mode": "nexus_handoff",
                "prompt": self._build_handoff_prompt(
                    source,
                    target_harness=target_harness,
                ),
            }
            return self.proxy_create_harness_session(target_host_id, structured_payload)
        launch_payload: dict[str, Any] = {
            "harness": target_harness,
            "cwd": source.cwd,
            "repo_owner": source.repo_owner,
            "repo_name": source.repo_name,
            "branch": source.branch,
            "source_session_id": source.session_id,
            "handoff_mode": handoff_mode,
            "native_resume": native_resume,
            "rows": payload.get("rows") or 32,
            "cols": payload.get("cols") or 100,
        }
        if not native_resume or target_harness != source.harness:
            launch_payload["initial_input"] = self._build_handoff_prompt(
                source,
                target_harness=target_harness,
            )
            launch_payload["handoff_mode"] = "nexus_handoff"
        return self.proxy_create_harness_session(target_host_id, launch_payload)

    def harness_terminal_route(
        self, session_id: str
    ) -> tuple["HarnessHost", str] | None:
        """Resolve a terminal attach to its host + host-relative path.

        Runs the same reconcile/status/host checks
        ``harness_terminal_endpoint`` has always run; split out so callers
        that need the host object itself (to check relay liveness) don't
        have to re-derive it from a joined URL.
        """
        if not self._reconcile_harness_session_from_host(session_id):
            return None
        session = self._harness_session(session_id)
        if session is None:
            return None
        if str(session.status) not in {"created", "starting", "running"}:
            return None
        host = self._harness_host(session.host_id)
        if host is None:
            return None
        return host, f"/sessions/{session_id}/terminal"

    def harness_terminal_endpoint(self, session_id: str) -> str | None:
        route = self.harness_terminal_route(session_id)
        if route is None:
            return None
        host, path = route
        endpoint = _harness_endpoint(host)
        if not endpoint:
            return None
        return f"{endpoint}{path}"

    def _reconcile_harness_session_from_host(self, session_id: str) -> bool:
        session = self._harness_session(session_id)
        if session is None:
            return False
        if str(session.status) not in {"created", "starting", "running"}:
            return True
        host = self._harness_host(session.host_id)
        if host is None:
            return True
        # 1.0s: this gates every terminal attach, so it must fail fast on a
        # hung host; the sub-second-only rule for RelayManager.request
        # permits any timeout >= 1s.
        status, body = self._harness_request(
            host,
            f"/sessions/{session_id}",
            method="GET",
            payload={},
            timeout_s=1.0,
        )
        if 200 <= status < 300:
            return True
        if status == 404:
            self._mark_harness_session_missing_on_host(session_id)
            return False
        log.warning(
            "failed to reconcile harness session %s from %s: %s %s",
            session_id,
            host.host_id,
            status,
            body.strip()[:300],
        )
        return True

    def _tombstone_stale_harness_session(self, session_id: str) -> None:
        # Mirrors _sync_terminated_harness_session's terminal status so the
        # row buckets as done on clients, but records why the daemon never
        # acknowledged the terminate.
        try:
            HarnessRegistry(self.duckdb_path).update_session_status(
                session_id,
                "terminated",
                ended_at=datetime.now(timezone.utc),
                last_error="session missing on host; tombstoned by central terminate",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "failed to tombstone stale harness session %s: %s", session_id, exc
            )

    def _mark_harness_session_missing_on_host(self, session_id: str) -> None:
        try:
            HarnessRegistry(self.duckdb_path).update_session_status(
                session_id,
                "completed",
                ended_at=datetime.now(timezone.utc),
                last_error="host no longer has active PTY session; reconciled before terminal attach",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to mark stale harness session %s: %s", session_id, exc)

    def harness_snapshot(
        self,
        *,
        include_hosts: bool = True,
        include_sessions: bool = True,
        archived_limit: int | None = _UNSET_ARCHIVED_LIMIT,
    ) -> dict[str, Any]:
        from drover.server.cockpit.service import COCKPIT_SECTIONS

        source = Path(self.duckdb_path)
        if not source.exists():
            return {
                "cockpit_api_version": 1,
                "cockpit_sections": list(COCKPIT_SECTIONS),
                "hosts": [],
                "sessions": [],
                "error": f"DuckDB file does not exist: {source}",
            }
        try:
            # Query the live database rather than copying it: the file is
            # ~483MB and this runs on every fleet poll (measured 0.78s per
            # copy against a 5s poll = a 16% disk duty cycle per client, and
            # it grows with the store). Two indexed reads under the
            # registry's connect lock cost microseconds. Live reads beside
            # live writers are the supported path -- see
            # open_duckdb_connection's docstring.
            if archived_limit is _UNSET_ARCHIVED_LIMIT:
                archived_limit = self.archived_session_limit
            registry = HarnessRegistry(source)
            hosts = registry.list_hosts() if include_hosts else []
            sessions = (
                registry.list_sessions(archived_limit=archived_limit)
                if include_sessions
                else []
            )
            previews = registry.latest_session_previews(
                [session.session_id for session in sessions]
            )
            try:
                recaps = registry.latest_live_recaps(
                    [session.session_id for session in sessions]
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to load live session recaps: %s", exc)
                recaps = {}
            return {
                "cockpit_api_version": 1,
                "cockpit_sections": list(COCKPIT_SECTIONS),
                "hosts": [
                    _harness_host_dict(host, self.relay_manager) for host in hosts
                ],
                "sessions": [
                    _harness_session_dict(
                        session,
                        previews.get(session.session_id),
                        recaps.get(session.session_id),
                    )
                    for session in sessions
                ],
                "cwd_suggestions": _harness_cwd_suggestions(
                    sessions, self.favorite_cwds
                ),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to render harness snapshot: %s", exc)
            return {"hosts": [], "sessions": [], "error": str(exc)}

    def harness_session_snapshot(self, session_id: str) -> dict[str, Any]:
        source = Path(self.duckdb_path)
        if not source.exists():
            return {"error": f"DuckDB file does not exist: {source}"}
        try:
            self._reconcile_harness_session_from_host(session_id)
            # Live read, same reasoning as harness_snapshot above.
            registry = HarnessRegistry(source)
            session = registry.get_session(session_id)
            if session is None:
                return {"error": f"unknown harness session: {session_id}"}
            host = registry.get_host(session.host_id)
            events = registry.list_events(session_id)
            native_transcript: dict[str, Any] | None = None
            status, body = self.proxy_harness_native_transcript(session_id)
            if 200 <= status < 300:
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = {}
                if isinstance(parsed, dict):
                    native_transcript = parsed
            return {
                "session": session.__dict__,
                "host": host.__dict__ if host else None,
                "events": [event.__dict__ for event in events],
                "native_transcript": native_transcript,
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to render harness session %s: %s", session_id, exc)
            return {"error": str(exc)}

    def _harness_host(self, host_id: str) -> Any | None:
        try:
            registry = HarnessRegistry(self.duckdb_path)
            return registry.get_host(host_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to load harness host %s: %s", host_id, exc)
            return None

    def _harness_session(self, session_id: str) -> Any | None:
        try:
            registry = HarnessRegistry(self.duckdb_path)
            return registry.get_session(session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to load harness session %s: %s", session_id, exc)
            return None

    def _sync_created_harness_session(
        self,
        host_id: str,
        request_payload: Mapping[str, Any],
        response_body: str,
    ) -> None:
        try:
            payload = json.loads(response_body)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            return
        harness = str(
            payload.get("harness") or request_payload.get("harness") or "shell"
        )
        command = payload.get("command") or request_payload.get("command") or harness
        status = str(payload.get("status") or "running")
        mode = str(payload.get("mode") or request_payload.get("mode") or "pty")
        registry = HarnessRegistry(self.duckdb_path)
        try:
            existing = registry.get_session(session_id)
            if existing is None:
                registry.create_session(
                    session_id=session_id,
                    host_id=host_id,
                    harness=harness,
                    mode=mode,
                    command=_command_label(command),
                    repo_owner=_optional_str(request_payload.get("repo_owner")),
                    repo_name=_optional_str(request_payload.get("repo_name")),
                    branch=_optional_str(request_payload.get("branch")),
                    cwd=_optional_str(request_payload.get("cwd")),
                    status=status,
                    native_session_id=_native_session_id(
                        request_payload.get("native_resume")
                    ),
                    native_resume_label=_native_resume_label(
                        request_payload.get("native_resume")
                    ),
                    source_session_id=_optional_str(
                        request_payload.get("source_session_id")
                    ),
                    handoff_mode=_optional_str(request_payload.get("handoff_mode")),
                    permission_mode=_optional_str(
                        payload.get("permission_mode")
                        or request_payload.get("permission_mode")
                    ),
                    model=_optional_str(
                        payload.get("model") or request_payload.get("model")
                    ),
                    thinking_effort=_optional_str(
                        payload.get("thinking_effort")
                        or request_payload.get("thinking_effort")
                    ),
                )
            else:
                registry.update_session_status(session_id, status)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "failed to sync created harness session %s: %s", session_id, exc
            )

    def _sync_harness_session_preferences(
        self, session_id: str, payload: Mapping[str, Any]
    ) -> None:
        model = _optional_str(payload.get("model"))
        thinking_effort = _optional_str(payload.get("thinking_effort"))
        if model is None and thinking_effort is None:
            return
        try:
            HarnessRegistry(self.duckdb_path).update_session_preferences(
                session_id,
                model=model,
                thinking_effort=thinking_effort,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "failed to sync harness session preferences %s: %s",
                session_id,
                exc,
            )

    def _sync_terminated_harness_session(
        self,
        session_id: str,
        response_body: str,
    ) -> None:
        status = "terminated"
        try:
            payload = json.loads(response_body)
            if isinstance(payload, dict):
                status = str(payload.get("status") or status)
        except json.JSONDecodeError:
            pass
        try:
            HarnessRegistry(self.duckdb_path).update_session_status(
                session_id,
                status,
                ended_at=datetime.now(timezone.utc),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "failed to sync terminated harness session %s: %s", session_id, exc
            )

    def _build_handoff_prompt(self, source: Any, *, target_harness: str) -> str:
        # transcript_text replays harness_events, which is where every
        # session's conversation lives -- structured and PTY alike.
        try:
            registry = HarnessRegistry(self.duckdb_path)
            transcript = registry.transcript_text(source.session_id)
        except Exception:
            transcript = ""
        if len(transcript) > 4000:
            transcript = transcript[-4000:]
        lines = [
            "Continue this Drover Harness session in the current CLI.",
            "",
            f"Source session: {source.session_id}",
            f"Source harness: {source.harness}",
            f"Target harness: {target_harness}",
            f"Host: {source.host_id}",
            f"CWD: {source.cwd or '(not recorded)'}",
            f"Command: {source.command}",
            f"Status at handoff: {source.status}",
            "",
            "Use the context below to continue the work without asking me to restate it.",
        ]
        if transcript:
            lines.extend(["", "Recent transcript:", transcript])
        else:
            lines.extend(
                ["", "Recent transcript: not available in central Drover yet."]
            )
        lines.extend(["", "Start by briefly confirming what you are continuing."])
        return "\n".join(lines).strip() + "\n"

    def _harness_request(
        self,
        host: Any,
        path: str,
        *,
        method: str,
        payload: Mapping[str, Any] | None = None,
        timeout_s: float = 15,
        max_response_bytes: int | None = None,
    ) -> tuple[int, str]:
        """Single routing choke point for every hub->harnessd API call.

        Prefers a live relay connection for ``host``; falls back to a direct
        URL dial when no relay is live *and the host is not a relay host*;
        reports 502 when neither is available. ``path`` is host-relative (may
        include a query string) and is forwarded verbatim to both the relay
        and the direct-dial path -- callers must not pre-join it to an
        endpoint.
        """
        if self.relay_manager is not None and self.relay_manager.is_live(host.host_id):
            if max_response_bytes is not None:
                return self.relay_manager.request(
                    host.host_id,
                    method,
                    path,
                    dict(payload or {}),
                    timeout_s=max(timeout_s, RELAY_MIN_TIMEOUT_S),
                    max_response_bytes=max_response_bytes,
                )
            return self.relay_manager.request(
                host.host_id,
                method,
                path,
                dict(payload or {}),
                timeout_s=max(timeout_s, RELAY_MIN_TIMEOUT_S),
            )
        if getattr(host, "connection_kind", "direct") == "relay":
            # A relay host is behind NAT by definition: it has no meaningful
            # inbound URL, and its socket is the only way in. Falling through
            # to a dial would be actively dangerous rather than merely
            # useless, because the default listen address for every host
            # shape in this repo is 127.0.0.1:7081 -- so a stale or
            # mistakenly-set local_url on a relay row resolves against the
            # HUB's own loopback and silently runs the work laptop's commands
            # against the hub's harnessd instead. These are agent sessions
            # with filesystem access; being unreachable is the safe failure.
            return _json_response(
                502,
                {"error": f"relay host is not connected: {host.host_id}"},
            )
        endpoint = _harness_endpoint(host)
        if endpoint:
            return self._proxy_harness_request(
                f"{endpoint}{path}",
                method=method,
                payload=payload,
                timeout_s=timeout_s,
                max_response_bytes=max_response_bytes,
                host_id=getattr(host, "host_id", None),
            )
        return _json_response(
            502, {"error": f"harness host has no reachable endpoint: {host.host_id}"}
        )

    def _proxy_harness_request(
        self,
        url: str,
        *,
        method: str,
        payload: Mapping[str, Any] | None = None,
        timeout_s: float = 15,
        max_response_bytes: int | None = None,
        host_id: str | None = None,
    ) -> tuple[int, str]:
        body = json.dumps(dict(payload or {}), sort_keys=True)
        parsed = urlparse(url)
        if parsed.scheme != "http" or not parsed.hostname:
            return _json_response(502, {"error": f"unsupported harness URL: {url}"})
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        port = parsed.port or 80
        # A bare URL names a tailnet address, which reads as a machine but is
        # not one the reader can map back to a host (issue #222 -- it sent an
        # investigation after the wrong box). Every failure here says which
        # host it was talking to.
        target_label = f"{host_id} ({url})" if host_id else url
        try:
            conn = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout_s)
            headers = {"Content-Type": "application/json"}
            if self.api_token:
                headers["Authorization"] = f"Bearer {self.api_token}"
            conn.request(
                method,
                path,
                body=body.encode("utf-8") if method != "GET" else None,
                headers=headers,
            )
            response = conn.getresponse()
            return response.status, _read_bounded_http_body(
                response, max_response_bytes=max_response_bytes
            )
        except TimeoutError as exc:
            # Distinct from unreachable, because the host answered and is
            # still working. A create that overran this budget left the daemon
            # writing its reply into a closed socket (BrokenPipeError at
            # daemon.py:2135, after the session existed), and the app told the
            # user to try again -- the one thing that can leave two sessions
            # where they asked for one.
            log.warning(
                "harness request to %s timed out after %ss; the host may still "
                "complete it",
                target_label,
                timeout_s,
            )
            return _json_response(
                504,
                {
                    "error": (
                        f"harness host did not answer within {timeout_s:g}s: {exc}. "
                        "It may still be working, so a session may have been "
                        "created; check the sessions list before retrying."
                    )
                },
            )
        except OSError as exc:
            return _json_response(502, {"error": f"harness host request failed: {exc}"})
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to proxy harness request to %s: %s", target_label, exc)
            return _json_response(502, {"error": str(exc)})
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def warm(self) -> None:
        """Build the first render before anyone asks for it.

        Cold, a refresh copies the whole database and then reads a parquet
        tree DuckDB has never globbed in this instance: 35.6s measured on the
        Mac hub after a restart, against 7-15s warm (#78). Whoever scrapes
        first should not be the one paying that, so the server pays it itself
        -- the same bargain ``_warm_cockpit`` already makes.
        """
        self._refresh_if_needed()

    def _refresh_if_needed(self) -> None:
        now = time.monotonic()
        if self._cached_text is not None:
            if now < self._cached_until:
                return
            if now < self._cached_until + self.max_stale_seconds:
                # Expired but still usable. A rebuild is seconds of DuckDB
                # work; making the scraper that happens to arrive first wait
                # for it is what turned a 60s TTL into 7-15s scrapes. Hand
                # back the previous render and rebuild behind the request.
                self._refresh_in_background()
                return
        with self._lock:
            now = time.monotonic()
            if self._cached_text is not None and now < self._cached_until:
                return
            self._rebuild()

    def _refresh_in_background(self) -> None:
        """Rebuild off the request path, at most one rebuild at a time."""
        with self._refresh_guard:
            if self._refreshing:
                return
            self._refreshing = True

        def run() -> None:
            try:
                with self._lock:
                    self._rebuild()
            except Exception as exc:  # noqa: BLE001 - a scrape must not die here
                # The cache keeps its old timestamp, so the next scrape tries
                # again, and once the staleness window closes the failure
                # surfaces to the caller instead of being served as data.
                log.warning("background metrics refresh failed: %s", exc)
            finally:
                with self._refresh_guard:
                    self._refreshing = False

        threading.Thread(target=run, name="drover-metrics-refresh", daemon=True).start()

    def _rebuild(self) -> None:
        """Render every cached payload. Callers must hold ``self._lock``."""
        snapshot = self._quality_snapshot()
        lines = [format_prometheus(snapshot).rstrip()]
        _append_details_metrics(lines, snapshot)
        _append_operational_health_metrics(lines, self.duckdb_path, snapshot)
        _append_summarizer_metrics(lines, self.summarizer_report)
        _append_redis_metrics(lines, self.job_streams)
        _append_adoption_metrics(lines, snapshot)
        _append_harness_metrics(lines)
        _append_advisory_metrics(lines)
        _append_usage_rollup_metrics(lines)
        _append_analytics_gate_metrics(lines)
        observatory = self._observatory_snapshot(snapshot)
        redis_streams = _redis_stream_snapshots(self.job_streams)
        self._cached_text = "\n".join(lines) + "\n"
        self._cached_json = (
            json.dumps(
                {
                    "quality": snapshot,
                    "observatory": observatory,
                    "redis_streams": redis_streams,
                    "summarizer": dict(self.summarizer_report),
                    "redis_queues": sorted(self.job_streams),
                },
                sort_keys=True,
                default=str,
            )
            + "\n"
        )
        # Timed from when the render landed, not from when it started: the
        # rebuild itself takes seconds, and charging those to the TTL would
        # expire a render that is only just finished.
        self._cached_until = time.monotonic() + self.ttl_seconds

    def _quality_snapshot(self) -> dict:
        source = Path(self.duckdb_path)
        if not source.exists():
            return quality_snapshot(
                duckdb_path=source,
                incoming_dir=self.incoming_dir,
                deep=False,
            )
        # Deliberately a COPY, unlike harness_snapshot's live read.
        #
        # This is isolation, not caching. quality_snapshot is a heavy
        # analytical scan -- 19.4s measured against a 686MB store, and it
        # spills ~175MB to duckdb_temp_storage. Pointed at the live file it
        # shares one DuckDB instance with the harness registry: it saturates
        # the task scheduler and starves every other DB-backed endpoint, so
        # /harness timed out for minutes while /healthz stayed instant.
        # A separate file is a separate instance, which is the entire point.
        #
        # Do NOT "optimize" this into a live read to save the copy. That was
        # tried (927e446, 2026-08-04) and is exactly what caused the outage.
        # The copy costs ~0.9s behind a 60s TTL; the live read cost the fleet.
        #
        # Owning the instance is also what earns role="snapshot": DuckDB's
        # `threads` is instance-wide, so only a reader with its own instance
        # can raise it without touching the live one. Measured 13.7-14.8s at
        # threads=1 vs 6.6-8.1s here (#78).
        with tempfile.TemporaryDirectory(
            prefix="drover-metrics-", dir=snapshot_scratch_root(source)
        ) as tmp:
            snapshot = Path(tmp) / source.name
            copy_duckdb_store(source, snapshot)
            return quality_snapshot(
                duckdb_path=snapshot,
                incoming_dir=self.incoming_dir,
                deep=False,
                role="snapshot",
            )

    def _observatory_snapshot(self, quality: dict) -> dict:
        audit = quality.get("runtime_audit", {})
        source = Path(self.duckdb_path)
        if not source.exists():
            return {}
        try:
            # Copy for the same isolation reason as _quality_snapshot above.
            # This one is fast on its own (0.23s measured), but it runs in the
            # same refresh and must not add live-instance contention either.
            with tempfile.TemporaryDirectory(
                prefix="drover-observatory-", dir=snapshot_scratch_root(source)
            ) as tmp:
                snapshot = Path(tmp) / source.name
                copy_duckdb_store(source, snapshot)
                return pipeline_observatory_snapshot(
                    duckdb_path=snapshot,
                    runtime_audit=audit,
                    max_artifacts=10,
                    max_projects=10,
                    role="snapshot",
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to render observatory drilldown: %s", exc)
            return {"error": str(exc)}


from drover.server.web.app import (  # noqa: E402,F401 - compat re-export
    start_metrics_server,
)
