# Plan 3 — `nexus-server` OTLP gRPC Receiver

**Status:** In implementation
**Date:** 2026-05-09
**Spec:** `docs/superpowers/specs/2026-05-08-nexus-architecture-redesign-design.md` §3.1, §5.1

---

## Goal

Stand up the OTLP gRPC receiver inside `nexus-server`. AgentWeave's proxy on
NAS k3s adds an OTLP exporter pointing at `mac-mini.local:4317`; spans land
in DuckDB's `spans` Parquet partition (Hive-partitioned by `date=YYYY-MM-DD`)
and are merged idempotently via `dedup_key = (trace_id, span_id)`.

The existing `parse_agentweave_trace` parser in `src/nexus/parsers.py` already
knows how to extract spans from a Tempo-style `{"batches": [...]}` JSON
trace; this plan adds the OTLP→trace-dict adapter and the gRPC plumbing.

The AgentWeave-side change (k3s deployment adds OTLP exporter) is out of
scope for this plan — it lives in the agentweave repo and ships separately.

---

## Non-goals

- Modifying AgentWeave's k3s deployment to push to `mac-mini.local:4317`
  (separate PR in arniesaha/agentweave).
- Ports / TLS / authn — the receiver listens on `0.0.0.0:4317` plaintext.
  Local-network only.
- Tempo decommission or BigQuery retire (Plan 7).

---

## Module layout

```
src/nexus/server/otlp/
  __init__.py
  proto_adapter.py   # otlp_request_to_trace_dict(request) → {"batches": [...]}
  ingest.py          # ingest_otlp_request(req, *, parquet_dir, duckdb_path) → IngestStats
  receiver.py        # OTLPReceiver class — gRPC server start/stop + Servicer impl

src/nexus/parsers.py # adds dedup_key per span; otherwise unchanged

tests/
  test_otlp_adapter.py
  test_otlp_ingest.py
  test_otlp_receiver.py
```

---

## Tasks (TDD)

### T1. OTLP proto → trace-dict adapter

**File:** `src/nexus/server/otlp/proto_adapter.py`
**Test:** `tests/test_otlp_adapter.py`

```python
def otlp_request_to_trace_dict(request: ExportTraceServiceRequest) -> dict:
    """Convert OTLP gRPC request to the Tempo-style {"batches": [...]} dict
    that parse_agentweave_trace consumes."""
```

Uses `google.protobuf.json_format.MessageToDict(preserving_proto_field_name=False)`
to get camelCase JSON, then renames `resourceSpans` → `batches`.

Tests: synthetic request with one span → adapter produces a dict that
`parse_agentweave_trace` parses into a single row.

### T2. ingest_otlp_request

**File:** `src/nexus/server/otlp/ingest.py`
**Test:** `tests/test_otlp_ingest.py`

```python
def ingest_otlp_request(
    request: ExportTraceServiceRequest,
    *,
    parquet_dir: Path,
    duckdb_path: Path,
    raw_object_uri: str = "otlp://stream",
) -> IngestStats:
    """Convert → parse → compute dedup_key → write per-day Parquet → MERGE-ish dedup via row append + global compact (deferred)."""
```

Writes one Parquet file per call to
`<parquet_dir>/spans/date=YYYY-MM-DD/part-<run-id>.parquet`, partitioned by the
**span start_time date**. dedup_key derived as
`sha256(trace_id|span_id)[:32]`. If the spans Parquet view has the
dedup_key already, the new row is dropped before write (DuckDB
`ANTI JOIN` against `spans` view on `dedup_key`).

`tasks` table is upserted in the same way as `agent_events` ingest does
(if the span carries `prov.repo.owner` / `prov.repo.name` /
`prov.git.branch` attributes).

Tests:
- Synthetic batch with 3 spans → 3 rows in `spans` view after ingest
- Re-running same request → no new rows (idempotent)
- Span without `start_time` → skipped (logged), no row
- Spans across two days → two Parquet files in two date partitions

### T3. OTLPReceiver gRPC server

**File:** `src/nexus/server/otlp/receiver.py`
**Test:** `tests/test_otlp_receiver.py`

```python
class OTLPReceiver:
    def __init__(self, *, host: str, port: int, parquet_dir: Path, duckdb_path: Path,
                 max_workers: int = 4):
    def start(self) -> None:
    def stop(self, grace: float = 5.0) -> None:
```

Implements `TraceServiceServicer.Export` → calls `ingest_otlp_request`,
returns `ExportTraceServiceResponse()`. Errors are logged but Export still
returns OK so the client doesn't retry-storm.

Tests:
- start receiver on port 0 → grpc client `Export` succeeds → row in spans
- stop is idempotent
- malformed request handled (raises in ingest → Export returns OK,
  warning logged, no crash)

### T4. Wire OTLPReceiver into `nexus-server run`

**File:** `src/nexus/server/__main__.py`

Add `otlp_grpc_port` from existing config (already present). In `run`,
start `OTLPReceiver` alongside `IncomingWatcher`. Both stop cleanly on
SIGINT/SIGTERM.

CLI flag `--no-otlp` to disable for environments where the port can't be
bound (CI, etc).

### T5. Spans seed parquet bootstrap

**File:** `src/nexus/schema.py`

Extend `_ensure_seed_parquet` to also create
`<parquet_dir>/spans/date=_seed/part-empty.parquet` with the minimum
schema (trace_id, span_id, dedup_key, start_time, attributes_json) so the
spans view doesn't error on cold start.

### T6. Dependency + smoke test

- Add `opentelemetry-proto>=1.30` to `pyproject.toml`.
- Smoke test: spin up receiver in a thread, send a synthetic
  `ExportTraceServiceRequest` via stub, assert spans table grows.

---

## Acceptance

- All new tests pass.
- Existing 124 tests still pass.
- `nexus-server run` starts OTLP receiver on configured port; SIGINT
  shuts both watcher and receiver down cleanly.
- Synthetic `ExportTraceServiceRequest` with N spans → N rows visible
  in `SELECT count(*) FROM spans` (after `bootstrap` re-init).
- Idempotent: same request twice → still N rows.
