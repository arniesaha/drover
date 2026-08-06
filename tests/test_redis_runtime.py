"""Runtime wiring tests for Redis-backed derived-job streams."""

from pathlib import Path

from drover.schema import bootstrap
from drover.server.__main__ import _redis_job_stream_config, _seed_redis_job_streams
from drover.config import default_config
from drover.server.db import open_duckdb_connection


class FakeStream:
    def __init__(self) -> None:
        self.published: list[dict] = []

    def add(self, fields: dict) -> str:
        self.published.append(fields)
        return f"{len(self.published)}-0"


def test_redis_job_stream_config_uses_runtime_prefix_and_group() -> None:
    cfg = default_config()
    stream_cfg = _redis_job_stream_config(cfg, "summarize_session")
    assert stream_cfg.stream == "drover:jobs:summarize_session"
    assert stream_cfg.group == "workers"
    assert stream_cfg.max_deliveries == 5


def test_seed_redis_job_streams_from_pending_duckdb_jobs(tmp_path: Path) -> None:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = open_duckdb_connection(duckdb_path)
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, source_version) "
            "VALUES ('sess-pending', 'pending', 'version-1')"
        )
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status) VALUES ('sess-done', 'done')"
        )
        con.execute(
            "INSERT INTO brief_jobs (project_key, status) VALUES ('arniesaha/nexus', 'pending')"
        )
        con.execute(
            "INSERT INTO embed_jobs (session_id, status) VALUES ('sess-embed', 'pending')"
        )
        con.execute(
            "INSERT INTO span_embed_jobs (span_id, status) VALUES ('span-embed', 'pending')"
        )
    finally:
        con.close()

    streams = {
        "summarize": FakeStream(),
        "brief": FakeStream(),
        "embed_session": FakeStream(),
        "embed_span": FakeStream(),
    }

    counts = _seed_redis_job_streams(duckdb_path=duckdb_path, streams=streams)

    assert counts == {
        "summarize": 1,
        "brief": 1,
        "embed_session": 1,
        "embed_span": 1,
    }
    assert streams["summarize"].published == [
        {"session_id": "sess-pending", "source_version": "version-1"}
    ]
    assert streams["brief"].published == [{"project_key": "arniesaha/nexus"}]
    assert streams["embed_session"].published == [{"session_id": "sess-embed"}]
    assert streams["embed_span"].published == [{"span_id": "span-embed"}]
