#!/usr/bin/env python3
"""
Nexus GCP → Local DuckDB Migration
====================================
Converts raw BQ CSV exports into a normalized DuckDB + Parquet lakehouse.

Schema design:
  - agent_events   : one row per conversation turn, with promoted raw_data fields
  - spans          : one row per OTel span, with promoted attributes_json fields
  - sessions       : derived session-level summary (aggregated from agent_events)
  - pr_events      : deduplicated PR link events (session → GitHub PR)
  - routing        : Mux routing decisions extracted from spans.attributes_json

Parquet layout:
  nexus/parquet/
    agent_events/date=YYYY-MM-DD/agent_id=<id>/part-0.parquet
    spans/date=YYYY-MM-DD/part-0.parquet
    sessions/part-0.parquet
    pr_events/part-0.parquet
    routing/part-0.parquet

Run:
  python migrate_to_duckdb.py [--dry-run] [--skip-parquet]
"""

import csv
import json
import os
import re
import sys
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

# ── Config ────────────────────────────────────────────────────────────────────
# Defaults point at ~/.nexus/ (the steady-state lakehouse), not the legacy
# repo-root paths the original script used. Override with --source-dir /
# --output-dir / --db-path. See docs/superpowers/plans/2026-05-09-nexus-doctor-and-decom.md.

_HOME = Path.home()
BACKUP_DIR = Path("/Users/arnabmac/jenny/nexus/local_backup")
OUTPUT_DIR = _HOME / ".nexus" / "parquet"
DB_PATH    = _HOME / ".nexus" / "nexus.duckdb"

# BQ-noisy files have garbage lines before the CSV header
NOISY_HEADER_FILES = {
    "agent_events_2026-01.csv",
    "agent_events_2026-02.csv",
    "agent_events_2026-03.csv",
}

# Files with 0 bytes or only BQ noise — skip
SKIP_FILES = {
    "agent_events_2026-04.csv",   # 0 bytes
    "agent_events_2026-05.csv",   # 0 bytes
    "agent_events_2026-05b.csv",  # 0 bytes
}

AGENT_EVENTS_FILES = sorted([
    f for f in os.listdir(BACKUP_DIR)
    if f.startswith("agent_events_") and f.endswith(".csv")
    and f not in SKIP_FILES
])

SPANS_FILE = BACKUP_DIR / "spans.csv"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nexus-migrate")

# ── Helpers ───────────────────────────────────────────────────────────────────

csv.field_size_limit(10_000_000)

WORKTREE_RE = re.compile(r"/\.claude/worktrees/agent-[a-f0-9]+")

def parse_git_repo(cwd: str) -> str:
    """Extract repo name from cwd path, skipping Claude Code worktree segments."""
    if not cwd:
        return ""
    # Strip worktree suffix: /path/to/repo/.claude/worktrees/agent-abc123 → /path/to/repo
    clean = WORKTREE_RE.sub("", cwd).rstrip("/")
    parts = clean.split("/")
    # Walk back to find a non-empty, meaningful segment
    for part in reversed(parts):
        if part and part not in ("", ".", ".."):
            return part
    return ""


def parse_raw_data(raw: str) -> dict:
    """Safely parse raw_data JSON blob."""
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def parse_attributes_json(attr: str) -> dict:
    """Parse spans.attributes_json — may be double-encoded."""
    if not attr:
        return {}
    try:
        # Try direct parse first
        d = json.loads(attr)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    try:
        # Double-encoded: strip surrounding quotes and unescape
        s = attr.strip('"').replace('\\"', '"').replace("\\\\", "\\")
        d = json.loads(s)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def str_or_none(v) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s if s else None


def float_or_none(v) -> float | None:
    try:
        return float(v) if v not in (None, "", "None") else None
    except (ValueError, TypeError):
        return None


def int_or_none(v) -> int | None:
    try:
        return int(v) if v not in (None, "", "None") else None
    except (ValueError, TypeError):
        return None


def parse_ts(s: str) -> str | None:
    """Normalize timestamp to ISO8601 UTC string."""
    if not s:
        return None
    # Already clean: "2026-04-01 02:32:12"
    s = s.strip().rstrip("Z")
    if "T" in s:
        s = s.replace("T", " ")
    return s


def open_agent_events_csv(path: Path):
    """Open an agent_events CSV, skipping BQ noise lines before the header."""
    filename = path.name
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        if filename in NOISY_HEADER_FILES:
            # Scan for the real header line
            for line in fh:
                if line.startswith("id,session_id"):
                    # Now fh is positioned right after the header
                    # Reconstruct a reader by re-reading with this header
                    break
            # Re-open and skip to the header line position
        else:
            pass

    # Re-open cleanly and seek to header
    fh = open(path, newline="", encoding="utf-8", errors="replace")
    if filename in NOISY_HEADER_FILES:
        for line in fh:
            if line.startswith("id,session_id"):
                # Put back the header by prepending to a chain reader
                import itertools
                reader = csv.DictReader(itertools.chain([line], fh))
                return fh, reader
        # Fallback
        fh.seek(0)
    reader = csv.DictReader(fh)
    return fh, reader


# ── Arrow Schemas ─────────────────────────────────────────────────────────────

AGENT_EVENTS_SCHEMA = pa.schema([
    pa.field("id",              pa.string()),
    pa.field("session_id",      pa.string()),
    pa.field("timestamp",       pa.string()),   # stored as string, cast in DuckDB
    pa.field("date",            pa.string()),   # partition key YYYY-MM-DD
    pa.field("agent_id",        pa.string()),
    pa.field("event_type",      pa.string()),
    pa.field("role",            pa.string()),
    pa.field("content",         pa.string()),
    # Promoted from raw_data
    pa.field("git_repo",        pa.string()),
    pa.field("git_branch",      pa.string()),
    pa.field("cwd",             pa.string()),
    pa.field("slug",            pa.string()),
    pa.field("entrypoint",      pa.string()),
    pa.field("prompt_id",       pa.string()),
    pa.field("request_id",      pa.string()),
    pa.field("sub_agent_id",    pa.string()),
    pa.field("permission_mode", pa.string()),
    pa.field("stop_reason",     pa.string()),
    pa.field("is_api_error",    pa.bool_()),
    pa.field("is_sidechain",    pa.bool_()),
    pa.field("parent_uuid",     pa.string()),
    pa.field("message_uuid",    pa.string()),
    # Keep raw blob for anything not promoted
    pa.field("raw_data",        pa.string()),
])

SPANS_SCHEMA = pa.schema([
    pa.field("trace_id",            pa.string()),
    pa.field("span_id",             pa.string()),
    pa.field("parent_span_id",      pa.string()),
    pa.field("name",                pa.string()),
    pa.field("service_name",        pa.string()),
    pa.field("start_time",          pa.string()),
    pa.field("end_time",            pa.string()),
    pa.field("date",                pa.string()),   # partition key
    pa.field("duration_ms",         pa.float64()),
    pa.field("activity_type",       pa.string()),
    pa.field("agent_id",            pa.string()),
    pa.field("agent_type",          pa.string()),
    pa.field("session_id",          pa.string()),
    pa.field("parent_session_id",   pa.string()),
    pa.field("project",             pa.string()),
    pa.field("task_label",          pa.string()),
    pa.field("llm_provider",        pa.string()),
    pa.field("llm_model",           pa.string()),
    pa.field("prompt_tokens",       pa.int64()),
    pa.field("completion_tokens",   pa.int64()),
    pa.field("total_tokens",        pa.int64()),
    pa.field("cache_read_tokens",   pa.int64()),
    pa.field("cache_write_tokens",  pa.int64()),
    pa.field("cost_usd",            pa.float64()),
    pa.field("prompt_preview",      pa.string()),
    pa.field("response_preview",    pa.string()),
    pa.field("stop_reason",         pa.string()),
    pa.field("agent_model",         pa.string()),
    pa.field("associated_with",     pa.string()),
    pa.field("ingested_at",         pa.string()),
    # Promoted from attributes_json
    pa.field("route_requested_model",  pa.string()),
    pa.field("route_resolved_model",   pa.string()),
    pa.field("route_reason",           pa.string()),
    pa.field("route_runtime",          pa.string()),
    pa.field("route_message_count",    pa.int64()),
    pa.field("route_provider_id",      pa.string()),
    pa.field("cache_hit_rate",         pa.float64()),
    pa.field("hook_source",            pa.string()),
    # Keep raw blob
    pa.field("attributes_json",     pa.string()),
])

PR_EVENTS_SCHEMA = pa.schema([
    pa.field("session_id",    pa.string()),
    pa.field("pr_number",     pa.string()),
    pa.field("pr_url",        pa.string()),
    pa.field("pr_repository", pa.string()),
    pa.field("timestamp",     pa.string()),
    pa.field("agent_id",      pa.string()),
])

ROUTING_SCHEMA = pa.schema([
    pa.field("trace_id",          pa.string()),
    pa.field("span_id",           pa.string()),
    pa.field("start_time",        pa.string()),
    pa.field("date",              pa.string()),
    pa.field("agent_id",          pa.string()),
    pa.field("session_id",        pa.string()),
    pa.field("project",           pa.string()),
    pa.field("requested_model",   pa.string()),
    pa.field("resolved_model",    pa.string()),
    pa.field("reason",            pa.string()),
    pa.field("runtime",           pa.string()),
    pa.field("message_count",     pa.int64()),
    pa.field("provider_id",       pa.string()),
    pa.field("duration_ms",       pa.float64()),
    pa.field("cost_usd",          pa.float64()),
])


# ── Row Processors ────────────────────────────────────────────────────────────

def process_agent_event_row(row: dict) -> dict | None:
    """Parse one agent_events CSV row into a normalized dict."""
    event_id = str_or_none(row.get("id"))
    if not event_id:
        return None

    raw_str  = row.get("raw_data", "")
    rd       = parse_raw_data(raw_str)
    ts       = parse_ts(row.get("timestamp", ""))
    date     = ts[:10] if ts and len(ts) >= 10 else ""
    cwd      = str_or_none(rd.get("cwd") or "")
    git_repo = parse_git_repo(cwd or "")
    branch   = str_or_none(rd.get("gitBranch") or "")

    # stop_reason lives in system events inside raw_data
    msg = rd.get("message", {})
    stop_reason = None
    if isinstance(msg, dict):
        stop_reason = str_or_none(msg.get("stop_reason") or "")
    if not stop_reason:
        stop_reason = str_or_none(rd.get("stopReason") or "")

    return {
        "id":              event_id,
        "session_id":      str_or_none(row.get("session_id")),
        "timestamp":       ts,
        "date":            date,
        "agent_id":        str_or_none(row.get("agent_id")),
        "event_type":      str_or_none(row.get("event_type")),
        "role":            str_or_none(row.get("role")),
        "content":         str_or_none(row.get("content")),
        "git_repo":        git_repo or None,
        "git_branch":      branch,
        "cwd":             cwd,
        "slug":            str_or_none(rd.get("slug") or ""),
        "entrypoint":      str_or_none(rd.get("entrypoint") or ""),
        "prompt_id":       str_or_none(rd.get("promptId") or ""),
        "request_id":      str_or_none(rd.get("requestId") or ""),
        "sub_agent_id":    str_or_none(rd.get("agentId") or ""),
        "permission_mode": str_or_none(rd.get("permissionMode") or ""),
        "stop_reason":     stop_reason,
        "is_api_error":    bool(rd.get("isApiErrorMessage", False)),
        "is_sidechain":    bool(rd.get("isSidechain", False)),
        "parent_uuid":     str_or_none(rd.get("parentUuid") or ""),
        "message_uuid":    str_or_none(rd.get("uuid") or ""),
        "raw_data":        raw_str or None,
    }


def process_pr_event(row: dict) -> dict | None:
    """Extract pr-link event into a deduplicated PR record."""
    raw_str = row.get("raw_data", "")
    rd      = parse_raw_data(raw_str)
    if row.get("event_type") != "pr-link":
        return None
    pr_num = str_or_none(rd.get("prNumber") or "")
    pr_url = str_or_none(rd.get("prUrl") or "")
    pr_repo = str_or_none(rd.get("prRepository") or "")
    sess_id = str_or_none(rd.get("sessionId") or row.get("session_id") or "")
    ts      = parse_ts(rd.get("timestamp") or row.get("timestamp") or "")
    if not pr_num:
        return None
    return {
        "session_id":    sess_id,
        "pr_number":     pr_num,
        "pr_url":        pr_url,
        "pr_repository": pr_repo,
        "timestamp":     ts,
        "agent_id":      str_or_none(row.get("agent_id")),
    }


def process_span_row(row: dict) -> tuple[dict | None, dict | None]:
    """
    Parse one spans CSV row.
    Returns (span_record, routing_record_or_None).
    """
    trace_id = str_or_none(row.get("trace_id"))
    if not trace_id:
        return None, None

    attrs    = parse_attributes_json(row.get("attributes_json", ""))
    st       = parse_ts(row.get("start_time", ""))
    date     = st[:10] if st and len(st) >= 10 else ""

    span = {
        "trace_id":           trace_id,
        "span_id":            str_or_none(row.get("span_id")),
        "parent_span_id":     str_or_none(row.get("parent_span_id")),
        "name":               str_or_none(row.get("name")),
        "service_name":       str_or_none(row.get("service_name")),
        "start_time":         st,
        "end_time":           parse_ts(row.get("end_time", "")),
        "date":               date,
        "duration_ms":        float_or_none(row.get("duration_ms")),
        "activity_type":      str_or_none(row.get("activity_type")),
        "agent_id":           str_or_none(row.get("agent_id")),
        "agent_type":         str_or_none(row.get("agent_type")),
        "session_id":         str_or_none(row.get("session_id")),
        "parent_session_id":  str_or_none(row.get("parent_session_id")),
        "project":            str_or_none(row.get("project")),
        "task_label":         str_or_none(row.get("task_label")),
        "llm_provider":       str_or_none(row.get("llm_provider")),
        "llm_model":          str_or_none(row.get("llm_model")),
        "prompt_tokens":      int_or_none(row.get("prompt_tokens")),
        "completion_tokens":  int_or_none(row.get("completion_tokens")),
        "total_tokens":       int_or_none(row.get("total_tokens")),
        "cache_read_tokens":  int_or_none(row.get("cache_read_tokens")),
        "cache_write_tokens": int_or_none(row.get("cache_write_tokens")),
        "cost_usd":           float_or_none(row.get("cost_usd")),
        "prompt_preview":     str_or_none(row.get("prompt_preview")),
        "response_preview":   str_or_none(row.get("response_preview")),
        "stop_reason":        str_or_none(row.get("stop_reason")),
        "agent_model":        str_or_none(row.get("agent_model")),
        "associated_with":    str_or_none(row.get("associated_with")),
        "ingested_at":        parse_ts(row.get("ingested_at", "")),
        # Promoted from attributes_json
        "route_requested_model": str_or_none(attrs.get("prov.route.requested_model") or ""),
        "route_resolved_model":  str_or_none(attrs.get("prov.route.resolved_model") or ""),
        "route_reason":          str_or_none(attrs.get("prov.route.reason") or ""),
        "route_runtime":         str_or_none(attrs.get("prov.route.runtime") or ""),
        "route_message_count":   int_or_none(attrs.get("prov.route.message_count")),
        "route_provider_id":     str_or_none(attrs.get("prov.route.provider_id") or ""),
        "cache_hit_rate":        float_or_none(attrs.get("cache.hit_rate")),
        "hook_source":           str_or_none(attrs.get("prov.hook.source") or ""),
        "attributes_json":       row.get("attributes_json") or None,
    }

    # Build routing record if Mux routing data present
    routing = None
    if attrs.get("prov.route.requested_model"):
        routing = {
            "trace_id":        trace_id,
            "span_id":         str_or_none(row.get("span_id")),
            "start_time":      st,
            "date":            date,
            "agent_id":        str_or_none(row.get("agent_id")),
            "session_id":      str_or_none(row.get("session_id")),
            "project":         str_or_none(row.get("project")),
            "requested_model": str_or_none(attrs.get("prov.route.requested_model") or ""),
            "resolved_model":  str_or_none(attrs.get("prov.route.resolved_model") or ""),
            "reason":          str_or_none(attrs.get("prov.route.reason") or ""),
            "runtime":         str_or_none(attrs.get("prov.route.runtime") or ""),
            "message_count":   int_or_none(attrs.get("prov.route.message_count")),
            "provider_id":     str_or_none(attrs.get("prov.route.provider_id") or ""),
            "duration_ms":     float_or_none(row.get("duration_ms")),
            "cost_usd":        float_or_none(row.get("cost_usd")),
        }

    return span, routing


# ── Parquet Writer ────────────────────────────────────────────────────────────

class BatchWriter:
    """Accumulates rows and flushes to Parquet in batches."""

    def __init__(self, schema: pa.Schema, output_dir: Path,
                 partition_by: list[str] | None = None,
                 batch_size: int = 50_000):
        self.schema        = schema
        self.output_dir    = output_dir
        self.partition_by  = partition_by or []
        self.batch_size    = batch_size
        self._buffers: dict[tuple, list] = defaultdict(list)
        self._part_counts: dict[tuple, int] = defaultdict(int)
        self._total_written = 0

    def add(self, row: dict):
        key = tuple(str(row.get(p) or "_unknown") for p in self.partition_by)
        self._buffers[key].append(row)
        if len(self._buffers[key]) >= self.batch_size:
            self._flush(key)

    def _flush(self, key: tuple):
        buf = self._buffers.pop(key, [])
        if not buf:
            return

        # Build output path
        if self.partition_by:
            parts = "/".join(
                f"{col}={val}"
                for col, val in zip(self.partition_by, key)
            )
            out_dir = self.output_dir / parts
        else:
            out_dir = self.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        part_n = self._part_counts[key]
        self._part_counts[key] += 1
        out_path = out_dir / f"part-{part_n:04d}.parquet"

        # Convert to Arrow table (column by column, coercing types)
        cols = {}
        for field in self.schema:
            values = [row.get(field.name) for row in buf]
            if pa.types.is_boolean(field.type):
                values = [bool(v) if v is not None else None for v in values]
            elif pa.types.is_integer(field.type):
                values = [int(v) if v is not None else None for v in values]
            elif pa.types.is_floating(field.type):
                values = [float(v) if v is not None else None for v in values]
            else:
                values = [str(v) if v is not None else None for v in values]
            cols[field.name] = pa.array(values, type=field.type)

        table = pa.table(cols, schema=self.schema)
        pq.write_table(table, out_path, compression="zstd")
        self._total_written += len(buf)

    def flush_all(self):
        for key in list(self._buffers.keys()):
            self._flush(key)

    @property
    def total_written(self):
        return self._total_written


# ── Phase 1: agent_events ─────────────────────────────────────────────────────

def migrate_agent_events(dry_run: bool = False):
    log.info("=== Phase 1: agent_events ===")

    ae_writer = BatchWriter(
        schema       = AGENT_EVENTS_SCHEMA,
        output_dir   = OUTPUT_DIR / "agent_events",
        partition_by = ["date", "agent_id"],
        batch_size   = 30_000,
    )
    pr_writer = BatchWriter(
        schema     = PR_EVENTS_SCHEMA,
        output_dir = OUTPUT_DIR / "pr_events",
        batch_size = 10_000,
    )

    seen_ids        = set()   # deduplication by event id
    seen_pr_keys    = set()   # dedup (session_id, pr_number, pr_repository)
    total_rows      = 0
    total_dupes     = 0
    total_pr        = 0

    for filename in AGENT_EVENTS_FILES:
        path = BACKUP_DIR / filename
        log.info(f"  Reading {filename} ({path.stat().st_size / 1024**2:.1f} MB)")

        fh, reader = open_agent_events_csv(path)
        file_rows = 0

        try:
            for row in reader:
                ev = process_agent_event_row(row)
                if ev is None:
                    continue

                # Dedup by id
                eid = ev["id"]
                if eid in seen_ids:
                    total_dupes += 1
                    continue
                seen_ids.add(eid)

                if not dry_run:
                    ae_writer.add(ev)

                # PR events
                if row.get("event_type") == "pr-link":
                    pr = process_pr_event(row)
                    if pr:
                        pr_key = (pr["session_id"], pr["pr_number"], pr["pr_repository"])
                        if pr_key not in seen_pr_keys:
                            seen_pr_keys.add(pr_key)
                            total_pr += 1
                            if not dry_run:
                                pr_writer.add(pr)

                file_rows += 1
                total_rows += 1

        finally:
            fh.close()

        log.info(f"    → {file_rows:,} rows processed")

    if not dry_run:
        ae_writer.flush_all()
        pr_writer.flush_all()

    log.info(f"  agent_events total: {total_rows:,} rows, {total_dupes:,} dupes dropped")
    log.info(f"  pr_events total: {total_pr:,} unique PR links")
    log.info(f"  Written: {ae_writer.total_written:,} agent_events, {pr_writer.total_written:,} pr_events")

    return ae_writer.total_written, pr_writer.total_written


# ── Phase 2: spans ────────────────────────────────────────────────────────────

def migrate_spans(dry_run: bool = False):
    log.info("=== Phase 2: spans ===")

    span_writer = BatchWriter(
        schema       = SPANS_SCHEMA,
        output_dir   = OUTPUT_DIR / "spans",
        partition_by = ["date"],
        batch_size   = 20_000,
    )
    route_writer = BatchWriter(
        schema     = ROUTING_SCHEMA,
        output_dir = OUTPUT_DIR / "routing",
        batch_size = 10_000,
    )

    seen_span_keys = set()   # dedup by (trace_id, span_id)
    total_rows  = 0
    total_dupes = 0
    total_route = 0

    log.info(f"  Reading spans.csv ({SPANS_FILE.stat().st_size / 1024**2:.1f} MB)")
    with open(SPANS_FILE, newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            span, routing = process_span_row(row)
            if span is None:
                continue

            key = (span["trace_id"], span["span_id"] or "")
            if key in seen_span_keys:
                total_dupes += 1
                continue
            seen_span_keys.add(key)

            if not dry_run:
                span_writer.add(span)

            if routing:
                total_route += 1
                if not dry_run:
                    route_writer.add(routing)

            total_rows += 1

    if not dry_run:
        span_writer.flush_all()
        route_writer.flush_all()

    log.info(f"  spans total: {total_rows:,} rows, {total_dupes:,} dupes dropped")
    log.info(f"  routing decisions: {total_route:,}")
    log.info(f"  Written: {span_writer.total_written:,} spans, {route_writer.total_written:,} routing")

    return span_writer.total_written, route_writer.total_written


# ── Phase 3: DuckDB views + sessions ─────────────────────────────────────────

DUCKDB_SETUP_SQL = """
-- ── External tables over Parquet partitions ──────────────────────────────────

CREATE OR REPLACE VIEW agent_events AS
SELECT
    id,
    session_id,
    CAST(timestamp AS TIMESTAMP) AS timestamp,
    date::DATE                   AS date,
    agent_id,
    event_type,
    role,
    content,
    git_repo,
    git_branch,
    cwd,
    slug,
    entrypoint,
    prompt_id,
    request_id,
    sub_agent_id,
    permission_mode,
    stop_reason,
    is_api_error,
    is_sidechain,
    parent_uuid,
    message_uuid,
    raw_data
FROM read_parquet('{parquet_dir}/agent_events/**/*.parquet', hive_partitioning=true);

CREATE OR REPLACE VIEW spans AS
SELECT
    trace_id,
    span_id,
    parent_span_id,
    name,
    service_name,
    CAST(start_time AS TIMESTAMP) AS start_time,
    CAST(end_time   AS TIMESTAMP) AS end_time,
    date::DATE                    AS date,
    duration_ms,
    activity_type,
    agent_id,
    agent_type,
    session_id,
    parent_session_id,
    project,
    task_label,
    llm_provider,
    llm_model,
    prompt_tokens,
    completion_tokens,
    total_tokens,
    cache_read_tokens,
    cache_write_tokens,
    cost_usd,
    prompt_preview,
    response_preview,
    stop_reason,
    agent_model,
    associated_with,
    CAST(ingested_at AS TIMESTAMP) AS ingested_at,
    route_requested_model,
    route_resolved_model,
    route_reason,
    route_runtime,
    route_message_count,
    route_provider_id,
    cache_hit_rate,
    hook_source,
    attributes_json
FROM read_parquet('{parquet_dir}/spans/**/*.parquet', hive_partitioning=true);

CREATE OR REPLACE VIEW pr_events AS
SELECT
    session_id,
    pr_number,
    pr_url,
    pr_repository,
    CAST(timestamp AS TIMESTAMP) AS timestamp,
    agent_id
FROM read_parquet('{parquet_dir}/pr_events/**/*.parquet');

CREATE OR REPLACE VIEW routing AS
SELECT
    trace_id,
    span_id,
    CAST(start_time AS TIMESTAMP) AS start_time,
    date::DATE                    AS date,
    agent_id,
    session_id,
    project,
    requested_model,
    resolved_model,
    reason,
    runtime,
    message_count,
    provider_id,
    duration_ms,
    cost_usd
FROM read_parquet('{parquet_dir}/routing/**/*.parquet');


-- ── Derived: sessions ─────────────────────────────────────────────────────────
-- Aggregated from agent_events (one row per Claude Code session / slug)

CREATE OR REPLACE VIEW sessions AS
WITH base AS (
    SELECT
        session_id,
        agent_id,
        -- Use the slug from the first event in session
        FIRST(slug ORDER BY timestamp)           AS slug,
        FIRST(git_repo ORDER BY timestamp)       AS git_repo,
        FIRST(git_branch ORDER BY timestamp)     AS git_branch,
        FIRST(entrypoint ORDER BY timestamp)     AS entrypoint,
        MIN(timestamp)                           AS started_at,
        MAX(timestamp)                           AS ended_at,
        COUNT(*)                                 AS total_events,
        COUNT(*) FILTER (WHERE role = 'user')    AS user_turns,
        COUNT(*) FILTER (WHERE role = 'assistant') AS assistant_turns,
        COUNT(*) FILTER (WHERE is_api_error)     AS api_errors,
        COUNT(*) FILTER (WHERE event_type = 'pr-link') AS pr_link_events,
    FROM agent_events
    WHERE session_id IS NOT NULL
    GROUP BY session_id, agent_id
),
costs AS (
    SELECT
        session_id,
        SUM(cost_usd)         AS total_cost_usd,
        SUM(total_tokens)     AS total_tokens,
        SUM(prompt_tokens)    AS total_prompt_tokens,
        SUM(completion_tokens) AS total_completion_tokens,
    FROM spans
    WHERE session_id IS NOT NULL
    GROUP BY session_id
),
prs AS (
    SELECT
        session_id,
        LIST(DISTINCT pr_number ORDER BY pr_number)    AS pr_numbers,
        LIST(DISTINCT pr_repository ORDER BY pr_repository) AS pr_repos,
    FROM pr_events
    GROUP BY session_id
)
SELECT
    b.session_id,
    b.agent_id,
    b.slug,
    b.git_repo,
    b.git_branch,
    b.entrypoint,
    b.started_at,
    b.ended_at,
    DATEDIFF('second', b.started_at, b.ended_at) AS duration_seconds,
    b.total_events,
    b.user_turns,
    b.assistant_turns,
    b.api_errors,
    COALESCE(c.total_cost_usd, 0)          AS total_cost_usd,
    COALESCE(c.total_tokens, 0)            AS total_tokens,
    COALESCE(c.total_prompt_tokens, 0)     AS total_prompt_tokens,
    COALESCE(c.total_completion_tokens, 0) AS total_completion_tokens,
    p.pr_numbers,
    p.pr_repos,
FROM base b
LEFT JOIN costs c USING (session_id)
LEFT JOIN prs   p USING (session_id);
"""

SAMPLE_QUERIES_SQL = """
-- ── Handy example queries ─────────────────────────────────────────────────────

-- Cost by model (all time)
-- SELECT llm_model, llm_provider, COUNT(*) AS calls, SUM(cost_usd) AS total_usd
-- FROM spans WHERE cost_usd IS NOT NULL
-- GROUP BY 1,2 ORDER BY total_usd DESC;

-- Sessions per repo, sorted by cost
-- SELECT git_repo, COUNT(DISTINCT session_id) AS sessions, SUM(total_cost_usd) AS cost_usd
-- FROM sessions GROUP BY 1 ORDER BY 2 DESC;

-- PRs created by agents
-- SELECT pr_repository, pr_number, pr_url, agent_id, timestamp
-- FROM pr_events ORDER BY timestamp DESC;

-- Mux routing decisions: what % of traffic gets downgraded?
-- SELECT requested_model, resolved_model, reason, COUNT(*) AS n
-- FROM routing GROUP BY 1,2,3 ORDER BY n DESC;

-- Busiest git repos by agent turns
-- SELECT git_repo, git_branch, COUNT(*) AS assistant_turns
-- FROM agent_events WHERE role='assistant' AND git_repo IS NOT NULL
-- GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20;

-- Daily cost trend
-- SELECT date, SUM(cost_usd) AS daily_cost_usd
-- FROM spans GROUP BY 1 ORDER BY 1;
"""


def setup_duckdb(skip_parquet: bool = False):
    log.info("=== Phase 3: DuckDB setup ===")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(DB_PATH))

    sql = DUCKDB_SETUP_SQL.replace("{parquet_dir}", str(OUTPUT_DIR))
    con.execute(sql)
    log.info(f"  Views created in {DB_PATH}")

    # Quick sanity checks
    if not skip_parquet:
        try:
            n_ae = con.execute("SELECT COUNT(*) FROM agent_events").fetchone()[0]
            n_sp = con.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
            n_pr = con.execute("SELECT COUNT(*) FROM pr_events").fetchone()[0]
            n_ro = con.execute("SELECT COUNT(*) FROM routing").fetchone()[0]
            log.info(f"  Sanity check: agent_events={n_ae:,}  spans={n_sp:,}  pr_events={n_pr:,}  routing={n_ro:,}")

            log.info("  Top 5 repos by assistant turns:")
            rows = con.execute("""
                SELECT git_repo, COUNT(*) AS turns
                FROM agent_events WHERE role='assistant' AND git_repo IS NOT NULL
                GROUP BY 1 ORDER BY 2 DESC LIMIT 5
            """).fetchall()
            for r in rows:
                log.info(f"    {r[0]}: {r[1]:,}")

            log.info("  Top models by cost:")
            rows = con.execute("""
                SELECT llm_model, ROUND(SUM(cost_usd),2) AS usd
                FROM spans WHERE cost_usd IS NOT NULL
                GROUP BY 1 ORDER BY 2 DESC LIMIT 5
            """).fetchall()
            for r in rows:
                log.info(f"    {r[0]}: ${r[1]}")
        except Exception as e:
            log.warning(f"  Sanity check skipped (parquet may not exist yet): {e}")

    # Save example queries next to the DB
    queries_path = DB_PATH.parent / "example_queries.sql"
    queries_path.write_text(SAMPLE_QUERIES_SQL)
    log.info(f"  Example queries → {queries_path}")

    con.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global BACKUP_DIR, OUTPUT_DIR, DB_PATH
    parser = argparse.ArgumentParser(description="Nexus GCP → Local DuckDB migration")
    parser.add_argument("--dry-run",      action="store_true", help="Parse only, don't write Parquet")
    parser.add_argument("--skip-parquet", action="store_true", help="Skip Parquet writing, just set up DuckDB views")
    parser.add_argument("--phase",        choices=["1","2","3","all"], default="all",
                        help="Run a specific phase only (1=agent_events, 2=spans, 3=duckdb)")
    parser.add_argument("--source-dir",   type=Path, default=BACKUP_DIR,
                        help=f"Directory containing CSV exports (default: {BACKUP_DIR})")
    parser.add_argument("--output-dir",   type=Path, default=OUTPUT_DIR,
                        help=f"Parquet output directory (default: {OUTPUT_DIR})")
    parser.add_argument("--db-path",      type=Path, default=DB_PATH,
                        help=f"DuckDB file path (default: {DB_PATH})")
    args = parser.parse_args()

    # Allow overrides without disturbing the rest of the legacy script's
    # module-level constant references.
    BACKUP_DIR = args.source_dir
    OUTPUT_DIR = args.output_dir
    DB_PATH = args.db_path

    log.info("Nexus migration starting")
    log.info(f"  Backup dir : {BACKUP_DIR}")
    log.info(f"  Output dir : {OUTPUT_DIR}")
    log.info(f"  DuckDB     : {DB_PATH}")
    log.info(f"  Dry run    : {args.dry_run}")
    log.info(f"  Files      : {len(AGENT_EVENTS_FILES)} agent_events CSVs + spans.csv")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    t0 = datetime.now()

    if args.phase in ("1", "all") and not args.skip_parquet:
        migrate_agent_events(dry_run=args.dry_run)

    if args.phase in ("2", "all") and not args.skip_parquet:
        migrate_spans(dry_run=args.dry_run)

    if args.phase in ("3", "all") and not args.dry_run:
        setup_duckdb(skip_parquet=args.skip_parquet)

    elapsed = (datetime.now() - t0).total_seconds()
    log.info(f"Done in {elapsed:.1f}s")

    if not args.dry_run and not args.skip_parquet:
        # Print parquet layout summary
        total_size = sum(
            f.stat().st_size
            for f in OUTPUT_DIR.rglob("*.parquet")
        )
        log.info(f"Total Parquet size: {total_size / 1024**2:.1f} MB")


if __name__ == "__main__":
    main()
