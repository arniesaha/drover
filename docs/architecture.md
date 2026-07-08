# Nexus Architecture

Nexus is a local-first context store and handoff layer for personal and open-source agent harnesses. Sessions and traces from Claude Code/Codex-style CLIs, OpenClaw, Hermes, Max, AgentWeave, and Mux can land in local DuckDB + Parquet storage so another agent can replay, search, and hand off mid-task without re-asking the user what's going on.

The default deployment is a single-user local install under `~/.nexus/` on one workstation. The dogfood deployment currently runs on the Mac Mini and accepts shippers from a NAS and work MacBook, but there is no cloud component. The GCP-era backend (BigQuery, Cloud SQL, Cloud Functions) was retired on 2026-05-09; its design and code are preserved under [`../legacy/`](../legacy/) for reference.

The 2026 pipeline direction keeps that local-first boundary but makes derived work explicit: ingest lands durable facts, workers produce summaries/briefs/embeddings, Redis Streams coordinates retryable work, and the Pipeline Observatory exposes health plus the actual summaries being saved. The current work plan is tracked in [`nexus-pipeline-roadmap.md`](nexus-pipeline-roadmap.md).

The proposed Meta Harness extension adds a phone-friendly control surface for
local CLI harnesses without changing the durable context-store boundary. Nexus
owns host/session metadata, auth, summaries, and handoff memory. Per-host
`nexus-harnessd` daemons own the data plane: PTY/tmux processes, terminal
streaming, and local command execution. See
[`meta-harness-mvp.md`](meta-harness-mvp.md).

## Shape

```
                              Workstation  (local DuckDB + Parquet store)
                              ─────────────────────────────────────────────────────────
Sources           ship         ┌──────────────────────────────────────────────────────┐
─────────         ─────        │  ~/.nexus/incoming/<host>/      ◄── rsync / launchd  │
NAS Claude  ──┐                │                                                       │
NAS OpenClaw  │                │  watcher  ─►  ingest  ─►  Parquet (Hive partitioned) │
Mac Mini      │  per-host      │                          ~/.nexus/parquet/            │
  Claude/Max  │  enrich-and-   │                                                       │
  Hermes      │─►ship  ───────►│  OTLP gRPC :4317  ────►   spans Parquet               │
Work MacBook  │                │                                                       │
  Claude      │                │  rollup   summarizer    briefs   embeddings           │
              │                │  workers writing into:                                │
              │                │  ~/.nexus/nexus.duckdb:                               │
              │                │   ├─ tasks                                            │
AgentWeave   ─┘                │   ├─ session_summaries                                │
(k3s+Tempo)                    │   ├─ project_briefs                                   │
                               │   ├─ session_embeddings                               │
                               │   ├─ span_embeddings                                  │
                               │   ├─ pipeline ledger tables                           │
                               │   └─ {summarize,brief,embed,span_embed}_jobs          │
                               │                                                       │
                               │  Redis Streams coordination (#151)                    │
                               │   └─ consumer groups, retries, DLQ                    │
                               │                                                       │
                               │  MCP HTTP :7077  ──► 15 tools                         │
                               │  Pipeline Observatory ─► health + saved summaries     │
                               └──────────────────────────────────────────────────────┘
                                                       │
                                                       ▼
                                           Agents call MCP tools plus
                                           shared Nexus skills
```

A few invariants:

- The Parquet files are the system of record for telemetry rows. DuckDB views (`agent_events`, `spans`, `sessions`, `active_sessions`) are stateless aggregations over them — drop the DB and a clean bootstrap reconstructs them.
- DuckDB *does* own mutable serving tables: `tasks`, `session_summaries`, `project_briefs`, `session_embeddings`, `span_embeddings`, pipeline ledger tables, and compatibility `*_jobs` queues. These are the "second-order" derived data, written by workers.
- Redis Streams is the coordination layer for retryable derived work. It does not replace the local store; it gives the summarizer/brief/embed/span workers consumer groups, retry leases, backpressure, and DLQ behavior. The design is specified in [`design/redis-job-queue-retry-dlq.md`](design/redis-job-queue-retry-dlq.md); production runtime cutover is tracked in [#151](https://github.com/arniesaha/nexus/issues/151).
- Every event carries a `dedup_key` so re-ingesting a JSONL file is a no-op.
- Attribution (`repo_owner`, `repo_name`, `branch`) is derived at **collect time** on the host that has access to the `cwd` path. The Mac Mini cannot resolve NAS paths, so this has to happen before shipping.

## Storage layout

```
~/.nexus/
├── incoming/<host>/                   # raw JSONL waiting for watcher pickup
│   └── .processed/                    # moved here after successful ingest
├── parquet/
│   ├── agent_events/date=YYYY-MM-DD/agent_id=<id>/part-*.parquet
│   ├── spans/date=YYYY-MM-DD/part-*.parquet
│   ├── pr_events/part-*.parquet
│   └── routing/part-*.parquet
├── nexus.duckdb                       # tasks + summaries + briefs + embeddings + jobs
├── raw_objects/                       # large blobs referenced by span/event URIs
└── config.toml                        # paths, ports, summarizer backend
```

## Components

### `nexus-collect` (per host)

A small Click app that scans a harness's local logs (Claude Code, Hermes, OpenClaw, Pimono), parses them into `AgentEvent` rows, runs `enrich_raw_repo_attribution()` against each event's `cwd` while git is still resolvable locally, and rsyncs the resulting JSONL into the Mac Mini's `~/.nexus/incoming/<host>/`.

Runs every 5 minutes via launchd (macOS) or systemd (Linux). See [`install-shipper.md`](install-shipper.md).

### `nexus-server` (local workstation, launchd/systemd/manual)

Long-lived process that hosts:

| Thread / endpoint | Job |
|---|---|
| `IncomingWatcher` | Picks up new JSONL files in `~/.nexus/incoming/*`, calls `ingest_file()`, moves originals into `.processed/` |
| `ingest_file()` | Per-event: dedup, compute `task_id`, write to Hive-partitioned Parquet, upsert `tasks`, refresh rollup |
| `OTLPReceiver` (`:4317`) | Accepts AgentWeave OTLP spans, normalizes them, writes to `spans/` Parquet |
| `SummarizerWorker` | Drains session-summary work → `session_summaries` using Claude API (or local Ollama via WoL); DuckDB queue compatibility remains while #151 wires Redis streams into production startup |
| `BriefWorker` | Drains project-brief work → `project_briefs` (uses the brief-prompt schema, not the session schema — see #50) |
| `EmbedWorker` | Drains session-embedding work → `session_embeddings` for semantic recall |
| `SpanEmbedWorker` | Drains span-embedding work → `span_embeddings` for semantic span recall |
| `PipelineLedger` | Tracks leases/completions for derived work so operators can reconcile and replay safely |
| `build_mcp_server()` (`:7077`) | FastMCP server exposing 15 tools |

The deployed dogfood path is still DuckDB-backed at process startup. The Redis stream adapter and stream-coordinated worker implementations are merged, but the launchd/runtime configuration cutover remains open in [#151](https://github.com/arniesaha/nexus/issues/151).

### MCP surface (`:7077`)

| Tool | Backed by |
|---|---|
| `nexus_handoff(repo_owner, repo_name, branch?)` | `tasks` + `session_summaries` + `active_sessions` — spans all branches when `branch` omitted (#53) |
| `nexus_active_handoff(session_id)` | `active_session_briefs` — lazy-generates a fresh brief from the last 30 events if cache is stale (60s TTL); calls the LLM backend (#51) |
| `nexus_fleet_status()` | `active_sessions` + `tasks` + latest user-message snippet |
| `nexus_data_quality(hours?)` | `quality_snapshot()` — read-only status/score/categories/warnings self-check for agent handoff readiness |
| `nexus_project_activity(project_key?, since?)` | `spans` grouped by repo (inherits from `agent_day_repos` CTE for AgentWeave-namespace spans, #52) |
| `nexus_project_brief(repo_owner, repo_name)` | `project_briefs` |
| `nexus_recent_sessions`, `nexus_active_sessions`, `nexus_session_summary`, `nexus_session_replay`, `nexus_search`, `nexus_recall`, `nexus_files_touched`, `nexus_task_status`, `nexus_session_close` | matching DuckDB views and tables |

## The attribution cascade

Most analytics in Nexus collapse if events aren't pinned to a repo. The chain is:

1. **`cwd` arrives in `raw_data`** — the source parser must forward it. Claude Code does this natively; OpenClaw hoists the session-level `cwd` / `workspaceDir` into each event (#45, #47).
2. **`enrich_raw_repo_attribution()` runs on the host** — `cwd` exists locally so `git remote get-url origin` works. Sets `_repo_owner`, `_repo_name`, `gitBranch` in `raw_data`. Wired into `write_events_jsonl` so all sources get it for free (#44).
3. **`ingest_file()` reads those fields and writes them as first-class columns** on the Parquet row, computes `task_id = compute_task_id(env_task_id, repo_owner, repo_name, branch)`, upserts a `tasks` row.
4. **`rollup_tasks()` refreshes derived columns** on `tasks` (`session_count`, `total_cost_usd`, back-fills `repo_owner`/`repo_name`/`branch` from agent_events for tasks that pre-dated attribution) — runs at the tail of every ingest commit (#49).
5. **The summarizer writes `task_id` into `session_summaries`** by joining to agent_events for the session. A one-time backfill (`scripts/backfill_session_summary_task_ids.py`, #46) closed this for historical rows.
6. **The brief worker pulls summaries by `(repo_owner, repo_name)`** through `tasks`, generates a project brief, writes to `project_briefs`. Enqueued via `nexus-server brief` for any task with recent activity (#50).

When any step is missing, downstream tools return empty results rather than wrong ones. The most visible failure mode — and a current limitation — is that `compute_task_id` hashes the branch, so each branch of a repo is a different task_id. Cross-branch handoff query is tracked in #53.

## Sources and parsers

| Source | Parser | Notes |
|---|---|---|
| Claude Code JSONL (`~/.claude/projects/`) | `parse_claude_audit_log` | Carries `cwd`, `gitBranch`, `version`, `userType` on every turn. The parser forwards the full source dict into `raw_data` so attribution survives (#48 pins this as a regression test). |
| OpenClaw session JSONL / hook / plugin events | `parse_openclaw_sessions` today; adapter contract next | Session header carries `cwd` / `workspaceDir`; the parser hoists that onto every subsequent event (#45, #47). The stable contract is defined in [`openclaw-adapter-contract.md`](openclaw-adapter-contract.md): `harness=openclaw`, canonical session UUID, separate session key, parent/child links, attribution, redaction, and AgentWeave trace/span links (#106). |
| Hermes (Max / Jenny) session JSON | `parse_hermes_sessions` | One file per session. |
| Pi-mono SQLite task journal | `parse_task_journal` | SQLite-backed task log. |
| AgentWeave spans | `nexus-server otlp` (gRPC :4317), Tempo pull, or direct import | Provenance facts for spans, delegation, model/token/cost/cache, and routing evidence. Nexus imports and links them for durable context recall without becoming a tracing dashboard; see [`agentweave-integration.md`](agentweave-integration.md) (#108). |

## DuckDB schema

Defined in [`src/nexus/schema.py`](../src/nexus/schema.py); bootstrap is idempotent (every `CREATE` is `IF NOT EXISTS` or `OR REPLACE`).

Tables that are sources of truth:

- `tasks` — one row per logical (`repo_owner`, `repo_name`, `branch`) hash
- `session_summaries` — one row per closed session
- `project_briefs` — one row per attributed project
- `session_embeddings` — semantic vectors over summaries
- `span_embeddings` — semantic vectors over spans
- `pipeline_jobs`, `pipeline_attempts`, `pipeline_dead_letters`, `pipeline_checkpoints` — derived-work ledger and operator evidence
- `summarize_jobs` / `brief_jobs` / `embed_jobs` / `span_embed_jobs` — DuckDB compatibility queues

Views (created over the Parquet files):

- `agent_events` — `read_parquet('parquet/agent_events/**', hive_partitioning=true, union_by_name=true)`
- `spans` — same shape over `parquet/spans/**`, with a session-level repo-fallback CTE for repo_owner/repo_name/branch and a `TRY_CAST` on `start_time` / `end_time` (older files stored those as strings)
- `pr_events`, `routing` — flat parquet directories
- `sessions` — `agent_events` aggregated per session_id, joined to `session_summaries`
- `active_sessions` — sessions with events in the last 30 min and no summary yet

## Operational invariants

- **Idempotent ingest.** Re-running `ingest_file` on the same JSONL produces zero new rows. `dedup_key` is `sha256(timestamp, agent_id, session_id, event_type, content[:200])`.
- **Idempotent boot.** Every `nexus-server` start runs `bootstrap()` which creates tables and views from scratch. Drop the `.duckdb` file and the next start rebuilds everything from Parquet.
- **Idempotent rollups.** `rollup_tasks()` and `enqueue_briefs_for_active_projects()` can be run repeatedly without churn.
- **No remote dependencies.** All workers degrade gracefully if Claude API / Ollama / Tempo are unreachable; jobs sit in their queues until they can run.

## Health checks

- `nexus-server doctor` — row counts per table, per-(date, agent_id) partition counts, and processed-file drift between `~/.nexus/incoming/<host>/.processed/` and the local store.
- `nexus-server quality --json` — read-only data-quality snapshot with category scores for freshness, completeness, repo attribution, event identity, and derived-context/handoff readiness.
- `nexus-server quality --prometheus` — the same snapshot rendered as Prometheus text metrics for Grafana scraping or node-exporter textfile export. Primary families: `nexus_quality_score`, `nexus_quality_status`, `nexus_quality_table_rows`, `nexus_quality_repo_attribution_percent`, `nexus_quality_identity_duplicate_dedup_key_values`, and `nexus_quality_handoff_ready`.
- `nexus-server status` — config dump + table sizes.
- launchd / systemd logs — `~/Library/Logs/com.nexus.server.{out,err}` on macOS.

## Pipeline Observatory

The Pipeline Observatory ([#176](https://github.com/arniesaha/nexus/issues/176)) shows:

- Ingestion freshness and incoming backlog by source host.
- Derived-worker queue depth, processing rate, retry count, and DLQ count.
- Latest saved session summaries and project briefs, with source session links.
- Embedding coverage for sessions and spans.
- Data-quality score, warnings, handoff readiness, and agent adoption warnings.

`nexus-server observatory` and the MCP tool `nexus_pipeline_observatory` provide
the richer read-only drilldown behind the metrics: latest saved
`session_summaries`, latest `project_briefs`, missing bundle fields, per-project
readiness, and the current agent-adoption matrix.

The same drilldown is available as a lightweight built-in web UI on the metrics
HTTP server at `/` or `/ui`. In Arnab's homelab this is intended to sit behind
`https://nexus.arnabsaha.com`, with `/observability` and `/metrics` remaining
available for JSON and Prometheus consumers.

The goal is not a generic tracing dashboard. It is the operator and interview-prep view for Nexus itself: can the pipeline keep up, what did it produce, and can an agent trust the derived context?

## Agent adoption

Nexus is useful only when agents actually call it. The rollout contract is tracked in [`agent-adoption.md`](agent-adoption.md) and [#179](https://github.com/arniesaha/nexus/issues/179):

- Every agent should have the Nexus MCP endpoint configured.
- Every agent should know the smoke checks: `nexus_data_quality`, `nexus_handoff`, `nexus_project_brief`, `nexus_recent_sessions`, and `nexus_session_replay`.
- Agents that operate Nexus should also know `nexus_pipeline_observatory` for saved artifact and project-readiness drilldown.
- Agent-local skills should point to the same Nexus usage pattern instead of re-explaining it per harness.
- Adoption is verified by each agent retrieving a handoff, replaying a session, and checking data quality before claiming continuity.

## Meta Harness

The Meta Harness proposal makes Nexus a mobile cockpit for Arnab's personal CLI
agent fleet while preserving Nexus as the context plane:

- Nexus server: host/session registry, auth, Redis-backed control events,
  transcript policy, summaries, handoffs, and observability.
- `nexus-harnessd`: per-host data plane for PTY/tmux sessions on NAS, Mac Mini,
  and GPU PC.
- Web UI/PWA: `/ui/harness` and `/ui/harness/sessions/:id` for phone-friendly
  start/attach/input/detach flows.

Redis Streams coordinate launch commands, host heartbeats, leases, retries, and
DLQ/replay. WebSockets carry live terminal bytes. DuckDB stores durable metadata
and redacted transcript chunks.

## What's not here

- **Session-link reconciliation between AgentWeave logical sessions and Claude JSONL UUIDs.** The `agent_day_repos` fallback handles repo attribution but doesn't stitch the two systems' `session_id` namespaces together. Tracked in [#13](../../issues/13).
- **Canonical OpenClaw session UUID vs session key linking.** The adapter contract is documented, but current runtime/bridge output may still need AgentWeave [#187](https://github.com/arniesaha/agentweave/issues/187) before exact native-event to span joins are available.
- **Central skill registry and self-learning loop.** Nexus can surface evidence, but it does not yet recommend skills or maintain a shared registry across agents. Tracked in [#178](https://github.com/arniesaha/nexus/issues/178).
- **Open-source release packaging.** The local dogfood store contains private history. Sanitization, demo data, history reset, and publishing approval are tracked in [#177](https://github.com/arniesaha/nexus/issues/177).
- **AgentWeave span text embedding.** Spans land but aren't semantically searchable. [#16](../../issues/16).
- **Decision extraction.** No `decisions` table yet. [#17](../../issues/17).
- **Span-tree CLI / streaming-recent commands.** [#14](../../issues/14), [#15](../../issues/15).
- **Cloud anything.** Retired with the GCP teardown.
