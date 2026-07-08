"""Convert an OTLP gRPC `ExportTraceServiceRequest` into the Tempo-style
`{"batches": [...]}` dict that ``drover.parsers.parse_agentweave_trace``
already knows how to consume.

This is a deliberately thin adapter so the AgentWeave parsing logic
stays in one place — the same function handles both pull-from-Tempo and
push-from-OTLP paths.
"""

from __future__ import annotations

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)


def otlp_request_to_trace_dict(request: ExportTraceServiceRequest) -> dict:
    """Return ``{"batches": [...]}`` matching Tempo's GET /api/traces format.

    The OTLP proto field is ``resource_spans`` (Python) /
    ``resourceSpans`` (JSON); Tempo uses ``batches`` for the same payload.
    Everything inside (`scopeSpans`, span attributes, ids) lines up after
    protobuf JSON serialization.
    """
    payload = MessageToDict(
        request,
        preserving_proto_field_name=False,
        use_integers_for_enums=False,
    )
    batches = payload.get("resourceSpans", [])
    return {"batches": batches}
