"""Tests for the OTLP proto → Tempo-style trace-dict adapter."""

from __future__ import annotations

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

from drover.parsers import parse_agentweave_trace
from drover.server.otlp.proto_adapter import otlp_request_to_trace_dict


def _kv(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


def _span(*, trace_id: bytes, span_id: bytes, name: str, attrs: list[KeyValue]) -> Span:
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        name=name,
        start_time_unix_nano=1715200000_000_000_000,  # 2024-05-08
        end_time_unix_nano=1715200001_000_000_000,
        attributes=attrs,
    )


def _request_with_one_span() -> ExportTraceServiceRequest:
    span = _span(
        trace_id=b"\x01" * 16,
        span_id=b"\x02" * 8,
        name="anthropic.messages",
        attrs=[
            _kv("session.id", "sess-otlp-1"),
            _kv("prov.agent.id", "macmini-claude"),
            _kv("prov.repo.owner", "arniesaha"),
            _kv("prov.repo.name", "nexus"),
            _kv("prov.git.branch", "main"),
            _kv("prov.llm.model", "claude-sonnet-4-6"),
        ],
    )
    return ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(attributes=[_kv("service.name", "agentweave-proxy")]),
                scope_spans=[
                    ScopeSpans(
                        scope=InstrumentationScope(name="agentweave"),
                        spans=[span],
                    )
                ],
            )
        ]
    )


def test_adapter_returns_batches_root() -> None:
    req = _request_with_one_span()
    out = otlp_request_to_trace_dict(req)
    assert "batches" in out
    assert isinstance(out["batches"], list)
    assert len(out["batches"]) == 1


def test_adapter_preserves_scope_spans_camelcase() -> None:
    req = _request_with_one_span()
    out = otlp_request_to_trace_dict(req)
    batch = out["batches"][0]
    # parse_agentweave_trace looks for "scopeSpans" or "instrumentationLibrarySpans"
    assert "scopeSpans" in batch
    assert len(batch["scopeSpans"][0]["spans"]) == 1


def test_adapter_round_trips_through_parser() -> None:
    """Adapter output must be consumable by parse_agentweave_trace."""
    req = _request_with_one_span()
    trace_dict = otlp_request_to_trace_dict(req)
    rows = parse_agentweave_trace(trace_dict, raw_object_uri="otlp://test")
    assert len(rows) == 1
    row = rows[0]
    assert row["trace_id"] == "01" * 16
    assert row["span_id"] == "02" * 8
    assert row["session_id"] == "sess-otlp-1"
    assert row["agent_id"] == "macmini-claude"
    assert row["service_name"] == "agentweave-proxy"


def test_adapter_handles_empty_request() -> None:
    out = otlp_request_to_trace_dict(ExportTraceServiceRequest())
    assert out == {"batches": []}


def test_adapter_multiple_batches() -> None:
    span_a = _span(trace_id=b"\xaa" * 16, span_id=b"\x01" * 8, name="a", attrs=[])
    span_b = _span(trace_id=b"\xbb" * 16, span_id=b"\x02" * 8, name="b", attrs=[])
    req = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(scope_spans=[ScopeSpans(spans=[span_a])]),
            ResourceSpans(scope_spans=[ScopeSpans(spans=[span_b])]),
        ]
    )
    out = otlp_request_to_trace_dict(req)
    assert len(out["batches"]) == 2
