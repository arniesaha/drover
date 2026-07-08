# Nexus Pipeline Foundations Review

Status: proposal  
Date: 2026-06-19

## Why This Exists

Nexus has grown from a local archive into a context lakehouse with live shippers,
MCP tools, summaries, embeddings, spans, and Paperclip-delegated work. The
system is useful, but the recent quality audits show that too many reliability
properties are implicit. This document steps back and defines the basics Nexus
needs before adding more derived intelligence.

The goal is not to turn Nexus into Kafka, Airflow, Langfuse, or Paperclip. Nexus
should remain the local-first context store and handoff layer. The pipeline
underneath it should become boring, observable, replayable, and easy to reason
about.

## Current Shape

Current durable facts:

- Source hosts parse local harness logs and ship JSONL into
  `~/.nexus/incoming/<host>/`.
- `IncomingWatcher` ingests JSONL into Hive-partitioned Parquet under
  `agent_events/date=.../agent_id=...`.
- AgentWeave/Mux/OpenTelemetry spans land in Parquet under `spans/date=...`.
- DuckDB owns mutable derived tables: `tasks`, `session_summaries`,
  `project_briefs`, `session_embeddings`, `span_embeddings`, and job tables.
- MCP tools serve handoff, replay, project brief, recall, and quality snapshots.

Current quality state from the June 19 audit:

- Ingestion freshness: healthy.
- Incoming backlog: healthy.
- Canonical event identity: healthy on `dedup_key`.
- Session consistency: healthy.
- Summary coverage: degraded.
- Bundle quality: degraded.
- Span embedding coverage: degraded.
- Summarizer runtime config: degraded, with retryable Ollama connection errors.

## Failure Patterns We Keep Seeing

### 1. No Explicit Pipeline Ledger

The system has durable fact tables and worker queues, but no single ledger of
"this unit of work entered stage X, became Y, failed at Z, and was retried by
worker W." Today, the truth is spread across files, DuckDB job rows, logs, and
quality snapshots.

Impact:

- Hard to distinguish source outage from worker outage.
- Hard to know whether a missing summary is queued, failed, skipped, or never
  discovered.
- Paperclip can mark work done while durable GitHub or Nexus state disagrees.

### 2. Queue Semantics Are Too Weak

DuckDB job tables (`summarize_jobs`, `embed_jobs`, `span_embed_jobs`,
`brief_jobs`) work for simple local polling, but they do not encode enough
operational semantics:

- no visibility timeout
- no dead-letter queue
- no retry policy per error class
- no lease owner or heartbeat
- no monotonic stage transitions
- no easy consumer-group view

Impact:

- `running` can become stale.
- Runtime errors accumulate as `errored` without a clear DLQ/retry lane.
- Workers need custom recovery logic.

### 3. Watermarks Are Per-Source But Not Audited End-to-End

Collectors track source watermarks and only advance after shipping. That is
good. But Nexus does not yet tie together:

- source file discovery watermark
- staged file hash
- shipped file
- ingested Parquet partitions
- processed-file move
- downstream job enqueue

Impact:

- A shipped file and a processed file can be audited only indirectly.
- Backfills are harder to prove correct.
- Late or corrected source events depend on dedupe behavior but do not show up
  as first-class pipeline facts.

### 4. Derived Context Is Treated Like Best Effort

Summaries, briefs, embeddings, and bundles are the product surface for handoff,
but their production path behaves like optional background work. Quality reports
detect failures after the fact; the pipeline does not yet guarantee freshness or
coverage targets.

Impact:

- Recall/handoff can be "mostly working" while the exact project Arnab cares
  about is stale.
- Bundle quality can stay partially degraded for a long time.

### 5. Attribution Is Still a Runtime Guess in Some Places

Repo/project attribution is much better than the original system, but some
spaces still depend on fallback inference:

- Paperclip workspace paths under `/paperclip/home/...`
- AgentWeave/Mux spans with provenance-only metadata
- general workspace activity that is intentionally not repo-bound

Impact:

- Quality reports need nuanced exceptions.
- Delegated work can show up as genuine unknown even when a human can tell what
  project it belongs to.

### 6. Paperclip Delegation Lacks Durable Completion Gates

Paperclip can delegate and execute work, but Nexus needs stricter gates before
accepting "done":

- branch or commit pushed
- PR opened or explicit no-PR rationale
- GitHub issue updated or closed
- tests captured
- Nexus quality snapshot captured
- Paperclip issue comment links durable artifacts

Impact:

- Paperclip board status can drift from GitHub and repo state.
- Useful work can remain only inside a pod/workspace until recovered.

## Proposed Target Architecture

Keep the current durable lakehouse, but add explicit runtime coordination.

```
Sources
  -> source cursors
  -> staged JSONL + file manifest
  -> Redis Streams: ingest units
  -> ingest workers
  -> Parquet fact tables
  -> Redis Streams: derived jobs
  -> summarize / brief / embed / quality workers
  -> DuckDB derived tables
  -> MCP serving tools
```

Parquet remains the source of truth for telemetry facts. DuckDB remains the
local analytical/serving database. Redis becomes the runtime coordination layer:
streams, consumer groups, leases, retries, DLQs, counters, and freshness signals.

## Why Redis Fits Here

Redis is worth introducing if it teaches and improves the exact areas relevant
to the Redis interview:

- streams as append-only event logs
- consumer groups for independent worker ownership
- pending entry lists as visibility timeout equivalents
- `XACK` / `XCLAIM` / `XAUTOCLAIM` for retry and recovery
- DLQ streams for poison messages
- sorted sets for scheduled retry/backoff
- hashes for pipeline state/materialized status
- counters and gauges for freshness/backpressure
- idempotency keys for exactly-once effects over at-least-once delivery

Redis should not replace:

- Parquet durable fact storage
- DuckDB analytical serving tables
- local filesystem raw/source archive

## Redis Layer Design

### Streams

- `nexus:ingest:v1`
  - one message per staged source file or source batch
  - fields: `source_id`, `host_id`, `run_id`, `file_path`, `file_sha256`,
    `watermark_start`, `watermark_end`, `event_count`, `created_at`

- `nexus:derive:summarize:v1`
  - one message per session needing summary
  - fields: `session_id`, `task_id`, `agent_id`, `repo_owner`, `repo_name`,
    `reason`, `attempt`, `created_at`

- `nexus:derive:brief:v1`
  - one message per project needing brief regeneration
  - fields: `project_key`, `reason`, `last_activity_at`, `created_at`

- `nexus:derive:embed:v1`
  - one message per session summary needing embedding
  - fields: `session_id`, `summary_hash`, `created_at`

- `nexus:derive:span_embed:v1`
  - one message per span needing embedding
  - fields: `span_id`, `trace_id`, `source_hash`, `created_at`

- `nexus:quality:v1`
  - one message per quality gate run request
  - fields: `scope`, `project_key`, `reason`, `created_at`

### Dead-Letter Streams

- `nexus:dlq:ingest:v1`
- `nexus:dlq:summarize:v1`
- `nexus:dlq:brief:v1`
- `nexus:dlq:embed:v1`
- `nexus:dlq:span_embed:v1`

DLQ messages should include the original payload, error category, error summary,
attempt count, worker id, and timestamp. They should never include secrets or
raw prompt bodies.

### State Hashes

- `nexus:source:{source_id}` for cursor and source health.
- `nexus:stage:{stage}:{unit_id}` for last-known state.
- `nexus:quality:latest` for summary score and category statuses.
- `nexus:worker:{worker_id}` for heartbeat and current assignment.

### Idempotency

Redis delivery should be treated as at-least-once. Effects must remain
idempotent:

- ingest idempotency: `file_sha256` plus event `dedup_key`
- summary idempotency: `session_id` plus source event high-watermark/hash
- embedding idempotency: `session_id` plus `summary_hash`
- span embedding idempotency: `span_id` plus `source_hash`
- brief idempotency: `project_key` plus source summary set hash

The idempotency target still lives in DuckDB/Parquet. Redis helps schedule and
observe work; it does not become the durable truth.

## Data Model Changes

Add or formalize these tables in DuckDB:

### `pipeline_units`

One row per source batch, staged file, backfill chunk, or derived job trigger.

Suggested columns:

- `unit_id`
- `unit_type`
- `source_id`
- `stage`
- `status`
- `idempotency_key`
- `input_ref`
- `output_ref`
- `watermark_start`
- `watermark_end`
- `attempts`
- `last_error_category`
- `last_error_summary`
- `created_at`
- `updated_at`

### `pipeline_transitions`

Append-only audit of stage transitions.

Suggested columns:

- `transition_id`
- `unit_id`
- `from_status`
- `to_status`
- `worker_id`
- `reason`
- `metadata_json`
- `recorded_at`

### `source_watermarks`

Promote collector cursor state into the lakehouse audit surface.

Suggested columns:

- `source_id`
- `host_id`
- `watermark_iso`
- `last_seen_file`
- `last_staged_run_id`
- `last_shipped_at`
- `last_ingested_at`
- `lag_seconds`
- `updated_at`

### `quality_observations`

Historical quality snapshots so regressions are visible over time.

Suggested columns:

- `observation_id`
- `scope`
- `status`
- `score`
- `categories_json`
- `warnings_json`
- `source_counts_json`
- `created_at`

## Quality Gates

Nexus should define hard gates, not just reports.

Minimum gates:

- ingestion freshness within threshold for required sources
- no unprocessed incoming files older than threshold
- zero duplicate canonical `dedup_key`
- no stale `running` jobs past visibility timeout
- summary coverage above target for recent closed sessions
- session embedding coverage above target
- span embedding coverage above target or explicitly waived
- bundle-ready coverage above target
- attribution coverage above target for project-scoped activity
- OpenClaw/AgentWeave span linkability above target where contract fields exist

For Paperclip-delegated work, "done" should require:

- GitHub durable artifact exists: commit, branch, or PR
- tests or explicit validation included
- Nexus quality gate captured after change
- Paperclip issue has artifact links
- GitHub issue state is updated or an exception is documented

## Refactor Roadmap

### Phase 0: Stabilize Current State

- Reconcile local Nexus checkout with `origin/main` without losing local dirty
  summarizer work.
- Fix live summarizer runtime config so retryable Ollama errors stop.
- Re-run quality snapshot and record the before/after.
- Update/close GitHub issues whose implementation already landed.

### Phase 1: Pipeline Ledger Without Redis

- Add `pipeline_units`, `pipeline_transitions`, `source_watermarks`, and
  `quality_observations`.
- Teach current filesystem/DuckDB workers to write ledger rows.
- Add quality checks for stale running jobs and missing stage transitions.
- Keep behavior unchanged; make the invisible pipeline visible first.

### Phase 2: Redis Streams Shadow Mode

- Add optional Redis config, disabled by default.
- Emit stream messages alongside current job-table enqueues.
- Add a read-only `nexus-server redis status` command.
- Compare Redis stream pending counts with DuckDB queue tables.
- Do not let Redis drive production workers yet.

### Phase 3: Redis-Driven Workers

- Move summarizer/brief/embed/span-embed workers to Redis consumer groups.
- Keep DuckDB tables as durable state and serving indexes.
- Use `XACK` only after idempotent DuckDB/Parquet writes succeed.
- Route exhausted retries to DLQ streams.
- Add `XAUTOCLAIM` recovery for abandoned pending entries.

### Phase 4: Paperclip Reliability Contract

- Add a Paperclip-Nexus completion contract:
  - branch/commit/PR required for done
  - quality gate required for done
  - GitHub issue sync required for done
  - durable artifact links required in comments
- Add a Nexus-specific Paperclip smoke test that creates a small change,
  validates it, opens/updates a PR or commit, and proves the board state matches
  GitHub state.

## Interview Learning Map

This project can cover the Redis/data-pipeline fundamentals cleanly:

- log vs database: Redis Streams vs Parquet/DuckDB
- at-least-once delivery: consumer groups and idempotent writes
- exactly-once effects: dedupe keys, output hashes, conditional commits
- backpressure: pending entry lists, stream length, worker lag
- retries: scheduled retry, max attempts, DLQ
- watermarks: source cursors, event-time vs processing-time
- replay: source archive, Parquet partitions, Redis stream replay
- schema evolution: payload versions, DuckDB table migrations, Parquet union
- observability: quality snapshots, transition ledger, freshness gauges
- separation of concerns: runtime coordination vs durable analytical storage

## Architectural Stance

Introduce Redis, but make it pay rent:

- Redis is runtime control plane, not storage of record.
- Parquet is immutable telemetry truth.
- DuckDB is local analytical and derived context serving state.
- MCP is the product API.
- Paperclip is delegation orchestration, not source-of-truth for code state.

If a future change does not improve one of reliability, replayability,
observability, or learning value, it should not be part of this refactor.
