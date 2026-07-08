"""Tests for OTLPReceiver — gRPC end-to-end."""

from __future__ import annotations

from pathlib import Path

import duckdb
import grpc
import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as ts
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc as tsg
from opentelemetry.proto.common.v1.common_pb2 import (
    AnyValue,
    InstrumentationScope,
    KeyValue,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

from drover.schema import bootstrap
from drover.server.otlp.receiver import OTLPReceiver


def _kv(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


def _make_request(
    trace_id: bytes = b"\x77" * 16, span_id: bytes = b"\x88" * 8
) -> ts.ExportTraceServiceRequest:
    span = Span(
        trace_id=trace_id,
        span_id=span_id,
        name="anthropic.messages",
        start_time_unix_nano=1715200000_000_000_000,
        end_time_unix_nano=1715200001_000_000_000,
        attributes=[
            _kv("session.id", "sess-rcv-1"),
            _kv("prov.agent.id", "macmini-claude"),
            _kv("prov.repo.owner", "arniesaha"),
            _kv("prov.repo.name", "nexus"),
            _kv("prov.git.branch", "main"),
        ],
    )
    return ts.ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(attributes=[_kv("service.name", "agentweave-proxy")]),
                scope_spans=[
                    ScopeSpans(
                        scope=InstrumentationScope(name="agentweave"), spans=[span]
                    )
                ],
            )
        ]
    )


@pytest.fixture
def lakehouse(tmp_path: Path):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return parquet_dir, duckdb_path


def _start_receiver(parquet_dir: Path, duckdb_path: Path) -> OTLPReceiver:
    receiver = OTLPReceiver(
        host="127.0.0.1", port=0, parquet_dir=parquet_dir, duckdb_path=duckdb_path
    )
    receiver.start()
    return receiver


def test_receiver_accepts_export_and_writes_span(lakehouse) -> None:
    parquet_dir, duckdb_path = lakehouse
    receiver = _start_receiver(parquet_dir, duckdb_path)
    try:
        with grpc.insecure_channel(f"127.0.0.1:{receiver.port}") as channel:
            stub = tsg.TraceServiceStub(channel)
            resp = stub.Export(_make_request(), timeout=5.0)
            assert isinstance(resp, ts.ExportTraceServiceResponse)
    finally:
        receiver.stop(grace=1.0)

    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        n = con.execute(
            "SELECT count(*) FROM spans WHERE dedup_key IS NOT NULL"
        ).fetchone()[0]
        assert n == 1
    finally:
        con.close()


def test_receiver_returns_ok_on_ingest_error(lakehouse, monkeypatch) -> None:
    """Even if ingestion blows up, Export must return OK so clients don't retry-storm."""
    parquet_dir, duckdb_path = lakehouse

    from drover.server.otlp import receiver as recv_mod

    def boom(*args, **kwargs):
        raise RuntimeError("simulated ingest crash")

    monkeypatch.setattr(recv_mod, "ingest_otlp_request", boom)
    receiver = _start_receiver(parquet_dir, duckdb_path)
    try:
        with grpc.insecure_channel(f"127.0.0.1:{receiver.port}") as channel:
            stub = tsg.TraceServiceStub(channel)
            resp = stub.Export(_make_request(), timeout=5.0)
            assert isinstance(resp, ts.ExportTraceServiceResponse)
    finally:
        receiver.stop(grace=1.0)


def test_receiver_stop_is_idempotent(lakehouse) -> None:
    parquet_dir, duckdb_path = lakehouse
    receiver = _start_receiver(parquet_dir, duckdb_path)
    receiver.stop(grace=1.0)
    receiver.stop(grace=1.0)  # should not raise


def test_receiver_port_is_assigned_after_start(lakehouse) -> None:
    parquet_dir, duckdb_path = lakehouse
    receiver = _start_receiver(parquet_dir, duckdb_path)
    try:
        assert receiver.port > 0
    finally:
        receiver.stop(grace=1.0)
