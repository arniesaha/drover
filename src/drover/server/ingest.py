"""Ingest a JSONL file of canonical AgentEvents into the lakehouse.

For each event:
  1. Parse + validate via AgentEvent.
  2. Compute dedup_key from (timestamp, agent_id, session_id, event_type, content[:200]).
  3. Compute task_id from raw_data._repo_owner / _repo_name / gitBranch (or env).
  4. Append to a date=YYYY-MM-DD/agent_id=<id> Parquet file with a unique part name.
  5. Drop rows whose dedup_key already appears in the existing partition.
  6. Upsert tasks rows.

Idempotent: re-ingesting the same file produces zero new rows.
"""

from __future__ import annotations
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional, Set

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from drover.attribution import enrich_raw_repo_attribution
from drover.dedup import make_dedup_key
from drover.server import ledger_shadow
from drover.server.db import open_duckdb_connection
from drover.server.parquet_io import atomic_write_table
from drover.server.redis_shadow import ShadowPublisher
from drover.server.rollup import rollup_tasks
from drover.task_id import compute_task_id
from drover.models import AgentEvent

log = logging.getLogger("drover.ingest")


@dataclass
class IngestStats:
    read: int = 0
    inserted: int = 0
    skipped_dupes: int = 0
    errors: int = 0
    shadow_published: int = 0
    ledger_receipts: int = 0
    new_session_ids: Set[str] = field(default_factory=set)


def _extract_content(ev: AgentEvent) -> str:
    if ev.message and isinstance(ev.message.content, str):
        return ev.message.content
    if ev.message and isinstance(ev.message.content, list):
        # Concatenate text blocks for fingerprinting
        parts = []
        for block in ev.message.content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _row_from_event(ev: AgentEvent, env_task_id: Optional[str]) -> dict:
    rd = enrich_raw_repo_attribution(ev.raw_data)
    repo_owner = rd.get("_repo_owner")
    repo_name = rd.get("_repo_name")
    branch = rd.get("gitBranch") or rd.get("git_branch")
    content = _extract_content(ev)

    return {
        "id": ev.id,
        "session_id": ev.session_id,
        "timestamp": ev.timestamp,
        "date": ev.timestamp.strftime("%Y-%m-%d"),
        "agent_id": ev.agent_id,
        "event_type": ev.event_type,
        "role": ev.message.role if ev.message else None,
        "content": content,
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "branch": branch,
        "task_id": compute_task_id(env_task_id, repo_owner, repo_name, branch),
        "principal_id": rd.get("_principal_id"),
        "dedup_key": make_dedup_key(
            ev.timestamp.isoformat(),
            ev.agent_id,
            ev.session_id,
            ev.event_type,
            content,
        ),
        "raw_data": json.dumps(rd, default=str),
    }


def _propagate_unique_session_repo(
    rows: list[dict], env_task_id: Optional[str]
) -> None:
    """Fill unattributed rows from the only repo observed in the same session.

    Claude Code sessions often emit early hook/observer rows before a cwd is
    present, then later rows in the same session carry definitive repo metadata.
    If a session has exactly one repo in the incoming batch, propagating it is
    deterministic. Sessions with multiple repos are intentionally left alone.
    """
    repos_by_session: dict[tuple[str | None, str], set[tuple[str, str, str | None]]] = (
        {}
    )
    for row in rows:
        owner = row.get("repo_owner")
        name = row.get("repo_name")
        if owner and name:
            key = (row.get("agent_id"), row["session_id"])
            repos_by_session.setdefault(key, set()).add(
                (owner, name, row.get("branch"))
            )

    unique_repos = {
        key: next(iter(repos))
        for key, repos in repos_by_session.items()
        if len({(owner, name) for owner, name, _branch in repos}) == 1
    }

    for row in rows:
        if row.get("repo_owner") and row.get("repo_name"):
            continue
        repo = unique_repos.get((row.get("agent_id"), row["session_id"]))
        if not repo:
            continue
        owner, name, branch = repo
        row["repo_owner"] = owner
        row["repo_name"] = name
        row["branch"] = row.get("branch") or branch
        row["task_id"] = compute_task_id(env_task_id, owner, name, row.get("branch"))
        try:
            raw = json.loads(row.get("raw_data") or "{}")
        except json.JSONDecodeError:
            raw = {}
        if isinstance(raw, dict):
            raw.setdefault("_repo_owner", owner)
            raw.setdefault("_repo_name", name)
            if row.get("branch"):
                raw.setdefault("gitBranch", row["branch"])
            row["raw_data"] = json.dumps(raw, default=str)


def _iter_events(
    path: Path, env_task_id: Optional[str]
) -> Iterator[tuple[Optional[dict], Optional[str]]]:
    """Yield (row_dict, error_msg) — exactly one of the two is None per yield."""
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                ev = AgentEvent.model_validate(obj)
                yield _row_from_event(ev, env_task_id), None
            except Exception as e:
                yield None, f"line {lineno}: {e!r}"


def _existing_dedup_keys(con, rows: Iterable[dict]) -> set:
    """Read existing dedup_keys only from date partitions touched by incoming rows."""
    partitions: dict[str, set[str]] = {}
    for row in rows:
        date = row.get("date")
        agent_id = row.get("agent_id")
        if date and agent_id:
            partitions.setdefault(str(date), set()).add(str(agent_id))
    if not partitions:
        return set()

    source_sql = "\nUNION ALL\n".join(
        "SELECT dedup_key, agent_id FROM agent_events_for_date(?)" for _ in partitions
    )
    params: list = list(partitions)
    agent_ids = sorted({agent for agents in partitions.values() for agent in agents})
    try:
        existing_rows = con.execute(
            f"""
            WITH bounded_agent_events AS (
              {source_sql}
            )
            SELECT dedup_key
            FROM bounded_agent_events
            WHERE dedup_key IS NOT NULL
              AND agent_id = ANY(?::VARCHAR[])
            """,
            [*params, agent_ids],
        ).fetchall()
        return {r[0] for r in existing_rows}
    except duckdb.Error as e:
        log.warning("_existing_dedup_keys failed: %s", e, exc_info=True)
        return set()


def _write_partition(rows: list[dict], parquet_dir: Path) -> None:
    """Group rows by (date, agent_id) and write one parquet file per partition."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        grouped.setdefault((r["date"], r["agent_id"]), []).append(r)

    for (date, agent_id), part_rows in grouped.items():
        out_dir = parquet_dir / "agent_events" / f"date={date}" / f"agent_id={agent_id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"part-{uuid.uuid4().hex[:12]}.parquet"
        # Drop the partition columns from the row payload — Hive partitioning encodes them in path
        payload = [
            {k: v for k, v in r.items() if k not in ("date", "agent_id")}
            for r in part_rows
        ]
        table = pa.Table.from_pylist(payload)
        atomic_write_table(table, out_path, compression="zstd")


def _is_valid_title(content: str) -> bool:
    """Return True only if *content* is suitable for use as a task title.

    Filters out:
    - XML/HTML fragments (content that starts with ``<``)
    - Suspiciously short strings (fewer than 10 characters after stripping)
    """
    stripped = content.strip()
    if stripped.startswith("<"):
        return False
    if len(stripped) < 10:
        return False
    return True


def _upsert_tasks(con, rows: list[dict]) -> None:
    """Insert any new (task_id) into tasks; update last_activity_at + session_count for existing."""
    seen: dict[str, dict] = {}
    for r in rows:
        tid = r["task_id"]
        if tid not in seen:
            seen[tid] = {
                "task_id": tid,
                "repo_owner": r.get("repo_owner"),
                "repo_name": r.get("repo_name"),
                "branch": r.get("branch"),
                "principal_id": r.get("principal_id"),
                "last_activity_at": r["timestamp"],
                "title": None,
            }
        else:
            if r["timestamp"] > seen[tid]["last_activity_at"]:
                seen[tid]["last_activity_at"] = r["timestamp"]
        # Capture the first user-role message as task title if not yet set.
        if not seen[tid]["title"] and r.get("role") == "user":
            raw_content = (r.get("content") or "").strip()
            if _is_valid_title(raw_content):
                seen[tid]["title"] = raw_content[:120].replace("\n", " ")

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


def ingest_file(
    path: Path,
    *,
    parquet_dir: Path,
    duckdb_path: Path,
    env_task_id: Optional[str] = None,
    shadow_publisher: Optional[ShadowPublisher] = None,
) -> IngestStats:
    """Ingest one JSONL file.  Returns IngestStats.

    When ``shadow_publisher`` is provided, newly-inserted rows are also mirrored
    to a Redis Stream (best-effort; the lakehouse stays the source of truth).
    """
    path = Path(path)
    parquet_dir = Path(parquet_dir)
    duckdb_path = Path(duckdb_path)

    stats = IngestStats()
    new_rows: list[dict] = []

    con = open_duckdb_connection(duckdb_path)
    try:
        parsed_rows: list[dict] = []
        for row, err in _iter_events(path, env_task_id):
            stats.read += 1
            if err:
                stats.errors += 1
                log.warning("ingest %s: %s", path, err)
                continue
            assert row is not None
            parsed_rows.append(row)

        existing = _existing_dedup_keys(con, parsed_rows)
        for row in parsed_rows:
            if row["dedup_key"] in existing:
                stats.skipped_dupes += 1
                continue
            new_rows.append(row)
            existing.add(row["dedup_key"])
            stats.new_session_ids.add(row["session_id"])

        if new_rows:
            _propagate_unique_session_repo(new_rows, env_task_id)
            _write_partition(new_rows, parquet_dir)
            _upsert_tasks(con, new_rows)
            rollup_tasks(
                con,
                task_ids=sorted({r["task_id"] for r in new_rows}),
                dates=sorted({r["date"] for r in new_rows}),
            )
            stats.inserted = len(new_rows)
            # Shadow-write a durable receipt per accepted source unit (AGE-44).
            # The dedup_key is the durable identity, so a re-arriving event is a
            # ledger no-op. Best-effort: never blocks the authoritative write.
            for row in new_rows:
                result = ledger_shadow.record_receipt(
                    con,
                    source_kind="agent_event",
                    source_key=row["dedup_key"],
                    subject_kind="session",
                    subject_key=row.get("session_id"),
                    payload_hash=row["dedup_key"],
                )
                if result is not None and not result.is_duplicate:
                    stats.ledger_receipts += 1
            if shadow_publisher is not None:
                # Mirror only after the authoritative write succeeds. The
                # publisher itself is best-effort and never raises.
                stats.shadow_published = shadow_publisher.publish_rows(new_rows)
    finally:
        con.close()

    return stats
