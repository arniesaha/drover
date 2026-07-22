"""Ingest OTLP gRPC trace requests into the spans Parquet partition.

For each span:
  1. Convert request → Tempo-style trace dict (proto_adapter).
  2. Parse via parse_agentweave_trace → list[row].
  3. Compute dedup_key = sha256(trace_id|span_id)[:32].
  4. Drop spans whose dedup_key already exists in the spans view.
  5. Group survivors by date(start_time) → write one Parquet file per
     (date) partition.
  6. Upsert tasks rows from spans that carry repo metadata.

Idempotent: re-ingesting the same request produces zero new rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from drover.event_identity import canonical_agent_events_cte
from drover.parsers import parse_agentweave_trace
from drover.server.parquet_io import atomic_write_table
from drover.attribution import derive_repo_attribution
from drover.server import ledger_shadow
from drover.server.db import open_duckdb_connection
from drover.server.otlp.proto_adapter import otlp_request_to_trace_dict
from drover.task_id import compute_task_id

log = logging.getLogger("drover.otlp.ingest")

# Columns in the order they live in the spans Parquet seed.
_SPANS_COLUMNS = (
    "trace_id",
    "span_id",
    "parent_span_id",
    "name",
    "service_name",
    "start_time",
    "end_time",
    "duration_ms",
    "harness",
    "session_id",
    "session_key",
    "task_id",
    "agent_id",
    "agent_type",
    "agent_model",
    "associated_with",
    "activity_type",
    "parent_session_id",
    "project",
    "cwd",
    "repository",
    "task_label",
    "llm_provider",
    "llm_model",
    "stop_reason",
    "repo_owner",
    "repo_name",
    "branch",
    "principal_id",
    "routing_provider",
    "routing_model",
    "routing_reason",
    "redaction_level",
    "sensitivity",
    "prompt_preview",
    "response_preview",
    "preview_truncated",
    "preview_bytes",
    "cost_usd",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "attributes_json",
    "raw_object_uri",
    "dedup_key",
)


@dataclass
class OTLPIngestStats:
    read: int = 0
    inserted: int = 0
    skipped_dupes: int = 0
    errors: int = 0
    ledger_receipts: int = 0


def _make_span_dedup_key(trace_id: str | None, span_id: str | None) -> str:
    raw = f"{trace_id or ''}|{span_id or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _existing_dedup_keys(con) -> set[str]:
    try:
        rows = con.execute(
            "SELECT dedup_key FROM spans WHERE dedup_key IS NOT NULL"
        ).fetchall()
        return {r[0] for r in rows}
    except duckdb.Error:
        return set()


def _session_repo_map(
    con: duckdb.DuckDBPyConnection, session_ids: set[str]
) -> dict[str, tuple]:
    """Return {session_id: (repo_owner, repo_name, branch)} from agent_events."""
    if not session_ids:
        return {}
    placeholders = ", ".join("?" * len(session_ids))
    try:
        rows = con.execute(
            f"""WITH {canonical_agent_events_cte()}
                SELECT session_id,
                       MAX(repo_owner) AS repo_owner,
                       MAX(repo_name)  AS repo_name,
                       MAX(branch)     AS branch
                FROM canonical_agent_events
               WHERE session_id IN ({placeholders})
                 AND repo_owner IS NOT NULL
               GROUP BY session_id""",
            list(session_ids),
        ).fetchall()
    except duckdb.Error:
        return {}
    return {sid: (owner, name, branch) for sid, owner, name, branch in rows if owner}


def _normalize_row(row: dict, session_repo_map: dict | None = None) -> dict:
    """Coerce parser output into the spans seed schema."""
    repo_owner = row.get("repo_owner")
    repo_name = row.get("repo_name")
    branch = row.get("branch")
    # AgentWeave parser exposes prov.repo.* under different keys; mirror them
    # if the dedicated columns weren't populated.
    attrs = row.get("attributes_json") or {}
    if isinstance(attrs, dict):
        repo_owner = repo_owner or attrs.get("prov.repo.owner")
        repo_name = repo_name or attrs.get("prov.repo.name")
        branch = branch or attrs.get("prov.git.branch")
        attr = derive_repo_attribution(
            {
                **attrs,
                "_repo_owner": repo_owner,
                "_repo_name": repo_name,
                "gitBranch": branch,
            }
        )
        repo_owner = repo_owner or attr.repo_owner
        repo_name = repo_name or attr.repo_name
        branch = branch or attr.branch
    # Fall back to repo info derived from the session's agent_events.
    if not (repo_owner and repo_name) and session_repo_map:
        fallback = session_repo_map.get(row.get("session_id") or "")
        if fallback:
            repo_owner = repo_owner or fallback[0]
            repo_name = repo_name or fallback[1]
            branch = branch or fallback[2]

    out: dict[str, Any] = {col: None for col in _SPANS_COLUMNS}
    for col in _SPANS_COLUMNS:
        if col in row:
            out[col] = row[col]

    out["repo_owner"] = repo_owner
    out["repo_name"] = repo_name
    out["branch"] = branch
    out["task_id"] = compute_task_id(None, repo_owner, repo_name, branch)
    out["dedup_key"] = _make_span_dedup_key(row.get("trace_id"), row.get("span_id"))
    out["principal_id"] = row.get("principal_id")
    # attributes_json must be serialized to fit the string column
    if isinstance(out["attributes_json"], dict):
        out["attributes_json"] = json.dumps(out["attributes_json"], default=str)
    elif out["attributes_json"] is None:
        out["attributes_json"] = "{}"
    return out


def _coerce_to_arrow(rows: list[dict]) -> pa.Table:
    """Build a PyArrow table whose schema matches the spans seed."""
    schema = pa.schema(
        [
            ("trace_id", pa.string()),
            ("span_id", pa.string()),
            ("parent_span_id", pa.string()),
            ("name", pa.string()),
            ("service_name", pa.string()),
            ("start_time", pa.timestamp("us", tz="UTC")),
            ("end_time", pa.timestamp("us", tz="UTC")),
            ("duration_ms", pa.float64()),
            ("harness", pa.string()),
            ("session_id", pa.string()),
            ("session_key", pa.string()),
            ("task_id", pa.string()),
            ("agent_id", pa.string()),
            ("agent_type", pa.string()),
            ("agent_model", pa.string()),
            ("associated_with", pa.string()),
            ("activity_type", pa.string()),
            ("parent_session_id", pa.string()),
            ("project", pa.string()),
            ("cwd", pa.string()),
            ("repository", pa.string()),
            ("task_label", pa.string()),
            ("llm_provider", pa.string()),
            ("llm_model", pa.string()),
            ("stop_reason", pa.string()),
            ("repo_owner", pa.string()),
            ("repo_name", pa.string()),
            ("branch", pa.string()),
            ("principal_id", pa.string()),
            ("routing_provider", pa.string()),
            ("routing_model", pa.string()),
            ("routing_reason", pa.string()),
            ("redaction_level", pa.string()),
            ("sensitivity", pa.string()),
            ("prompt_preview", pa.string()),
            ("response_preview", pa.string()),
            ("preview_truncated", pa.bool_()),
            ("preview_bytes", pa.int64()),
            ("cost_usd", pa.float64()),
            ("prompt_tokens", pa.int64()),
            ("completion_tokens", pa.int64()),
            ("total_tokens", pa.int64()),
            ("cache_read_tokens", pa.int64()),
            ("cache_write_tokens", pa.int64()),
            ("attributes_json", pa.string()),
            ("raw_object_uri", pa.string()),
            ("dedup_key", pa.string()),
        ]
    )
    cols: dict[str, list] = {col: [] for col in _SPANS_COLUMNS}
    for r in rows:
        for col in _SPANS_COLUMNS:
            cols[col].append(r.get(col))
    return pa.table(cols, schema=schema)


def _write_partition(rows: list[dict], parquet_dir: Path) -> None:
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        st = r["start_time"]
        date = st.strftime("%Y-%m-%d") if hasattr(st, "strftime") else "unknown"
        grouped.setdefault(date, []).append(r)

    for date, part_rows in grouped.items():
        out_dir = parquet_dir / "spans" / f"date={date}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"part-{uuid.uuid4().hex[:12]}.parquet"
        table = _coerce_to_arrow(part_rows)
        atomic_write_table(table, out_path, compression="zstd")


def _upsert_tasks(con, rows: list[dict]) -> None:
    seen: dict[str, dict] = {}
    for r in rows:
        tid = r.get("task_id")
        if not tid:
            continue
        last = seen.get(tid)
        if last is None or (
            r.get("start_time") and r["start_time"] > last["last_activity_at"]
        ):
            seen[tid] = {
                "task_id": tid,
                "repo_owner": r.get("repo_owner"),
                "repo_name": r.get("repo_name"),
                "branch": r.get("branch"),
                "principal_id": r.get("principal_id"),
                "last_activity_at": r.get("start_time"),
                "title": None,
            }
        # Use task_label as title if present and not yet captured.
        if not seen[tid]["title"] and r.get("task_label"):
            seen[tid]["title"] = str(r["task_label"])[:120]
    for tid, t in seen.items():
        con.execute(
            """
            INSERT INTO tasks (task_id, repo_owner, repo_name, branch, principal_id,
                               status, created_at, last_activity_at, session_count, total_cost_usd,
                               title)
            VALUES (?, ?, ?, ?, ?, 'open', now(), ?, 0, 0.0, ?)
            ON CONFLICT (task_id) DO UPDATE SET
              last_activity_at = greatest(tasks.last_activity_at, EXCLUDED.last_activity_at),
              title = COALESCE(tasks.title, EXCLUDED.title)
            """,
            [
                t["task_id"],
                t["repo_owner"],
                t["repo_name"],
                t["branch"],
                t["principal_id"],
                t["last_activity_at"],
                t["title"],
            ],
        )


def ingest_otlp_request(
    request: ExportTraceServiceRequest,
    *,
    parquet_dir: Path,
    duckdb_path: Path,
    raw_object_uri: str = "otlp://stream",
    span_job_stream: object | None = None,
) -> OTLPIngestStats:
    """Ingest one OTLP trace export request. Returns OTLPIngestStats."""
    parquet_dir = Path(parquet_dir)
    duckdb_path = Path(duckdb_path)
    stats = OTLPIngestStats()

    trace_dict = otlp_request_to_trace_dict(request)
    raw_rows = parse_agentweave_trace(trace_dict, raw_object_uri=raw_object_uri)

    # parse_agentweave_trace silently drops spans without start_time. Count
    # the input span population for `read`.
    total_spans = sum(
        len(ss.get("spans", []) or [])
        for batch in trace_dict.get("batches", [])
        for ss in (
            batch.get("scopeSpans", [])
            or batch.get("instrumentationLibrarySpans", [])
            or []
        )
    )
    stats.read = total_spans

    if not raw_rows:
        return stats

    con = open_duckdb_connection(duckdb_path)
    try:
        existing = _existing_dedup_keys(con)
        session_ids = {r.get("session_id") for r in raw_rows if r.get("session_id")}
        repo_map = _session_repo_map(con, session_ids)
        new_rows: list[dict] = []
        for raw in raw_rows:
            row = _normalize_row(raw, session_repo_map=repo_map)
            if row["dedup_key"] in existing:
                stats.skipped_dupes += 1
                continue
            existing.add(row["dedup_key"])
            new_rows.append(row)

        if new_rows:
            _write_partition(new_rows, parquet_dir)
            _upsert_tasks(con, new_rows)
            for row in new_rows:
                span_id = row.get("span_id")
                if span_id:
                    con.execute(
                        """INSERT INTO span_embed_jobs (span_id, status, attempts)
                           VALUES (?, 'pending', 0)
                           ON CONFLICT (span_id) DO NOTHING""",
                        [span_id],
                    )
                    if span_job_stream is not None:
                        span_job_stream.add({"span_id": str(span_id)})
                # Shadow-write a durable receipt per accepted span (AGE-44). The
                # span dedup_key is the durable identity, so a re-arriving span is
                # a ledger no-op. Best-effort; never blocks the parquet write.
                result = ledger_shadow.record_receipt(
                    con,
                    source_kind="otlp_span",
                    source_key=row["dedup_key"],
                    subject_kind="span",
                    subject_key=span_id,
                    payload_hash=row["dedup_key"],
                )
                if result is not None and not result.is_duplicate:
                    stats.ledger_receipts += 1
            stats.inserted = len(new_rows)
    finally:
        con.close()

    return stats
