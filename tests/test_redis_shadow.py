"""Tests for the optional Redis Streams shadow mirror."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.ingest import ingest_file
from drover.server.redis_shadow import (
    InMemoryStreamClient,
    RedisShadowConfig,
    ShadowPublisher,
    build_publisher,
    event_fields,
    read_events,
)


def _row(i: int) -> dict:
    ts = datetime(2026, 6, 19, 12, 0, i, tzinfo=timezone.utc)
    return {
        "id": f"evt-{i}",
        "session_id": "sess-1",
        "agent_id": "test-agent",
        "event_type": "user_message",
        "task_id": "task-1",
        "repo_owner": "acme",
        "repo_name": "widgets",
        "branch": "main",
        "date": ts.strftime("%Y-%m-%d"),
        "timestamp": ts,
        "dedup_key": f"dedup-{i}",
    }


def test_event_fields_uses_dedup_key_as_idempotency_key():
    fields = event_fields(_row(7))
    assert fields["idempotency_key"] == "dedup-7"
    assert fields["dedup_key"] == "dedup-7"
    assert fields["event_type"] == "user_message"
    assert fields["timestamp"] == "2026-06-19T12:00:07+00:00"
    # All values are flat strings (Redis stream field constraint).
    assert all(isinstance(v, str) for v in fields.values())


def test_event_fields_skips_missing_values():
    fields = event_fields({"dedup_key": "d", "id": "x"})
    assert fields == {"idempotency_key": "d", "dedup_key": "d", "id": "x"}


def test_build_publisher_disabled_returns_none():
    cfg = RedisShadowConfig.from_runtime(
        enabled=False, url="redis://x", stream="s", maxlen=10
    )
    assert build_publisher(cfg, client=InMemoryStreamClient()) is None


def test_publish_and_read_roundtrip():
    client = InMemoryStreamClient()
    cfg = RedisShadowConfig.from_runtime(
        enabled=True, url="memory://", stream="nexus:events", maxlen=100
    )
    publisher = build_publisher(cfg, client=client)
    assert publisher is not None

    rows = [_row(i) for i in range(3)]
    published = publisher.publish_rows(rows)
    assert published == 3

    entries = read_events(client, "nexus:events")
    assert len(entries) == 3
    idem = [fields["idempotency_key"] for _id, fields in entries]
    assert idem == ["dedup-0", "dedup-1", "dedup-2"]


def test_maxlen_trims_stream():
    client = InMemoryStreamClient()
    publisher = ShadowPublisher(client, stream="s", maxlen=2)
    publisher.publish_rows([_row(i) for i in range(5)])
    assert client.xlen("s") == 2


class _RaisingClient:
    def xadd(self, *args, **kwargs):
        raise RuntimeError("redis down")


def test_publish_swallows_errors():
    """A failing Redis client must never raise — the mirror is best-effort."""
    publisher = ShadowPublisher(_RaisingClient(), stream="s")
    # Should not raise; returns 0 published.
    assert publisher.publish_rows([_row(0)]) == 0


def test_ingest_file_mirrors_to_shadow_stream(tmp_path: Path):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    jsonl = tmp_path / "events.jsonl"
    events = []
    for i in range(2):
        ts = datetime(2026, 6, 19, 9, 0, i, tzinfo=timezone.utc)
        events.append(
            {
                "id": f"e{i}",
                "session_id": "s1",
                "timestamp": ts.isoformat(),
                "agent_id": "a1",
                "event_type": "user_message",
                "message": {"role": "user", "content": f"hello {i}"},
                "raw_data": {"_repo_owner": "acme", "_repo_name": "widgets"},
            }
        )
    jsonl.write_text("\n".join(json.dumps(e) for e in events))

    client = InMemoryStreamClient()
    publisher = ShadowPublisher(client, stream="nexus:events")

    stats = ingest_file(
        jsonl,
        parquet_dir=parquet_dir,
        duckdb_path=duckdb_path,
        shadow_publisher=publisher,
    )
    assert stats.inserted == 2
    assert stats.shadow_published == 2
    assert client.xlen("nexus:events") == 2

    # Re-ingesting the same file is a no-op for the mirror too (rows deduped).
    stats2 = ingest_file(
        jsonl,
        parquet_dir=parquet_dir,
        duckdb_path=duckdb_path,
        shadow_publisher=publisher,
    )
    assert stats2.inserted == 0
    assert stats2.shadow_published == 0
    assert client.xlen("nexus:events") == 2


def test_ingest_without_publisher_is_unaffected(tmp_path: Path):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    jsonl = tmp_path / "events.jsonl"
    ts = datetime(2026, 6, 19, 9, 0, 0, tzinfo=timezone.utc)
    jsonl.write_text(
        json.dumps(
            {
                "id": "e0",
                "session_id": "s1",
                "timestamp": ts.isoformat(),
                "agent_id": "a1",
                "event_type": "user_message",
                "message": {"role": "user", "content": "hi"},
                "raw_data": {},
            }
        )
    )
    stats = ingest_file(jsonl, parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    assert stats.inserted == 1
    assert stats.shadow_published == 0
