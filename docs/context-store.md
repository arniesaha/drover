# Context Store

Drover's context store preserves what happened, what is running, what was
derived, and how each derived result was produced. It uses Parquet for durable
telemetry facts and DuckDB for query views plus mutable serving state.

## Design Goals

- Local-first: the default store lives under `~/.drover/`.
- Replayable: normalized views and derived work can be rebuilt from durable
  facts and ledger intent.
- Provenance-aware: summaries, decisions, embeddings, and curated records link
  back to their source session, span, task, or receipt.
- Source-preserving: adapters normalize common fields without discarding
  source-native identifiers or raw metadata.
- Compatibility-conscious: historical `nexus.*` telemetry remains readable
  while new public interfaces use Drover.

## Data Model

### 1. Durable Facts

Partitioned Parquet is the system of record for append-oriented telemetry:

| Dataset | Identity and purpose |
| --- | --- |
| `agent_events` | Agent turn, tool, lifecycle, and harness events with a stable `dedup_key` |
| `spans` | OTLP trace/span facts with timing, model, token, cost, cache, and provenance attributes |
| `pr_events` | Pull-request lifecycle facts |
| `routing` | Model or harness routing observations |

DuckDB creates normalized views over those files: `agent_events`, `spans`,
`spans_enriched`, `pr_events`, and `routing`. The `sessions`,
`active_sessions`, `session_links`, and `openclaw_span_links` views derive
cross-source relationships without rewriting the fact rows.

### 2. Operational State

The command plane keeps mutable state in a **separate DuckDB database**,
`drover.registry.duckdb`, beside the lakehouse file:

| Table | Purpose |
| --- | --- |
| `harness_hosts` | Host identity, connection kind, capabilities, status, heartbeat |
| `harness_sessions` | Running or completed session metadata and native resume identity |
| `harness_events` | Ordered structured/terminal event envelope for a harness session |
| `live_recap_jobs` | Durable queue for incremental live session recaps |
| `live_session_recaps` | Latest recap projection per live session |

`tasks`, the logical repository/task rollup across one or more sessions, stays
in the lakehouse file with the analytical tables.

The separation is deliberate and load-bearing. A DuckDB database file is one
in-process instance: one task scheduler, one buffer manager, and one
`memory_limit` shared by every connection to it. While the command plane lived
in the lakehouse file, any background scan over the parquet-backed views could
saturate that instance and starve fleet endpoints, which is what repeatedly
took `/harness*` from milliseconds to timeouts while `/healthz` stayed instant.
Its own file gives it its own instance and its own budget.

Analytical queries that need command-plane state - the advisory snapshot and
the cockpit activity rollup - attach a private copy of that (small) database
rather than the live file, for the same reason the quality snapshot copies the
lakehouse.

The host daemon remains authoritative for live processes. Registry rows describe
and route those processes; they do not replace host-local process state.

### 3. Derived Context

Workers and explicit curation create mutable context records:

| Table | Derived value |
| --- | --- |
| `session_summaries` | Closed-session summary, files, tools, next steps, questions |
| `active_session_briefs` | Short-lived handoff brief for an open session |
| `project_briefs` | Project-level state synthesized from recent sessions |
| `decisions` | Extracted decision, rationale, alternatives, and selected action |
| `session_embeddings` | Vector over a session summary |
| `span_embeddings` | Vector plus exact redacted span source fields |
| `context_containers` | Confidence-aware grouping for code, operations, research, or general activity |
| `curated_context_records` | User- or tool-curated Markdown context with stable hashes |
| `curated_context_provenance` | Append-only generated, edited, and imported events |

Derived records are replaceable projections, not new raw truth. Their generator
model, timestamps, source identifiers, hashes, or provenance events make the
derivation inspectable.

### 4. Pipeline Provenance

The durable pipeline ledger separates intent from execution:

| Table | Question answered |
| --- | --- |
| `pipeline_receipts` | What source unit arrived, and was it applied, duplicated, quarantined, or failed? |
| `pipeline_jobs` | What logical work should run, and what is its current state? |
| `pipeline_job_attempts` | Which worker ran each attempt, and how did it finish? |
| `pipeline_artifacts` | Which durable output was produced, and which version is current? |

Compatibility job tables (`summarize_jobs`, `brief_jobs`, `embed_jobs`, and
`span_embed_jobs`) remain while worker coordination evolves. Optional Redis
Streams provides leases, retries, backpressure, and dead-letter coordination;
DuckDB remains the durable source of job intent and results.

## Identity And Linking

- `session_id` identifies a source or harness session. Native provider IDs are
  retained separately when they differ.
- `task_id` groups work, normally from repository owner, repository name, and
  branch unless a source supplies an explicit task identifier.
- `trace_id` and `span_id` retain distributed-tracing identity.
- Repository attribution is captured on the source host while its `cwd` and
  Git remote are resolvable.
- `session_links` and integration-specific link views reconcile namespaces
  without mutating the original facts.
- `context_id` identifies a resumable context container even when no source
  repository exists.

## Storage Layout

```text
~/.drover/
├── config.toml
├── api_token
├── incoming/<source>/
├── parquet/
│   ├── agent_events/date=YYYY-MM-DD/agent_id=<id>/part-*.parquet
│   ├── spans/date=YYYY-MM-DD/part-*.parquet
│   ├── pr_events/part-*.parquet
│   └── routing/part-*.parquet
├── raw_objects/
├── drover.duckdb
└── drover.registry.duckdb
```

`drover.registry.duckdb` is the command-plane store described above. Its
location can be overridden with `DROVER_CONTROL_PLANE_DUCKDB`; by default it
sits beside the lakehouse file and is named after it.

`raw_objects/` stores large payloads referenced by URI rather than copied into
every row. Keep the whole directory private; context may contain prompts,
responses, paths, diffs, and tool output.

## Query Surface

Applications use the harness API for live control. Agents use `drover_*` MCP
tools for fleet state, replay, search, recall, summaries, briefs, files touched,
handoff, and quality checks. Operators can use `drover-server status`,
`drover-server doctor`, and the observability endpoints for local diagnostics.

The Python schema definitions in `src/drover/schema.py` and
`src/drover/server/harness/schema.py` are authoritative when this overview and
the implementation differ.
