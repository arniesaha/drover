"""Tempo → OTLP gRPC relay.

Periodically polls Grafana Tempo's HTTP API for new traces emitted by
AgentWeave (and Mux), repackages each trace as an OTLP
``ExportTraceServiceRequest``, and pushes it to the lakehouse's OTLP gRPC
receiver (``drover-server`` on :4317). Tempo remains the source of truth
for live observability; this relay is purely a downstream feed into the
local DuckDB lakehouse so the ``spans`` table stays current.

Design notes
------------
* Tempo's ``/api/traces/{id}`` returns OTLP-shaped JSON: a top-level
  ``batches`` array whose entries are ``ResourceSpans`` protos in JSON
  form (``resource`` + ``scopeSpans`` with ``spans`` inside).
* The mac-mini OTLP receiver (:4317) accepts standard gRPC
  ``ExportTraceServiceRequest``s and already handles dedup, parquet
  partitioning, and DuckDB merge. So this relay only needs to do
  JSON → proto + gRPC push.
* A single cursor at ``<state_dir>/tempo_relay.cursor`` stores
  ``last_end_iso`` (the last successful window's right edge). Each tick
  picks ``start = last_end - lookback`` to tolerate Tempo's eventual-
  consistency around fresh spans.
"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

import grpc
import requests
from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc import (
    TraceServiceStub,
)
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans

from drover.collect.cursor import CursorStore

log = logging.getLogger("drover.collect.tempo_relay")

CURSOR_KEY = "tempo_relay"

DEFAULT_SERVICES = ("agentweave-proxy", "mux-router")
DEFAULT_LOOKBACK_S = 60
DEFAULT_SEARCH_LIMIT = 1000
DEFAULT_TIMER_WINDOW_S = 3600
DEFAULT_SEARCH_TIMEOUT_S = 30.0
DEFAULT_FETCH_TIMEOUT_S = 60.0
DEFAULT_PUSH_TIMEOUT_S = 30.0


# ---------------------------------------------------------------------------
# Tempo HTTP API
# ---------------------------------------------------------------------------


def _build_traceql(services: Iterable[str]) -> str:
    """Return a TraceQL clause matching any of the given service names."""
    parts = [f'resource.service.name = "{s}"' for s in services if s]
    if not parts:
        raise ValueError("at least one service must be provided")
    return "{ " + " || ".join(parts) + " }"


_session = requests.Session()
_session.headers.update({"Accept": "application/json"})


def search_traces(
    tempo_base: str,
    *,
    services: Iterable[str],
    start_epoch: int,
    end_epoch: int,
    limit: int = DEFAULT_SEARCH_LIMIT,
    timeout: float = 30.0,
    session: Optional[requests.Session] = None,
) -> list[str]:
    """Return trace IDs matching ``services`` whose root falls in the window.

    Uses a module-level keep-alive Session by default — backfill runs over
    1000s of traces, and a per-request connection setup multiplies wall
    time several-fold.
    """
    s = session or _session
    params = {
        "q": _build_traceql(services),
        "start": str(start_epoch),
        "end": str(end_epoch),
        "limit": str(limit),
    }
    resp = s.get(f"{tempo_base}/api/search", params=params, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    seen: set[str] = set()
    out: list[str] = []
    for t in body.get("traces", []) or []:
        tid = t.get("traceID")
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def fetch_trace(
    tempo_base: str,
    trace_id: str,
    *,
    timeout: float = 60.0,
    session: Optional[requests.Session] = None,
) -> dict:
    """Return the raw OTLP-shaped JSON for a single trace."""
    s = session or _session
    resp = s.get(
        f"{tempo_base}/api/traces/{trace_id}",
        params={"format": "json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# JSON → OTLP protobuf
# ---------------------------------------------------------------------------


def trace_json_to_resource_spans(trace_json: dict) -> list[ResourceSpans]:
    """Convert a Tempo ``/api/traces`` JSON response to ``ResourceSpans`` protos.

    Tempo returns ``{"batches": [<ResourceSpans>, ...]}``. ``json_format``
    accepts both camelCase (which Tempo emits) and snake_case field names,
    and tolerates unknown fields if we ask it to.
    """
    batches = trace_json.get("batches") or []
    out: list[ResourceSpans] = []
    for batch in batches:
        try:
            msg = json_format.ParseDict(
                batch, ResourceSpans(), ignore_unknown_fields=True
            )
        except json_format.ParseError as exc:
            log.warning("skip malformed ResourceSpans batch: %s", exc)
            continue
        out.append(msg)
    return out


def build_export_request(
    resource_spans: Iterable[ResourceSpans],
) -> ExportTraceServiceRequest:
    req = ExportTraceServiceRequest()
    for rs in resource_spans:
        req.resource_spans.append(rs)
    return req


# ---------------------------------------------------------------------------
# OTLP gRPC push
# ---------------------------------------------------------------------------


@dataclass
class RelayStats:
    traces_seen: int = 0
    traces_fetched: int = 0
    spans_sent: int = 0
    push_calls: int = 0
    errors: list[str] = field(default_factory=list)
    zero_span_traces: int = 0
    zero_span_trace_ids: list[str] = field(default_factory=list)


def _supports_kwarg(func, name: str) -> bool:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return True
    for p in sig.parameters.values():
        if p.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if p.name == name:
            return True
    return False


def _count_spans(req: ExportTraceServiceRequest) -> int:
    n = 0
    for rs in req.resource_spans:
        for ss in rs.scope_spans:
            n += len(ss.spans)
    return n


def push_otlp(
    target: str,
    request: ExportTraceServiceRequest,
    *,
    timeout: float = 30.0,
    insecure: bool = True,
) -> ExportTraceServiceResponse:
    """Send one ``ExportTraceServiceRequest`` to an OTLP gRPC receiver."""
    if insecure:
        channel = grpc.insecure_channel(target)
    else:
        channel = grpc.secure_channel(target, grpc.ssl_channel_credentials())
    try:
        stub = TraceServiceStub(channel)
        return stub.Export(request, timeout=timeout)
    finally:
        channel.close()


# ---------------------------------------------------------------------------
# Relay orchestration
# ---------------------------------------------------------------------------


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _parse_iso(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    return datetime.fromisoformat(s).timestamp()


def relay_window(
    *,
    tempo_base: str,
    target_otlp: str,
    services: Iterable[str],
    start_epoch: int,
    end_epoch: int,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
    search_timeout_s: float = DEFAULT_SEARCH_TIMEOUT_S,
    fetch_timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
    push_timeout_s: float = DEFAULT_PUSH_TIMEOUT_S,
    pusher=None,
    searcher=None,
    fetcher=None,
) -> RelayStats:
    """Pull a single explicit window and push it. Used by both ``relay_once``
    (live tick) and ``backfill`` (catch-up replay). Does not touch any cursor.
    """
    pusher = pusher or push_otlp
    searcher = searcher or search_traces
    fetcher = fetcher or fetch_trace

    stats = RelayStats()
    try:
        search_kwargs = {
            "services": list(services),
            "start_epoch": start_epoch,
            "end_epoch": end_epoch,
            "limit": search_limit,
        }
        if _supports_kwarg(searcher, "timeout"):
            search_kwargs["timeout"] = search_timeout_s
        trace_ids = searcher(
            tempo_base,
            **search_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"tempo search failed: {exc}"
        log.error(msg)
        stats.errors.append(msg)
        return stats

    stats.traces_seen = len(trace_ids)
    if not trace_ids:
        return stats

    resource_spans: list[ResourceSpans] = []
    for tid in trace_ids:
        try:
            fetch_kwargs = {}
            if _supports_kwarg(fetcher, "timeout"):
                fetch_kwargs["timeout"] = fetch_timeout_s
            tj = fetcher(tempo_base, tid, **fetch_kwargs)
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"fetch {tid} failed: {exc}")
            continue
        spans = trace_json_to_resource_spans(tj)
        if not spans:
            stats.zero_span_traces += 1
            stats.zero_span_trace_ids.append(tid)
            log.warning("tempo_relay: trace %s had zero spans", tid)
            continue
        resource_spans.extend(spans)
        stats.traces_fetched += 1

    if not resource_spans:
        return stats

    request = build_export_request(resource_spans)
    span_count = _count_spans(request)

    try:
        push_kwargs = {}
        if _supports_kwarg(pusher, "timeout"):
            push_kwargs["timeout"] = push_timeout_s
        pusher(target_otlp, request, **push_kwargs)
    except Exception as exc:  # noqa: BLE001
        stats.errors.append(f"otlp push to {target_otlp} failed: {exc}")
        return stats

    stats.spans_sent = span_count
    stats.push_calls = 1
    return stats


def backfill(
    *,
    tempo_base: str,
    target_otlp: str,
    services: Iterable[str],
    start_epoch: int,
    end_epoch: int,
    chunk_seconds: int = 3600,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
    search_timeout_s: float = DEFAULT_SEARCH_TIMEOUT_S,
    fetch_timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
    push_timeout_s: float = DEFAULT_PUSH_TIMEOUT_S,
    progress=None,
    pusher=None,
    searcher=None,
    fetcher=None,
) -> RelayStats:
    """Replay a closed time range in chunked windows.

    Tempo's ``/api/search`` caps each call at ~1000 results, so we walk
    ``[start_epoch, end_epoch)`` in ``chunk_seconds`` slices. Each slice
    is an independent relay_window — failures in one slice don't stop
    the rest, but they accumulate into the returned ``stats.errors``.
    """
    if end_epoch <= start_epoch:
        raise ValueError("end_epoch must be > start_epoch")
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be positive")

    total = RelayStats()
    cursor = start_epoch
    chunk_num = 0
    while cursor < end_epoch:
        chunk_num += 1
        chunk_end = min(cursor + chunk_seconds, end_epoch)
        s = relay_window(
            tempo_base=tempo_base,
            target_otlp=target_otlp,
            services=services,
            start_epoch=cursor,
            end_epoch=chunk_end,
            search_limit=search_limit,
            search_timeout_s=search_timeout_s,
            fetch_timeout_s=fetch_timeout_s,
            push_timeout_s=push_timeout_s,
            pusher=pusher,
            searcher=searcher,
            fetcher=fetcher,
        )
        total.traces_seen += s.traces_seen
        total.traces_fetched += s.traces_fetched
        total.spans_sent += s.spans_sent
        total.push_calls += s.push_calls
        total.errors.extend(s.errors)
        if progress is not None:
            progress(chunk_num, cursor, chunk_end, s)
        cursor = chunk_end
    return total


def relay_once(
    *,
    tempo_base: str,
    target_otlp: str,
    services: Iterable[str] = DEFAULT_SERVICES,
    state: CursorStore,
    lookback_s: int = DEFAULT_LOOKBACK_S,
    initial_window_s: int = 3600,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
    max_window_s: int = DEFAULT_TIMER_WINDOW_S,
    search_timeout_s: float = DEFAULT_SEARCH_TIMEOUT_S,
    fetch_timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
    push_timeout_s: float = DEFAULT_PUSH_TIMEOUT_S,
    now_epoch: Optional[float] = None,
    pusher=push_otlp,
    searcher=search_traces,
    fetcher=fetch_trace,
) -> RelayStats:
    """Pull one Tempo window forward and push it to the OTLP receiver.

    On success the cursor advances to ``end``. On any push error the
    cursor stays put so the next run retries the same window.
    """
    services = list(services)
    now = now_epoch if now_epoch is not None else time.time()
    cursor = state.read(CURSOR_KEY)
    last_end = _parse_iso(cursor.get("last_end_iso"))
    if last_end is None:
        start = now - initial_window_s
    else:
        start = last_end - lookback_s

    start_epoch = int(start)
    end_epoch = int(now)
    if max_window_s > 0:
        min_start = end_epoch - max_window_s
        if start_epoch < min_start:
            log.warning(
                "tempo_relay: bounding timer window to %ds (requested_start=%s requested_end=%s)",
                max_window_s,
                _iso(start_epoch),
                _iso(end_epoch),
            )
            start_epoch = min_start

    if end_epoch <= start_epoch:
        log.info("tempo_relay: window has zero length; nothing to do")
        return RelayStats()

    stats = relay_window(
        tempo_base=tempo_base,
        target_otlp=target_otlp,
        services=services,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        search_limit=search_limit,
        search_timeout_s=search_timeout_s,
        fetch_timeout_s=fetch_timeout_s,
        push_timeout_s=push_timeout_s,
        pusher=pusher,
        searcher=searcher,
        fetcher=fetcher,
    )

    # Advance the cursor only when nothing failed catastrophically. A search
    # failure (no trace_ids ever returned) leaves the cursor put so the next
    # tick retries. An empty-but-successful window or a fully-successful push
    # both move the cursor forward.
    search_failed = any(e.startswith("tempo search failed") for e in stats.errors)
    push_failed = any(e.startswith("otlp push to") for e in stats.errors)
    if not search_failed and not push_failed:
        state.write(
            CURSOR_KEY,
            {
                "last_end_iso": _iso(end_epoch),
                "last_run_iso": _iso(now),
            },
        )

    log.info(
        "tempo_relay: window %s..%s — traces=%d/%d spans=%d target=%s errors=%d zero_span_traces=%d",
        _iso(start_epoch),
        _iso(end_epoch),
        stats.traces_seen,
        stats.traces_fetched,
        stats.spans_sent,
        target_otlp,
        len(stats.errors),
        stats.zero_span_traces,
    )
    return stats
