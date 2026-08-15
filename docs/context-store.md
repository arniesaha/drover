# Context Store

Drover's context store preserves:
- **What happened** - normalized agent events and spans
- **What is running** - active sessions and host status
- **What was derived** - summaries, briefs, decisions, embeddings
- **How results were produced** - derivation history and provenance

It uses Parquet for durable telemetry facts and DuckDB for query views plus
mutable serving state.

## Design Goals

- **Local-first**: the default store lives under `~/.drover/`.
- **Replayable**: normalized views and derived work can be rebuilt from durable
  facts and ledger intent.
- **Provenance-aware**: summaries, decisions, embeddings, and curated records link
  back to their source session, span, task, or receipt.
- **Source-preserving**: adapters normalize common fields without discarding
  source-native identifiers or raw metadata.
- **Compatibility-conscious**: historical `nexus.*` telemetry remains readable
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

**Key Concepts**:
- Events are append-only and partitioned by date and agent_id
- Each event has a `dedup_key` - a canonical identifier across sources
- Tool use blocks track file edits, code execution, and other actions
- Spans capture trace-level data with model tokens, costs, and cache info

### 2. Operational State

The command plane keeps mutable state in a **separate DuckDB database**,
`~/.drover/registry.duckdb`, beside the lakehouse file:

| Table | Purpose |
| --- | --- |
| `harness_hosts` | Host identity, connection kind, capabilities, status, heartbeat |
| `harness_sessions` | Running or completed session metadata and native resume identity |
| `harness_events` | Ordered structured/terminal event envelope for a harness session |
| `live_recap_jobs` | Durable queue for incremental live session recaps |
| `live_session_recaps` | Latest recap projection per live session |

`tasks`, the logical repository/task rollup across one or more sessions, stays
in the lakehouse file with the analytical tables.

**Why separate?** A DuckDB database file is a single in-process instance. One
task scheduler, one buffer manager, and one `memory_limit` are shared by every
connection. If the command plane lived in the lakehouse file, background scans
over Parquet-backed views could saturate that instance and starve fleet endpoints.
The separate file gives it its own instance and its own budget.

**Analytical queries** that need command-plane state attach a private copy of that
(small) database rather than the live file.

The host daemon remains authoritative for live processes. Registry rows describe
and route those processes; they do not replace host-local process state.

### 3. Derived Context

Workers and explicit curation create mutable context records:

| Table | Derived value |
| --- | --- |
| `session_summaries` | Post-session summaries with narrative, outcome, next steps, questions, files touched |
| `context_containers` | Confidence-aware grouping for code, operations, research, or general activity |
| `context_links` | Links between context containers and sessions/span_ids with confidence scores |
| `active_session_briefs` | Short-lived TTL-cached handoff briefs for mid-task transfers |
| `project_briefs` | Repository-level summaries with open questions and current scope |
| `decisions` | Extracted decisions with rationale, alternatives, and selected action |
| `session_embeddings` | Vector embeddings over session summaries for semantic search |
| `span_embeddings` | Vector embeddings plus redacted span source fields |
| `curated_context_records` | User- or tool-curated Markdown context with stable hashes |
| `curated_context_provenance` | Append-only history of generated, edited, and imported records |

Derived records are replaceable projections, not new raw truth. Their generator
model, timestamps, source identifiers, hashes, or provenance events make the
derivation inspectable.

**Session Summary Format**:
```json
{
  "session_id": "abc123",
  "task_id": "task456",
  "summary_md": "Summary of what happened...",
  "next_steps_md": "Recommended next steps...",
  "open_questions": ["Question 1...", "Question 2..."],
  "files_touched": ["file1.py", "file2.py"],
  "source_version": "v1.0.0"
}
```

### 4. Pipeline Provenance

The durable pipeline ledger captures derivation history and separates intent from execution:

| Table | Purpose |
| --- | --- |
| `pipeline_receipts` | Source arrival status, applied/duplicated/quarantined/failed outcomes |
| `pipeline_jobs` | Logical work queue and current state |
| `pipeline_job_attempts` | Which worker ran each attempt and its result |
| `pipeline_artifacts` | Which durable output was produced and its current version |

**Derivation History**:
| Field | Purpose |
| --- | --- |
| `session_id` | Which session was this derived for? |
| `source_version` | Version of input data used |
| `summary_id` | ID of generated summary |
| `input_sources` | Which sources were used? |
| `model_invocation` | What model call was made (model, input, output)? |
| `reasoning` | Why was this derivation requested? |
| `metadata` | Additional context for auditing |

**Benefits**:
- **Debugging** - trace back from summary to source data
- **Rollbacks** - if derivation logic changes, see what was affected
- **Audit** - compliance with provenance requirements
- **Replay** - reconstruct state from ledger entries

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
- `dedup_key` is the canonical event identity across all sources and stores.

## Storage Layout

```text
~/.drover/
├── config.toml              # Configuration
├── api_token                # API authentication token
├── incoming/<source>/       # Incoming parquet from each source
├── parquet/                 # Durable Parquet facts
│   ├── agent_events/date=YYYY-MM-DD/agent_id=<id>/part-*.parquet
│   ├── spans/date=YYYY-MM-DD/part-*.parquet
│   ├── pr_events/part-*.parquet
│   └── routing/part-*.parquet
├── raw_objects/             # Large payloads stored by URI
├── drover.duckdb            # Lakehouse DuckDB (facts + views)
└── drover.registry.duckdb   # Command-plane DuckDB (mutable state)
```

`drover.registry.duckdb` is the command-plane store described above. Its
location can be overridden with `DROVER_CONTROL_PLANE_DUCKDB`; by default it
sits beside the lakehouse file and is named after it.

`raw_objects/` stores large payloads referenced by URI rather than copied into
every row. Keep the whole directory private; context may contain prompts,
responses, paths, diffs, and tool output.

## Provenance Graph

Everything connects via a provenance graph:

```
Context Container
    ├── Context Links (confidence, relationship type)
    ├── References Session Summaries
    │   └── References Agent Events (original turns)
    │       └── References Tool Use Blocks
    │           └── References Actual File Edits
    ├── Active Session Briefs (handoff)
    └── Project Briefs (repo-level)
```

**Link Provenance**: Each link tracks:
- `session_id` - source session
- `source_version` - version/token of the source
- `agent_id` - which agent created the record
- `span_id` - with trace context
- `dedup_key` - for event identity
- `confidence` - confidence score for fuzzy matches

## Query Surface

Applications use the harness API for live control. Agents use `drover_*` MCP
tools for fleet state, replay, search, recall, summaries, briefs, files touched,
handoff, and quality checks. Operators can use `drover-server status`,
`drover-server doctor`, and the observability endpoints for local diagnostics.

### MCP Tools Overview

| Tool | Purpose | Query Pattern |
| --- | --- | --- |
| `drover_handoff` | Recent summaries + active sessions for a task/repo | `session_summaries` ↔ `tasks` |
| `drover_session_replay` | Last N turns for a session | Direct event lookup |
| `drover_session_summary` | Summary for one session | Direct lookup |
| `drover_active_sessions` | Currently active sessions (30 min window) | Direct lookup |
| `drover_search` | Content search across events | LIKE on content column |
| `drover_files_touched` | Files edited during session | Parses tool_use_blocks |
| `drover_session_close` | Enqueue summary generation | Updates job queue |
| `drover_project_brief` | Repo-level summary | Queries `context_containers` |
| `drover_recent_sessions` | Recent summaries for a repo | `session_summaries` ordered |
| `drover_recent_contexts` | Recent context containers | Query containers |
| `drover_context_brief` | Context container details | Select container |
| `drover_open_loops` | Contexts with open actions | Filter by open_loops |
| `drover_resume_context` | Context + linked summaries | Join contexts ↔ summaries |
| `drover_recall` | Semantic search via embedding | Cosine similarity |
| `drover_task_status` | Aggregated task stats | COUNT sessions |
| `drover_project_activity` | Span-level activity by repo | SUM costs |
| `drover_active_handoff` | Live handoff brief for mid-task | TTL cache lookup |
| `drover_fleet_status` | All active sessions right now | Aggregates active |
| `drover_data_quality` | Lakehouse health check | Run quality check |
| `drover_pipeline_observatory` | Latest summaries/briefs | Query pipeline tables |

### Example Query Patterns

```sql
-- Get recent summaries for a repo
SELECT ss.session_id, ss.agent_id, ss.summary_md
FROM session_summaries ss
JOIN tasks t ON ss.task_id = t.task_id
WHERE t.repo_owner = 'arniesaha' AND t.repo_name = 'drover'
ORDER BY ss.ended_at DESC
LIMIT 5;

-- Get all files touched by a task
SELECT DISTINCT task_id,
       raw_data->>'$.tool_use_blocks[*].input.file_path' AS file_path
FROM agent_events
WHERE task_id = ? AND tool_use_blocks IS NOT NULL;

-- Semantic search by embedding
SELECT session_id, summary_embedding,
       (1.0 - cosine_similarity(summary_embedding, ?)) as similarity
FROM session_summaries
ORDER BY similarity
LIMIT 5;
```

## Quality And Observability

### Quality Checks

`drover-server quality` runs periodic health checks:

- **Fresh check** - How recently has data arrived?
- **Completeness** - Are required fields populated?
- **Consistency** - Are cross-source links valid?
- **Deduplication** - Are there duplicate events?
- **Span Coverage** - Are span and event counts matching?
- **Derivation History** - Are summary derivations tracked?

### Observability

`drover-server observatory` reports:

- **Latest summaries** - Most recent generation
- **Latest briefs** - Project briefs generated
- **Missing bundles** - Records with required fields but no source data
- **Per-project readiness** - Status by repo/project
- **Agent adoption** - How many agents have been used?

## Identity Mapping

Drover uses a stable identity scheme that maps across sources:

| Identifier | Scope | Format |
| --- | --- | --- |
| `session_id` | Session | UUID |
| `task_id` | Task (repository + branch) | Hash of owner/repo/branch |
| `trace_id` | Distributed trace | UUID |
| `span_id` | Span within trace | UUID |
| `dedup_key` | Event identity | Hash of content + timestamp |
| `context_id` | Resumable context | UUID |
| `source_version` | Summary version | String (e.g. "v1.0.0") |

**Native Provider IDs** are retained separately when they differ from Drover's
identifiers. This allows bi-directional lookup from external systems while
maintaining Drover's unified identity model.

## Security And Privacy

- **Local-first**: Default storage is local (`~/.drover/`)
- **Optional sharing**: Users can choose to share with hub
- **Data control**: Users can see, modify, or delete their data
- **Provenance tracking**: All data has full audit trail
- **Privacy**: Sensitive data can be redacted or excluded via raw_objects

## Session And Task Management

### Session Lifecycle

1. **Start** - Session begins, captured in `harness_sessions`
2. **Active** - Events stream in, tracked in `harness_events`
3. **Close** - Session ends, triggers summary generation
4. **Summarized** - Summary written to `session_summaries`

### Task Lifecycle

```text
Repository/branch → task_id → Sessions → Summaries → Context
```

Each unique owner/repo/branch combination gets a stable task_id. Multiple
sessions can belong to the same task_id as the task evolves across time.

## Future Enhancements

- **Cross-store sync** - Sync context across multiple stores (local, remote, hub)
- **Advanced provenance** - More detailed lineage tracking with sub-graphs
- **Real-time indexing** - More efficient search and query capabilities
- **Multi-model support** - Support different models and embeddings
- **Automated curation** - AI-driven curation of context and summaries
- **Query optimization** - Performance improvements for large datasets

## Troubleshooting

### Common Issues

- **Slow Queries** - Check table sizes, ensure recent data is indexed
- **Missing Summaries** - Check summary worker logs, verify session end detection
- **Provenance Gaps** - Check derivation logs, ensure sources are complete
- **Stale Data** - Check event ingestion pipeline, verify heartbeat from hosts

### Debug Commands

```bash
# Check lakehouse health
drover-server quality --deep

# Inspect the latest pipeline state
drover-server observatory

# Audit session consistency
drover-server audit-sessions
```

### Quality Check Outputs

```
Fleet Status: ✅ Operational
Event Count: 1234
Span Count: 5678
Recent Event: 2024-01-15T10:30:00Z (5 minutes ago)
Quality Score: 95.2/100
Status: HEALTHY
```

---

The Python schema definitions in `src/drover/schema.py` and
`src/drover/server/harness/schema.py` are authoritative when this overview and
the implementation differ.
