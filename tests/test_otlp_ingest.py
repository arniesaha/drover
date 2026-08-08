"""Tests for ingest_otlp_request — OTLP proto → Parquet → DuckDB spans view."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import (
    AnyValue,
    InstrumentationScope,
    KeyValue,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

from drover.schema import bootstrap
from drover.server.otlp.ingest import ingest_otlp_request


def _kv(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


def _span(
    *,
    trace_id: bytes,
    span_id: bytes,
    name: str,
    start_ns: int,
    attrs: list[KeyValue] | None = None,
) -> Span:
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        name=name,
        start_time_unix_nano=start_ns,
        end_time_unix_nano=start_ns + 1_000_000_000,
        attributes=attrs or [],
    )


def _bootstrap(tmp_path: Path) -> tuple[Path, Path]:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return parquet_dir, duckdb_path


def _request_with_n_spans(
    n: int, *, day_offset_per: int = 0
) -> ExportTraceServiceRequest:
    base_ns = 1715200000_000_000_000  # 2024-05-08
    spans = []
    for i in range(n):
        spans.append(
            _span(
                trace_id=bytes([i + 1]) * 16,
                span_id=bytes([i + 1]) * 8,
                name=f"span-{i}",
                start_ns=base_ns + i * day_offset_per * 86400_000_000_000,
                attrs=[
                    _kv("session.id", f"sess-{i}"),
                    _kv("prov.agent.id", "macmini-claude"),
                    _kv("prov.repo.owner", "arniesaha"),
                    _kv("prov.repo.name", "nexus"),
                    _kv("prov.git.branch", "main"),
                ],
            )
        )
    return ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(attributes=[_kv("service.name", "agentweave-proxy")]),
                scope_spans=[
                    ScopeSpans(
                        scope=InstrumentationScope(name="agentweave"), spans=spans
                    )
                ],
            )
        ]
    )


def test_ingest_writes_n_rows_into_spans(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _bootstrap(tmp_path)
    req = _request_with_n_spans(3)

    stats = ingest_otlp_request(req, parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    assert stats.read == 3
    assert stats.inserted == 3
    assert stats.skipped_dupes == 0

    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)  # refresh view
    con = duckdb.connect(str(duckdb_path))
    try:
        n = con.execute(
            "SELECT count(*) FROM spans WHERE dedup_key IS NOT NULL"
        ).fetchone()[0]
        assert n == 3
        jobs = con.execute("SELECT count(*) FROM span_embed_jobs").fetchone()[0]
        assert jobs == 3
    finally:
        con.close()


def test_ingest_is_idempotent(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _bootstrap(tmp_path)
    req = _request_with_n_spans(2)

    s1 = ingest_otlp_request(req, parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    s2 = ingest_otlp_request(req, parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    assert s1.inserted == 2
    assert s2.inserted == 0
    assert s2.skipped_dupes == 2

    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        n = con.execute(
            "SELECT count(*) FROM spans WHERE dedup_key IS NOT NULL"
        ).fetchone()[0]
        assert n == 2
    finally:
        con.close()


def test_ingest_skips_spans_without_start_time(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _bootstrap(tmp_path)
    bad = Span(trace_id=b"\xaa" * 16, span_id=b"\xbb" * 8, name="no-start")
    req = ExportTraceServiceRequest(
        resource_spans=[ResourceSpans(scope_spans=[ScopeSpans(spans=[bad])])]
    )
    stats = ingest_otlp_request(req, parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    assert stats.read == 1
    assert stats.inserted == 0
    assert stats.errors == 0  # parser-level skip is not an error


def test_ingest_partitions_by_date(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _bootstrap(tmp_path)

    # Two spans on different days
    span_day1 = _span(
        trace_id=b"\x01" * 16,
        span_id=b"\x01" * 8,
        name="d1",
        start_ns=1715126400_000_000_000,  # 2024-05-08T00:00:00Z
        attrs=[_kv("session.id", "s1")],
    )
    span_day2 = _span(
        trace_id=b"\x02" * 16,
        span_id=b"\x02" * 8,
        name="d2",
        start_ns=1715212800_000_000_000,  # 2024-05-09T00:00:00Z
        attrs=[_kv("session.id", "s2")],
    )
    req = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(scope_spans=[ScopeSpans(spans=[span_day1, span_day2])])
        ]
    )
    ingest_otlp_request(req, parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    date_dirs = sorted(p.name for p in (parquet_dir / "spans").iterdir() if p.is_dir())
    # _seed plus two real date partitions
    assert "date=2024-05-08" in date_dirs
    assert "date=2024-05-09" in date_dirs


def test_ingest_upserts_tasks(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _bootstrap(tmp_path)
    req = _request_with_n_spans(1)

    ingest_otlp_request(req, parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    con = duckdb.connect(str(duckdb_path))
    try:
        rows = con.execute(
            "SELECT repo_owner, repo_name, branch FROM tasks WHERE repo_owner='arniesaha'"
        ).fetchall()
        assert rows == [("arniesaha", "nexus", "main")]
    finally:
        con.close()


def test_ingest_derives_repo_from_agentweave_repository_attr(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _bootstrap(tmp_path)
    req = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(attributes=[_kv("service.name", "agentweave-proxy")]),
                scope_spans=[
                    ScopeSpans(
                        scope=InstrumentationScope(name="agentweave"),
                        spans=[
                            _span(
                                trace_id=b"\x10" * 16,
                                span_id=b"\x10" * 8,
                                name="repo-attr",
                                start_ns=1715200000_000_000_000,
                                attrs=[
                                    _kv("session.id", "sess-repo"),
                                    _kv("prov.agent.id", "claude-code-nas"),
                                    _kv("prov.repository", "arniesaha/healthos"),
                                    _kv("prov.git.branch", "main"),
                                ],
                            )
                        ],
                    )
                ],
            )
        ]
    )

    stats = ingest_otlp_request(req, parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    assert stats.inserted == 1
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        row = con.execute(
            "SELECT repo_owner, repo_name, branch FROM spans WHERE name='repo-attr'"
        ).fetchone()
    finally:
        con.close()
    assert row == ("arniesaha", "healthos", "main")


def test_ingest_derives_repo_from_agentweave_prov_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(
        "DROVER_REPO_ROOTS_JSON",
        '{"/home/Arnab/clawd/projects/healthos": "arniesaha/healthos"}',
    )
    parquet_dir, duckdb_path = _bootstrap(tmp_path)
    req = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(attributes=[_kv("service.name", "agentweave-proxy")]),
                scope_spans=[
                    ScopeSpans(
                        scope=InstrumentationScope(name="agentweave"),
                        spans=[
                            _span(
                                trace_id=b"\x11" * 16,
                                span_id=b"\x11" * 8,
                                name="cwd-attr",
                                start_ns=1715200000_000_000_000,
                                attrs=[
                                    _kv("session.id", "sess-cwd"),
                                    _kv("prov.agent.id", "claude-code-nas"),
                                    _kv(
                                        "prov.cwd",
                                        "/home/Arnab/clawd/projects/healthos/app",
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            )
        ]
    )

    stats = ingest_otlp_request(req, parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    assert stats.inserted == 1
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        row = con.execute(
            "SELECT repo_owner, repo_name FROM spans WHERE name='cwd-attr'"
        ).fetchone()
    finally:
        con.close()
    assert row == ("arniesaha", "healthos")


def test_ingest_persists_agentweave_openclaw_contract_columns(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _bootstrap(tmp_path)
    req = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(
                    attributes=[_kv("service.name", "agentweave-openclaw-bridge")]
                ),
                scope_spans=[
                    ScopeSpans(
                        scope=InstrumentationScope(name="agentweave"),
                        spans=[
                            _span(
                                trace_id=b"\x12" * 16,
                                span_id=b"\x12" * 8,
                                name="contract-cols",
                                start_ns=1715200000_000_000_000,
                                attrs=[
                                    _kv("prov.harness", "openclaw"),
                                    _kv("prov.session.id", "018f-openclaw-main-0001"),
                                    _kv("prov.session.key", "agent:main:main"),
                                    _kv("prov.cwd", "/tmp/nexus-demo"),
                                    _kv(
                                        "prov.repository",
                                        "https://github.com/example/nexus-demo.git",
                                    ),
                                    _kv("prov.routing.provider", "fake-provider"),
                                    _kv("prov.routing.model", "fake-model-small"),
                                    _kv("prov.routing.reason", "policy-test"),
                                    _kv("redaction.level", "preview"),
                                    _kv("sensitivity", "unknown"),
                                    _kv("prov.llm.prompt_preview", "P" * 2500),
                                ],
                            )
                        ],
                    )
                ],
            )
        ]
    )

    stats = ingest_otlp_request(req, parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    assert stats.inserted == 1
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        row = con.execute("""
            SELECT harness, session_id, session_key, cwd, repository,
                   routing_provider, routing_model, routing_reason,
                   redaction_level, sensitivity, preview_truncated, preview_bytes,
                   length(prompt_preview)
            FROM spans WHERE name='contract-cols'
            """).fetchone()
    finally:
        con.close()

    assert row == (
        "openclaw",
        "018f-openclaw-main-0001",
        "agent:main:main",
        "/tmp/nexus-demo",
        "https://github.com/example/nexus-demo.git",
        "fake-provider",
        "fake-model-small",
        "policy-test",
        "preview",
        "unknown",
        True,
        2000,
        2000,
    )


def test_spans_view_reads_old_partitions_with_new_columns_defaulted(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _bootstrap(tmp_path)
    old_part_dir = parquet_dir / "spans" / "date=2024-05-10"
    old_part_dir.mkdir(parents=True, exist_ok=True)
    old_schema = pa.schema(
        [
            ("trace_id", pa.string()),
            ("span_id", pa.string()),
            ("name", pa.string()),
            ("start_time", pa.timestamp("us", tz="UTC")),
            ("session_id", pa.string()),
            ("dedup_key", pa.string()),
        ]
    )
    pq.write_table(
        pa.table(
            {
                "trace_id": ["old-trace"],
                "span_id": ["old-span"],
                "name": ["old-row"],
                "start_time": [datetime(2024, 5, 10, tzinfo=timezone.utc)],
                "session_id": ["old-session"],
                "dedup_key": ["old-dedup"],
            },
            schema=old_schema,
        ),
        old_part_dir / "part-old.parquet",
    )

    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        row = con.execute("""
            SELECT harness, session_key, cwd, repository, preview_truncated, preview_bytes
            FROM spans WHERE span_id='old-span'
            """).fetchone()
    finally:
        con.close()

    assert row == (None, None, None, None, None, None)


def test_ingest_empty_request_no_writes(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _bootstrap(tmp_path)
    stats = ingest_otlp_request(
        ExportTraceServiceRequest(),
        parquet_dir=parquet_dir,
        duckdb_path=duckdb_path,
    )
    assert stats.read == 0
    assert stats.inserted == 0
