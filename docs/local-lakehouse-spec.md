# Nexus Local DuckDB + Parquet Storage — Specification & Migration Record

**Status:** Draft — for agent review  
**Date:** 2026-05-08  
**Author:** Jenny (agent)  
**Replaces:** `docs/migration-proposal.md` (superseded — GCP exit complete)

---

## 1. Context: What Nexus Was

Nexus Context Engine started as a GCP-hosted personal data lakehouse for Arnab's AI agent fleet. The original architecture (Stages 1–5) was:

```
Agents (NAS, Mac Mini, Work MacBook)
  └── rsync shippers (systemd/launchd)
        └── GCS raw bucket: gs://nexus-raw-logs-26/
              └── Pub/Sub → Cloud Function nexus-etl
                    ├── BigQuery: lakehouse.agent_events  (session events)
                    └── BigQuery: lakehouse.spans         (AgentWeave OTel traces)
                          └── Cloud SQL / pgvector (serving index — never actively used)
```

**GCP resources (all deleted 2026-05-08):**
- `nexus-context-engine-26` GCP project resources:
  - Cloud SQL instance `nexus-db` (Postgres 15, pgvector, 1 vCPU / 3.8 GiB, us-central1-f)
  - BigQuery dataset `lakehouse` (tables: `agent_events`, `spans`, + 205 orphaned `_spans_staging_*` temp tables)
  - GCS bucket `nexus-raw-logs-26`

**Why we exited GCP:**
- ~$43/month with no production users
- Cloud SQL was the dominant cost driver (~$15/month) and was never actively queried
- All value in the data, not the managed services
- Mac Mini has 24/7 excess capacity; DuckDB + Parquet is a better fit at this scale

---

## 2. What Was Built on GCP (Historical Record)

### Stage 1 — Data Modeling ✅
- `AgentEvent` Pydantic schema with optional `tool_calls`, `tool_result`, `token_usage`
- Parsers for Claude Code JSONL, pi-mono SQLite task journals, Hermes session files
- Cloud SQL schema: `agent_events` with `vector(768)` embedding column

### Stage 2 — GCP Infrastructure ✅
- Cloud SQL (pgvector) with private IP, Terraform-managed VPC, Serverless VPC connector
- Vertex AI `text-embedding-004` integration (provisioned, not actively queried)
- `ingest_to_cloud.py` orchestrator

### Stage 2.5 — Distributed Ingestion ✅
- NAS (`ARNABSNAS`): systemd user timer shipping Claude Code + Nix/OpenClaw every 15 min — live 2026-04-24
- Work MacBook: launchd agent shipping Claude Code every 15 min — live 2026-04-26
- Mac Mini: shipper script existed but was not scheduled

### Stage 3 — Cloud ETL ✅
- GCS-triggered Cloud Function `nexus-etl` replacing local batch script
- Dual-write: raw events → BigQuery Lakehouse; embeddings → Cloud SQL serving index

### Stage 4 — BigQuery Lakehouse ✅
- BQ-native tables (`agent_events`, `spans`) under dataset `lakehouse`
- Iceberg/BigLake migration deferred (was stage 4b, now irrelevant post-GCP-exit)

### Stage 5 — AgentWeave Integration ✅ (partially)
- Pull pipeline: Tempo HTTP API on NAS k3s → local JSON → GCS → BQ `lakehouse.spans`
- `nexus-agentweave.timer` systemd timer on NAS (now stopped)
- `nexus` CLI with `search`, `replay`, `trace search`, `trace get` commands
- `agent_id` alias reconciliation between AgentWeave (`<tool>-<host>`) and shippers (`<host>-<tool>`)

### What Was Deferred / Never Built
- Session graph reconstruction from spans
- Decision extraction (`lakehouse.decisions`)
- pgvector semantic search (provisioned, not populated)
- Mac Mini shipper scheduling
- Native Hermes/Claude skill for mid-session Nexus queries

---

## 3. Data Backup Summary

All GCP data exported to `/Users/arnabmac/jenny/nexus/local_backup/` before GCP teardown.

### `agent_events` — 357,236 unique rows (post-dedup)

**Raw backup files (13 CSVs, 3.9 GB total):**

| File | Period | Size | Notes |
|---|---|---|---|
| `agent_events_2026-01.csv` | Jan 15–31 | 7.6 MB | BQ progress noise in header (lines 1–6 skip) |
| `agent_events_2026-02.csv` | Feb | 22.7 MB | BQ noise in header (lines 1–4 skip) |
| `agent_events_2026-03.csv` | Mar | 74.5 MB | BQ noise in header (lines 1–4 skip) |
| `agent_events_2026-04.csv` | — | 0 bytes | Corrupted export; ignore |
| `agent_events_2026-04a.csv` | Apr 1–14 | 273 MB | Clean |
| `agent_events_2026-04b.csv` | Apr 14–22 | 295 MB | Clean |
| `agent_events_2026-04c.csv` | Apr 22–26 | 907 MB | Clean (largest file) |
| `agent_events_2026-04d.csv` | Apr 26–30 | 705 MB | Clean |
| `agent_events_2026-05-03.csv` | May 3 | 198 MB | Daily split |
| `agent_events_2026-05-04.csv` | May 4 | 744 MB | Daily split (busiest day) |
| `agent_events_2026-05-05.csv` | May 5 | 170 MB | Daily split |
| `agent_events_2026-05-06.csv` | May 6 | 425 MB | Daily split |
| `agent_events_2026-05a.csv` | May 1–2 | 144 MB | Clean |
| `agent_events_2026-05c.csv` | May 7 | 31.5 MB | Clean |
| `agent_events_2026-05.csv` / `05b.csv` | — | 0 bytes | Empty; skip |

**Raw schema (8 columns):**
```
id, session_id, timestamp, agent_id, event_type, role, content, raw_data
```

**`raw_data` is a polymorphic JSON blob** — schema varies by `event_type`. See Section 4 for the full breakdown.

**Event identity semantics:** `agent_events.id` is source/trace identity, not the canonical logical-event key. Historical BigQuery exports were originally deduplicated by `id`, but live Parquet can still contain repeated `id` values from timestamp-normalization variants (for example, the same event represented once with a timezone offset and once as normalized UTC). Runtime and downstream queries that need one logical row per event must deduplicate by `dedup_key`, which is computed from stable business fields (`timestamp`, `agent_id`, `session_id`, `event_type`, `content[:200]`) by `nexus.dedup.make_dedup_key`. `runtime-audit` reports both duplicate `id` metrics (data-quality signal) and duplicate `dedup_key` metrics (canonical dedupe health). Keep the raw `agent_events` view as the audit surface; read paths that require logical-event uniqueness should use the canonical CTE from `nexus.event_identity.canonical_agent_events_cte()`.

**Agent coverage:**

| Agent | Events (raw) | % |
|---|---|---|
| `work-macbook-claude` | ~808,695 | 68% |
| `nas-claude` | ~288,851 | 24% |
| `nas-openclaw` | ~66,271 | 6% |
| `macmini-hermes` | ~14,650 | 1.2% |
| `macmini-pimono` | ~2,852 | 0.2% |
| `macmini-claude` | ~1,717 | 0.1% |

### `spans` — 11,125 unique rows

**Raw file:** `spans.csv` (22.5 MB)  
**Date range:** 2026-04-14 → 2026-05-07  
**Source:** AgentWeave traces pulled from Tempo (k3s NAS)

**Raw schema (31 columns):**
```
trace_id, span_id, parent_span_id, name, service_name, start_time, end_time, duration_ms,
activity_type, agent_id, agent_type, session_id, parent_session_id, project, task_label,
llm_provider, llm_model, prompt_tokens, completion_tokens, total_tokens, cache_read_tokens,
cache_write_tokens, cost_usd, prompt_preview, response_preview, attributes_json,
raw_object_uri, ingested_at, agent_model, associated_with, stop_reason
```

**`attributes_json`** is a double-encoded JSON blob of raw OTel attributes. Contains Mux routing decisions under `prov.route.*` keys.

---

## 4. Schema Analysis: `raw_data` Field Map

The `raw_data` column in `agent_events` is the central complexity. It is populated differently per `event_type`:

### Core turn events (`user`, `assistant`)

Every event includes:
- `cwd` — working directory at time of turn (source of git repo context)
- `gitBranch` — active git branch
- `sessionId` — Claude Code internal session UUID
- `uuid` — this event's UUID
- `parentUuid` — parent event UUID (conversation tree linkage)
- `isSidechain` — whether this is a sub-agent sidechain turn
- `version` — Claude Code version
- `entrypoint` — `cli` or `sdk-cli`
- `slug` — human-readable session name (e.g. `joyful-skipping-catmull`)
- `message` — the full message object (content array, role, model)

`user` additionally has:
- `promptId`, `agentId`, `permissionMode`, `sourceToolAssistantUUID`, `toolUseResult`, `isMeta`

`assistant` additionally has:
- `requestId`, `agentId`, `isApiErrorMessage`, `error`, `errorDetails`

### `progress` events
- `data`, `toolUseID`, `parentToolUseID` — streaming tool execution updates

### `queue-operation` events
- `operation` — `enqueue` / `dequeue` / `remove`
- `content` — queued message content

### `system` events
- `subtype`, `level`, `isMeta`, `hookCount`, `hookInfos`, `hookErrors`
- `preventedContinuation`, `stopReason` — session lifecycle signals

### `pr-link` events ⭐
- `sessionId`, `prNumber`, `prUrl`, `prRepository`, `timestamp`
- Links a Claude Code session to a GitHub PR it created
- **Massively duplicated** (one copy per turn in the session — dedup required)

### `task_telegram_msg` / `task_a2a_task` events
- `id`, `source`, `status`, `payload`, `result` — A2A task tracking

### `model_change` / `thinking_level_change` events
- `provider`, `modelId` / `thinkingLevel` — runtime config changes

### `custom` events
- `customType`, `data` — application-defined event types

---

## 5. New Local Schema Design

### Design Principles

1. **Promote, don't blob** — fields queried more than once should be first-class columns
2. **Deduplicate at load** — the BQ backup had ~70% duplication; the local schema enforces uniqueness
3. **Partition for performance** — DuckDB scans Parquet partitions; `date` + `agent_id` eliminates irrelevant files
4. **Separate concerns** — `pr_events` and `routing` are their own tables, not buried in blobs
5. **Raw blob preserved** — `raw_data` / `attributes_json` retained for any fields not yet promoted

### Tables

#### `agent_events` (primary fact table)

Promoted columns beyond the original 8:

| New Column | Source | Why |
|---|---|---|
| `date` | `timestamp[:10]` | Partition key |
| `git_repo` | parsed from `raw_data.cwd` | Repo-level filtering without JSON parse |
| `git_branch` | `raw_data.gitBranch` | Branch-level filtering |
| `cwd` | `raw_data.cwd` | Full path for debugging |
| `slug` | `raw_data.slug` | Session name (human-readable) |
| `entrypoint` | `raw_data.entrypoint` | cli vs sdk-cli |
| `prompt_id` | `raw_data.promptId` | Dedup and lineage |
| `request_id` | `raw_data.requestId` | API call tracing |
| `sub_agent_id` | `raw_data.agentId` | Sub-agent identification |
| `permission_mode` | `raw_data.permissionMode` | Safety/audit |
| `stop_reason` | `raw_data.message.stop_reason` | Session outcome |
| `is_api_error` | `raw_data.isApiErrorMessage` | Error filtering |
| `is_sidechain` | `raw_data.isSidechain` | Multi-agent topology |
| `parent_uuid` | `raw_data.parentUuid` | Conversation tree |
| `message_uuid` | `raw_data.uuid` | Event identity |

**Partitioning:** `date=YYYY-MM-DD / agent_id=<id>`

#### `spans` (OTel traces)

Promoted from `attributes_json`:

| New Column | Source | Why |
|---|---|---|
| `route_requested_model` | `prov.route.requested_model` | Mux analytics |
| `route_resolved_model` | `prov.route.resolved_model` | Mux analytics |
| `route_reason` | `prov.route.reason` | Routing heuristic |
| `route_runtime` | `prov.route.runtime` | Agent runtime |
| `route_message_count` | `prov.route.message_count` | Context depth at routing |
| `route_provider_id` | `prov.route.provider_id` | Provider analytics |
| `cache_hit_rate` | `cache.hit_rate` | Cache efficiency |
| `hook_source` | `prov.hook.source` | Hook attribution |

**Partitioning:** `date=YYYY-MM-DD`

#### `pr_events` (new, derived)

Extracted from `pr-link` events in `agent_events`, deduplicated to one row per `(session_id, pr_number, pr_repository)`.

| Column | Type | Notes |
|---|---|---|
| `session_id` | string | Links back to agent_events |
| `pr_number` | string | GitHub PR number |
| `pr_url` | string | Full GitHub URL |
| `pr_repository` | string | `owner/repo` format |
| `timestamp` | timestamp | When the PR was created |
| `agent_id` | string | Which agent created it |

**Observed PRs in backup:**
- `arniesaha/agentweave` PR #182
- `atlanhq/mothership` PRs #558, #657
- `atlanhq/sentinel` PRs #16, #17

#### `routing` (new, derived)

Extracted from spans where `prov.route.requested_model` is present. These are Mux routing decisions — what was asked for vs. what was resolved.

| Column | Type | Notes |
|---|---|---|
| `trace_id`, `span_id` | string | OTel identity |
| `start_time`, `date` | timestamp/date | Partitioning |
| `agent_id`, `session_id`, `project` | string | Attribution |
| `requested_model` | string | What was requested |
| `resolved_model` | string | What Mux routed to |
| `reason` | string | Routing heuristic used |
| `runtime` | string | Agent runtime |
| `message_count` | int | Context depth |
| `provider_id` | string | Provider |
| `duration_ms`, `cost_usd` | float | Cost of this call |

**1,990 routing decisions** in the backup. Notable: `MiniMax-M2.7-highspeed` received 2,780 calls — Mux is actively downrouting from Anthropic.

#### `sessions` (derived view)

Not a stored table — a DuckDB view that aggregates `agent_events` + `spans` + `pr_events` into one row per session:

| Column | Notes |
|---|---|
| `session_id`, `agent_id` | Identity |
| `slug` | Human-readable name |
| `git_repo`, `git_branch` | Repository context |
| `entrypoint` | cli / sdk-cli |
| `started_at`, `ended_at`, `duration_seconds` | Timing |
| `total_events`, `user_turns`, `assistant_turns`, `api_errors` | Volume |
| `total_cost_usd`, `total_tokens`, `total_prompt_tokens`, `total_completion_tokens` | Cost (from spans) |
| `pr_numbers[]`, `pr_repos[]` | PRs created (from pr_events) |

---

## 6. Migration Implementation

### Script: `migrate_to_duckdb.py`

Location: `/Users/arnabmac/jenny/nexus/migrate_to_duckdb.py`

**Phases:**

**Phase 1 — `agent_events` → Parquet**
- Reads all 13 agent_events CSVs
- Handles BQ-noise header lines in Jan/Feb/Mar files (scans to `id,session_id,...` header)
- Deduplicates by `id` (eliminated 827,393 duplicates)
- Extracts `pr-link` events into deduplicated `pr_events` table
- Writes partitioned Parquet: `parquet/agent_events/date=YYYY-MM-DD/agent_id=<id>/part-XXXX.parquet`

**Phase 2 — `spans` → Parquet**
- Reads `spans.csv`
- Parses double-encoded `attributes_json` (strips surrounding quotes, unescapes)
- Promotes `prov.route.*` attributes to first-class columns
- Extracts Mux routing decisions into `routing` table
- Writes partitioned Parquet: `parquet/spans/date=YYYY-MM-DD/part-XXXX.parquet`

**Phase 3 — DuckDB views**
- Creates `nexus.duckdb` at `/Users/arnabmac/jenny/nexus/nexus.duckdb`
- 5 views: `agent_events`, `spans`, `pr_events`, `routing`, `sessions`
- All views use `read_parquet(..., hive_partitioning=true)` for partition pruning

**Dry-run support:** `--dry-run` flag parses without writing. `--phase 1|2|3` for incremental runs.

### Results

| Table | Rows | Parquet Size |
|---|---|---|
| `agent_events` | 357,236 | ~130 MB |
| `spans` | 11,125 | ~5 MB |
| `pr_events` | 64 | <1 MB |
| `routing` | 1,990 | <1 MB |
| **Total** | **371,476** | **148.6 MB** |

- Input: 3.9 GB CSV → Output: 148.6 MB Parquet (**96% compression** via zstd)
- Runtime: 26 seconds on Mac Mini

---

## 7. Key Findings from Data Analysis

### Cost
- `claude-opus-4-7`: **$1,057.21** (tracked over ~23 days of spans)
- `gpt-5.4`: $148.51
- `claude-sonnet-4-6`: $108.06
- `gpt-5.3-codex`: $20.44
- `MiniMax-M2.7-highspeed`: $16.38
- **Total tracked: ~$1,350+**

### Top repositories by agent assistant turns
1. `observer-sessions` — 15,410 (claude-mem observer — meta!)
2. `atlas-metastore` — 3,418
3. `agentweave` — 3,377
4. `sherlock` — 2,819
5. `sentinel` — 2,063

### Mux routing
- 1,990 routing decisions captured
- Mux is actively downrouting: e.g. `claude-sonnet-4-6` → `claude-haiku-4-5-20251001` for simple tasks
- MiniMax-M2.7-highspeed received 2,780 routed calls — a significant portion of traffic

### Data quality issues (inherited, not yet fixed)
- `unknown` event type: 103,688 events from `nas-claude` (parser doesn't recognize format)
- No source file provenance in `agent_events` (can't trace event back to raw JSONL)
- `role` not normalized (`human` / `user` / `assistant` mixed across agents)
- `pr-link` events were duplicated ~30x per session in BQ (fixed in migration by dedup)

---

## 8. What's New / Changed vs. AgentWeave Updates

You mentioned AgentWeave and Mux Traces have been updated. The key new fields available from the current `attributes_json` that are **not yet promoted** to first-class columns and represent the highest-value opportunities:

### Already captured in spans but worth highlighting:
- `prov.route.*` — fully extracted into `routing` table ✅
- `hook_data` — Claude Code hook payload with `transcript_path`, `session_id` (partially captured via `hook_source`)

### Opportunities with new AgentWeave data:
1. **`prov.llm.prompt_preview` / `response_preview`** — currently in `spans` but not indexed. Could power a "what was the agent thinking?" query.
2. **`gen_ai.response.finish_reasons`** — stop reason array; more granular than current `stop_reason`.
3. **Repository details** — `cwd` gives repo name but no `git_remote`, `git_commit`, or `pr_number`. If AgentWeave now emits these, they should be first-class span attributes.
4. **Tool call spans** — `activity_type = tool_call` is referenced in the spec but absent from the backup data. If AgentWeave now emits tool-level spans, these are high-value for debugging agent behavior.
5. **Session graph** — `parent_span_id` is sparsely populated. If the new version emits complete parent/child linkage, we can reconstruct full trace trees, not just flat span lists.

---

## 9. Next Steps

### Immediate (local stack)
- [ ] **Ingest pipeline**: Replace GCS+Cloud Function with a local watcher that ingests directly from Claude Code JSONL / Tempo → Parquet
- [ ] **Nexus CLI update**: Port `nexus search`, `nexus trace search/get` from BigQuery to DuckDB queries
- [ ] **Fix `unknown` event type**: Update parser to handle `nas-claude` format (103K events currently lost)
- [ ] **Role normalization**: Map `human` → `user` at ingest time

### Medium term
- [ ] **Repository enrichment**: If AgentWeave emits `git_remote` / `git_commit`, add to spans schema
- [ ] **Tool call spans**: Add `activity_type = tool_call` support to parser and schema
- [ ] **Session graph view**: Use `parent_span_id` linkage to build trace tree queries in DuckDB
- [ ] **Mux routing dashboard**: Build a simple query set showing routing patterns, cost savings, model distribution

### Deferred
- pgvector / semantic search (re-evaluate when DuckDB VSS extension matures)
- Decision extraction (`decisions` table)
- MCP/tool interface for mid-session Nexus queries

---

## 10. File Layout

```
nexus/
├── README.md                          # Project overview (needs update post-GCP-exit)
├── migrate_to_duckdb.py               # Migration script (phases 1–3)
├── nexus.duckdb                       # DuckDB database with views
├── example_queries.sql                # Sample DuckDB queries
├── local_backup/                      # Raw CSV exports from BigQuery
│   ├── agent_events_2026-*.csv        # 13 files, 3.9 GB
│   └── spans.csv                      # 22.5 MB
├── parquet/                           # Normalized Parquet lake (148.6 MB)
│   ├── agent_events/date=*/agent_id=*/part-*.parquet
│   ├── spans/date=*/part-*.parquet
│   ├── pr_events/part-0000.parquet
│   └── routing/part-0000.parquet
├── docs/
│   ├── local-lakehouse-spec.md        # This document
│   ├── architecture.md                # Original architecture notes
│   ├── migration-proposal.md          # GCP exit proposal (superseded)
│   ├── agentweave-integration.md      # AgentWeave pull model (GCP-era)
│   └── superpowers/
│       ├── specs/2026-04-25-agentweave-tempo-spans-design.md
│       └── plans/2026-04-25-agentweave-tempo-spans.md
├── src/nexus/                         # Python package
│   ├── cli.py                         # nexus CLI (needs DuckDB port)
│   ├── parsers.py                     # Event parsers
│   ├── models.py                      # Pydantic schemas
│   └── cloud_function/                # GCP ETL (now defunct)
├── scripts/                           # Shipper scripts (now stopped)
│   ├── sync_*.sh                      # Per-machine rsync shippers
│   ├── export_agentweave_tempo.py     # Tempo puller (needs local port)
│   ├── launchd/                       # Mac Mini launchd plists
│   └── systemd/                       # NAS systemd units
└── iac/                               # Terraform (GCP resources now deleted)
```

---

## Review Notes for Agent

This spec is intended for review by another agent. Key questions:

1. **Schema completeness**: Are there `raw_data` fields from `user`/`assistant` events that should be promoted but aren't? Specifically around `message.content` array structure — tool calls, images, and thinking blocks are all inside `message` but not parsed.

2. **Partition strategy**: `date + agent_id` is good for "what did agent X do on day Y" queries. But `git_repo + date` might be more useful for developer workflow queries ("what happened in sentinel this week"). Should we dual-partition or add a separate `git_repo` index?

3. **`sessions` view performance**: The current `sessions` view does a GROUP BY over all `agent_events` on every query. For ~357K rows this is fast, but it should probably be materialized as a Parquet table once the ingest pipeline is live and adding rows daily.

4. **AgentWeave schema evolution**: The spec notes that `activity_type = tool_call` was planned but absent from backup data. If the updated AgentWeave emits tool spans, the `spans` schema needs a `tool_name` and `tool_input_preview` column.

5. **Mux Traces**: The routing table captures `prov.route.*` well. But `prov.route.message_count` (context depth at routing time) might be the most interesting signal for cost optimization — high message_count + complex routing = session that should have been summarized earlier. Worth highlighting in any routing analytics.
