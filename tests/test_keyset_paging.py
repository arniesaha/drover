import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from drover.schema import bootstrap
from drover.server.memory_store import MemoryRepository
from drover.server.summarizer.jobs import JobLedger, enqueue_summary_generation
from drover.server.summarizer.worker import SummarizerWorker


def _fake_llm_call(prompt: str, **kwargs) -> dict:
    return {
        "summary_md": "Fixture summary describing the session.",
        "next_steps_md": "Move on to Plan 6.",
        "open_questions": ["use sse or streamable-http?"],
        "last_user_prompt": "do the thing",
        "last_assistant": "edited foo.py",
    }


def test_keyset_paging_straddles_duplicate_ids(
    tmp_path: Path, pg_control_path: Path
) -> None:
    parquet_dir = tmp_path / "parquet"
    session_id = "sess-dup"
    now = datetime.now(timezone.utc)

    schema = pa.schema(
        [
            ("id", pa.string()),
            ("session_id", pa.string()),
            ("agent_id", pa.string()),
            ("task_id", pa.string()),
            ("timestamp", pa.timestamp("us", tz="UTC")),
            ("event_type", pa.string()),
            ("role", pa.string()),
            ("content", pa.string()),
            ("repo_owner", pa.string()),
            ("repo_name", pa.string()),
            ("branch", pa.string()),
            ("principal_id", pa.string()),
            ("dedup_key", pa.string()),
            ("raw_data", pa.string()),
        ]
    )

    rows = []
    # 2000 duplicate events to straddle the 1000-row page boundary
    for i in range(2000):
        rows.append(
            (
                "dup-id",
                session_id,
                "macmini-claude",
                "tid1",
                now,
                "tool_call",
                "assistant",
                f"edited file_{i}.py",
                "arniesaha",
                "nexus",
                "main",
                "arnab",
                f"{session_id}-k{i}",
                json.dumps(
                    {
                        "tool_use_blocks": [
                            {"name": "Edit", "input": {"file_path": f"src/file_{i}.py"}}
                        ]
                    }
                ),
            )
        )

    cols = {f.name: [r[i] for r in rows] for i, f in enumerate(schema)}
    table = pa.table(
        {k: pa.array(v, type=schema.field(k).type) for k, v in cols.items()},
        schema=schema,
    )
    out = parquet_dir / "agent_events" / "date=2026-05-09" / "agent_id=macmini-claude"
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out / "part-0.parquet")

    bootstrap(parquet_dir=parquet_dir, duckdb_path=pg_control_path)

    enqueue_summary_generation(pg_control_path, session_id, "v1")

    worker = SummarizerWorker(
        duckdb_path=pg_control_path, api_key="sk-test", _llm_call=_fake_llm_call
    )
    worker.drain_once()

    repo = MemoryRepository(pg_control_path)
    memory = repo.latest([session_id])[session_id]

    assert len(memory.summary.files_touched) == 2000
    assert "src/file_0.py" in memory.summary.files_touched
    assert "src/file_1999.py" in memory.summary.files_touched
