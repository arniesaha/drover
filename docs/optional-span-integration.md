# Optional Span Integration

Drover can ingest OpenTelemetry spans from an external producer, such as an
LLM proxy that exports to Tempo and a `drover-collect tempo-relay` that
forwards to the hub's OTLP receiver. This is an **optional, legacy
integration**. It is off by default and no core feature depends on it.

## Why It Is Off

Spans only ever covered model calls routed through an external proxy. They did
not cover Drover-launched or native harness sessions. The relay chain also
failed silently for days at a time, and span cost was never priced (#288).
Since #473 the release surface is built only on data Drover records itself:
harness launches and session state, agent events, session summaries and the
`session_usage` token rollup.

## What Changes When It Is Off (Default)

| Surface | Behaviour |
| --- | --- |
| OTLP receiver (`:4317`) | Not started. `drover-server run --no-otlp` is implied. |
| Cockpit activity | Sessions and tokens from events and `session_usage`. Cost and latency report zero coverage and the app hides them. Span Parquet is never read. |
| Span embeddings | Removed from the core memory path in Phase 3 (#480), regardless of this flag. Existing data is preserved. |
| `drover_recall` | Session-summary hits only; span-embedding hits are not unioned in. |
| `drover-server trace-tail`, `recent-traces`, `decisions derive` | Exit with a message naming the flag. |
| `drover-server session graph` | Delegation tree from launch metadata. `--spans` (legacy span tree) needs the flag. |
| `drover_project_activity` (MCP) | Event and summary based; never reads spans regardless of the flag. |
| `doctor` / `data_quality` / advisory | Span checks report `disabled` rather than a failure. |

The legacy span embedding maintenance commands have been removed.

Historical span Parquet under `spans/` and every span table remain on disk.
Nothing is deleted and no schema is dropped.

## Turning It On

```toml
[telemetry]
spans_enabled = true

[server]
otlp_grpc_port = 4317
```

Restart the hub. The receiver binds to loopback unless `--otlp-host` is given.
Only the literal boolean `true` opts in.

`drover-collect tempo-relay` remains available to forward spans from Tempo.
Nothing in core requires it, and a hub that is not running it is healthy.
