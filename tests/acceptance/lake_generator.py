"""Deterministic seeded synthetic lakehouse generator with on-disk caching.

Generates parquet data in the exact Hive partition structure expected by Drover
(date=YYYY-MM-DD/agent_id=<id>/part-*.parquet) with realistic session-size
distribution (long tail), 120 days of date partitions, and one 40k-event session
whose raw payload is > 8 MiB.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from drover.schema import _ensure_seed_parquet

GENERATOR_VERSION = "2026.10.4"
BIG_SESSION_ID = "session-scale-40k"
BIG_SESSION_EVENT_COUNT = 40_000
DAYS_COUNT = 120
BASE_ANCHOR_DATE = datetime(2026, 10, 4, tzinfo=timezone.utc)

_VOCAB: list[str] = [
    "asyncio",
    "coroutine",
    "endpoint",
    "middleware",
    "handler",
    "request",
    "response",
    "payload",
    "schema",
    "migration",
    "database",
    "postgres",
    "duckdb",
    "parquet",
    "analytics",
    "telemetry",
    "harness",
    "agent",
    "session",
    "query",
    "execution",
    "process",
    "memory",
    "buffer",
    "optimizer",
    "partition",
    "timeout",
    "connection",
    "pipeline",
    "stream",
    "socket",
    "cluster",
    "worker",
    "executor",
    "thread",
    "exception",
    "traceback",
    "transaction",
    "commit",
    "rollback",
    "savepoint",
    "isolation",
    "read_committed",
    "serializable",
    "index",
    "scan",
    "filter",
    "projection",
    "aggregation",
    "group_by",
    "order_by",
    "join",
    "hash_join",
    "merge_join",
    "nested_loop",
    "cost_model",
    "cardinality",
    "statistics",
    "histogram",
    "hyperloglog",
    "bloom_filter",
    "lsm_tree",
    "b_tree",
    "wal",
    "checkpoint",
    "vacuum",
    "analyze",
    "reindex",
    "table_lock",
    "advisory_lock",
    "git",
    "branch",
    "commit_hash",
    "diff",
    "patch",
    "worktree",
    "rebase",
    "merge",
    "syntax_error",
    "type_error",
    "runtime_error",
    "out_of_memory",
    "segmentation_fault",
    "refactor",
    "benchmark",
    "profiler",
    "latency",
    "throughput",
    "saturation",
    "backoff",
    "exponential",
    "circuit_breaker",
    "health_check",
    "liveness",
    "readiness",
]
_IDENTIFIERS: list[str] = [f"{w1}_{w2}" for w1 in _VOCAB[:40] for w2 in _VOCAB[40:80]]
_WORDS: list[str] = _VOCAB + _IDENTIFIERS

# Canonical schema matching drover.schema ae_seed_schema
AE_ARROW_SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        ("session_id", pa.string()),
        ("task_id", pa.string()),
        ("timestamp", pa.timestamp("us", tz="UTC")),
        ("event_type", pa.string()),
        ("role", pa.string()),
        ("content", pa.string()),
        ("repo_owner", pa.string()),
        ("repo_name", pa.string()),
        ("branch", pa.string()),
        ("principal_id", pa.string()),
        ("input_tokens", pa.int64()),
        ("output_tokens", pa.int64()),
        ("cache_read_tokens", pa.int64()),
        ("cache_write_tokens", pa.int64()),
        ("reasoning_tokens", pa.int64()),
        ("dedup_key", pa.string()),
        ("raw_data", pa.string()),
    ]
)

_MEM_CACHE: dict[tuple[str, int, str, str], Path] = {}


@dataclass(frozen=True)
class LakeMetadata:
    seed: int
    scale: str
    version: str
    total_events: int
    total_sessions: int
    days_count: int
    big_session_id: str
    big_session_events: int
    big_session_raw_bytes: int


def _default_cache_dir() -> Path:
    env_dir = os.environ.get("DROVER_ACCEPTANCE_CACHE")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    # Default to .pytest_cache/acceptance_lake_cache
    return Path(".pytest_cache") / "acceptance_lake_cache"


def compute_cache_key(seed: int, scale: str, version: str = GENERATOR_VERSION) -> str:
    digest = hashlib.sha256(f"{seed}:{scale}:{version}".encode("utf-8")).hexdigest()
    return digest[:16]


def get_cached_lake(
    scale: str = "small",
    seed: int = 42,
    cache_dir: Path | None = None,
) -> Path:
    """Return a path to a cached seeded lake directory, generating at most once."""
    base_dir = (cache_dir or _default_cache_dir()).resolve()
    cache_key = (str(base_dir), seed, scale, GENERATOR_VERSION)
    if cache_key in _MEM_CACHE:
        cached = _MEM_CACHE[cache_key]
        if (cached / "_SUCCESS").is_file():
            return cached

    base_dir.mkdir(parents=True, exist_ok=True)
    key_str = compute_cache_key(seed, scale, GENERATOR_VERSION)
    lake_dir = base_dir / f"lake_{scale}_{key_str}"

    if (lake_dir / "_SUCCESS").is_file():
        _MEM_CACHE[cache_key] = lake_dir
        return lake_dir

    tmp_dir = base_dir / f"lake_{scale}_{key_str}.tmp.{os.getpid()}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        generate_lake(tmp_dir, seed=seed, scale=scale)
        if lake_dir.exists():
            shutil.rmtree(lake_dir, ignore_errors=True)
        tmp_dir.rename(lake_dir)
    except BaseException:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    _MEM_CACHE[cache_key] = lake_dir
    return lake_dir


def generate_lake(
    output_dir: Path,
    seed: int = 42,
    scale: str = "small",
) -> LakeMetadata:
    """Generate deterministic synthetic lake data into output_dir."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if scale not in {"small", "5m"}:
        raise ValueError(f"Unknown scale: {scale}")
    rng = random.Random(seed)
    agents = ["claude-code", "codex"]
    repos = [
        ("arniesaha", "drover", "main"),
        ("arniesaha", "drover", "feat/v2-lake"),
        ("openclaw", "clawd", "main"),
        ("deepmind", "antigravity", "main"),
    ]

    target_total = 50_000 if scale == "small" else 5_000_000
    target_non_big = target_total - BIG_SESSION_EVENT_COUNT

    # Generate 120 date partition strings
    dates = [
        (BASE_ANCHOR_DATE - timedelta(days=DAYS_COUNT - 1 - i)).strftime("%Y-%m-%d")
        for i in range(DAYS_COUNT)
    ]

    # Pre-plan sessions across 120 days
    # Ensure every day has at least 1 session
    # Sample session lengths from Pareto (alpha=1.4) to create realistic long tail
    day_sessions: list[list[tuple[str, int, str, tuple[str, str, str]]]] = [
        [] for _ in range(DAYS_COUNT)
    ]
    accumulated_events = 0
    session_idx = 0

    # Ensure every day gets at least 1 session
    for day_i in range(DAYS_COUNT):
        s_len = max(5, min(int(rng.paretovariate(1.4) * 8), 1000))
        agent = agents[rng.randint(0, len(agents) - 1)]
        repo = repos[rng.randint(0, len(repos) - 1)]
        sid = f"sess-{session_idx:07d}"
        session_idx += 1
        day_sessions[day_i].append((sid, s_len, agent, repo))
        accumulated_events += s_len

    # Continue generating sessions until target_non_big is reached
    while accumulated_events < target_non_big:
        day_i = rng.randint(0, DAYS_COUNT - 1)
        remaining = target_non_big - accumulated_events
        s_len = max(3, min(int(rng.paretovariate(1.4) * 8), 1000))
        if s_len > remaining:
            s_len = remaining
        agent = agents[rng.randint(0, len(agents) - 1)]
        repo = repos[rng.randint(0, len(repos) - 1)]
        sid = f"sess-{session_idx:07d}"
        session_idx += 1
        day_sessions[day_i].append((sid, s_len, agent, repo))
        accumulated_events += s_len

    # Place the 40k big session on the last day with claude-code
    big_session_day_idx = DAYS_COUNT - 1
    big_session_repo = repos[0]
    day_sessions[big_session_day_idx].append(
        (BIG_SESSION_ID, BIG_SESSION_EVENT_COUNT, "claude-code", big_session_repo)
    )

    total_events_written = 0
    total_sessions_count = session_idx + 1
    big_session_raw_bytes = 0

    event_counter = 0

    # Generate and write partitions
    for day_i, day_str in enumerate(dates):
        sessions_in_day = day_sessions[day_i]
        # Group by agent
        by_agent: dict[str, list[tuple[str, int, tuple[str, str, str]]]] = {}
        for sid, length, agent, repo in sessions_in_day:
            by_agent.setdefault(agent, []).append((sid, length, repo))

        day_base_dt = BASE_ANCHOR_DATE - timedelta(days=DAYS_COUNT - 1 - day_i)
        day_base_us = int(day_base_dt.timestamp() * 1_000_000)

        for agent, s_list in by_agent.items():
            part_dir = (
                output_dir / "agent_events" / f"date={day_str}" / f"agent_id={agent}"
            )
            part_dir.mkdir(parents=True, exist_ok=True)

            col_id = []
            col_session_id = []
            col_task_id = []
            col_timestamp = []
            col_event_type = []
            col_role = []
            col_content = []
            col_repo_owner = []
            col_repo_name = []
            col_branch = []
            col_principal_id = []
            col_input_tokens = []
            col_output_tokens = []
            col_cache_read_tokens = []
            col_cache_write_tokens = []
            col_reasoning_tokens = []
            col_dedup_key = []
            col_raw_data = []

            for sid, count, (r_owner, r_name, r_branch) in s_list:
                is_big = sid == BIG_SESSION_ID
                task_id = f"task-{r_owner}-{r_name}"

                for step in range(count):
                    event_counter += 1
                    eid = f"evt-{event_counter:09d}"
                    ts_us = day_base_us + (step * 50_000)  # offset by 50ms per step
                    role = "user" if step % 2 == 0 else "assistant"
                    ev_type = "user_input" if role == "user" else "assistant_output"

                    # Fresh seeded text per event avoids dictionary compression of
                    # a tiny repeated phrase pool. Pareto lengths model tool-output tails.
                    word_count = min(350, max(20, int(rng.paretovariate(1.35) * 32)))
                    phrase = " ".join(rng.choices(_WORDS, k=word_count))
                    if is_big:
                        w = _WORDS[(step * 11) % len(_WORDS)]
                        tool_name = _WORDS[(step * 5) % len(_WORDS)]
                        content = (
                            f"Turn {step} of large stress session: inspecting {w} and executing {tool_name}. "
                            f"Summary: {phrase[:160]}"
                        )
                        raw_payload = (
                            '{"id": "%s", "session_id": "%s", "step": %d, "agent": "%s", "role": "%s", '
                            '"_repo_owner": "%s", "_repo_name": "%s", "gitBranch": "%s", "cwd": "/workspace/%s", '
                            '"tool_use_blocks": [{"name": "%s", "input": {"path": "src/drover/%s.py"}}], '
                            '"details": "%s"}'
                            % (
                                eid,
                                sid,
                                step,
                                agent,
                                role,
                                r_owner,
                                r_name,
                                r_branch,
                                r_name,
                                tool_name,
                                w,
                                phrase,
                            )
                        )
                        big_session_raw_bytes += len(raw_payload.encode("utf-8"))
                    else:
                        w = _WORDS[(event_counter * 13) % len(_WORDS)]
                        if role == "user":
                            content = f"Turn {step} of session {sid}: inspect and refactor {w} module. Details: {phrase}"
                            raw_payload = (
                                '{"id": "%s", "session_id": "%s", "step": %d, "agent": "%s", "role": "user", '
                                '"_repo_owner": "%s", "_repo_name": "%s", "gitBranch": "%s", "cwd": "/workspace/%s", '
                                '"intent": "%s", "context": "%s"}'
                                % (
                                    eid,
                                    sid,
                                    step,
                                    agent,
                                    r_owner,
                                    r_name,
                                    r_branch,
                                    r_name,
                                    w,
                                    phrase[:180],
                                )
                            )
                        else:
                            tool_name = _WORDS[(event_counter * 7) % len(_WORDS)]
                            content = f"Turn {step} of session {sid}: executed {tool_name} for {w}. Result summary: {phrase[:200]}"
                            raw_payload = (
                                '{"id": "%s", "session_id": "%s", "step": %d, "agent": "%s", "role": "assistant", '
                                '"_repo_owner": "%s", "_repo_name": "%s", "gitBranch": "%s", "cwd": "/workspace/%s", '
                                '"tool_use_blocks": [{"name": "%s", "input": {"path": "src/drover/%s.py"}}], '
                                '"output": "%s"}'
                                % (
                                    eid,
                                    sid,
                                    step,
                                    agent,
                                    r_owner,
                                    r_name,
                                    r_branch,
                                    r_name,
                                    tool_name,
                                    w,
                                    phrase,
                                )
                            )

                    dedup = f"{day_str}:{agent}:{sid}:{ev_type}:{step}"

                    col_id.append(eid)
                    col_session_id.append(sid)
                    col_task_id.append(task_id)
                    col_timestamp.append(ts_us)
                    col_event_type.append(ev_type)
                    col_role.append(role)
                    col_content.append(content)
                    col_repo_owner.append(r_owner)
                    col_repo_name.append(r_name)
                    col_branch.append(r_branch)
                    col_principal_id.append("developer@local")
                    col_input_tokens.append(50 + (step % 200))
                    col_output_tokens.append(25 + (step % 100))
                    col_cache_read_tokens.append(10)
                    col_cache_write_tokens.append(5)
                    col_reasoning_tokens.append(0)
                    col_dedup_key.append(dedup)
                    col_raw_data.append(raw_payload)

            table = pa.Table.from_arrays(
                [
                    pa.array(col_id, type=pa.string()),
                    pa.array(col_session_id, type=pa.string()),
                    pa.array(col_task_id, type=pa.string()),
                    pa.array(col_timestamp, type=pa.timestamp("us", tz="UTC")),
                    pa.array(col_event_type, type=pa.string()),
                    pa.array(col_role, type=pa.string()),
                    pa.array(col_content, type=pa.string()),
                    pa.array(col_repo_owner, type=pa.string()),
                    pa.array(col_repo_name, type=pa.string()),
                    pa.array(col_branch, type=pa.string()),
                    pa.array(col_principal_id, type=pa.string()),
                    pa.array(col_input_tokens, type=pa.int64()),
                    pa.array(col_output_tokens, type=pa.int64()),
                    pa.array(col_cache_read_tokens, type=pa.int64()),
                    pa.array(col_cache_write_tokens, type=pa.int64()),
                    pa.array(col_reasoning_tokens, type=pa.int64()),
                    pa.array(col_dedup_key, type=pa.string()),
                    pa.array(col_raw_data, type=pa.string()),
                ],
                schema=AE_ARROW_SCHEMA,
            )

            part_file = part_dir / "part-0000.parquet"
            pq.write_table(table, part_file, compression="zstd")
            total_events_written += len(table)

    # Ensure empty seed files exist for other relations
    _ensure_seed_parquet(output_dir)

    metadata = LakeMetadata(
        seed=seed,
        scale=scale,
        version=GENERATOR_VERSION,
        total_events=total_events_written,
        total_sessions=total_sessions_count,
        days_count=DAYS_COUNT,
        big_session_id=BIG_SESSION_ID,
        big_session_events=BIG_SESSION_EVENT_COUNT,
        big_session_raw_bytes=big_session_raw_bytes,
    )

    (output_dir / "_SUCCESS").write_text(
        json.dumps(asdict(metadata), indent=2), encoding="utf-8"
    )
    return metadata
