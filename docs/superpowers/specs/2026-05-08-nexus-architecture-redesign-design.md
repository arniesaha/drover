# Nexus Architecture Redesign — Design Spec

**Status:** Draft for implementation planning
**Date:** 2026-05-08
**Replaces (in spirit):** `docs/local-lakehouse-spec.md` (kept as historical migration record)
**Brainstormed via:** `superpowers:brainstorming`

---

## 1. Context

Nexus is the personal context engine for an AI agent fleet running across three machines:

- **Work MacBook** — Claude Code
- **Mac Mini** — Jenny (Hermes), Max (pi-mono), Claude Code (this session)
- **NAS (`ARNABSNAS`)** — Nix (OpenClaw), Claude Code, AgentWeave (k3s)

Through 2026-Q1 the system was a GCP lakehouse (BigQuery + Cloud SQL/pgvector + Cloud Functions ETL + GCS raw archive). PR #39 documented the GCP exit and a one-shot CSV→Parquet→DuckDB migration on the Mac Mini. That migration successfully recovered 357K unique events and 11K spans into 148 MB of Parquet, but it did not design the steady-state system: the shippers were stopped, no live ingest replaced them, and the agent-facing query surface still points at BigQuery.

This spec defines that steady-state system. The driver is three concrete user goals:

1. **Seamless agent handoff.** When agent B picks up work agent A was doing on the same task, B should not need to be re-briefed from scratch.
2. **Seamless concurrent collaboration.** When two agents are active on the same task, each should be aware of the other.
3. **AgentWeave as a first-class data source.** The user's observability platform (https://github.com/arniesaha/agentweave, deployed on NAS k3s) emits OTel spans with PROV-O attributes; those spans should feed the lakehouse natively rather than via a 15-min Tempo-pull batch.

The system should also leave the door open to open-source it later (MIT/Apache-2.0, no GCP-only assumptions, a stable contract for third-party harnesses).

---

## 2. Decisions made during brainstorming

These are fixed inputs to the design — re-litigate them only if the implementation surfaces a hard problem.

| Topic | Decision |
|---|---|
| Handoff UX | **Hybrid** — auto-injected summary on session start + on-demand drilldown via MCP tools. |
| Network topology | **Central lakehouse on Mac Mini.** All three hosts reach it directly (LAN/Tailscale, always on). |
| Task identity | **`(repo_owner, repo_name, branch)` by default; `$NEXUS_TASK_ID` env var overrides** for cross-repo or non-git work. |
| Ingest strategy | **Dual-source, permanent.** AgentWeave OTLP for LLM-call telemetry/cost/routing; file shippers for full-fidelity transcripts (tool calls, tool results). |
| Coordination model | **Awareness only.** `active_sessions` view + MCP tool. No locks, no leases. |
| Architecture style | **Approach A — "Thin core."** Re-use existing parsers, shippers, AgentWeave; add one new daemon (`nexus-server`) on Mac Mini and a per-host collector + per-agent hook. |

---

## 3. Service topology

```
┌──────────────────── Mac Mini (hub) ────────────────────┐
│                                                          │
│  nexus-server  (Python daemon)                          │
│  ├── OTLP receiver        :4317 (gRPC)                  │  ← AgentWeave proxy pushes spans
│  ├── MCP server           stdio + http :7077            │  ← Agents call for handoff/search/active
│  ├── File watcher         (~/nexus/incoming/)            │  ← Per-host collectors drop here
│  ├── Summarizer worker    (claude-haiku-4-5-20251001)   │  ← Generates session summaries on close
│  └── Maintenance loop     (compact, vacuum, doctor)      │
│                                                          │
│  DuckDB (~/nexus/nexus.duckdb) ── parquet/ (Hive)        │
│  ├── agent_events/  spans/  pr_events/  routing/         │  (existing, schema-extended)
│  ├── sessions  (view)                                    │
│  ├── tasks                                               │  ← NEW
│  ├── session_summaries                                   │  ← NEW
│  └── active_sessions  (view)                             │  ← NEW
└──────────────────────────────────────────────────────────┘
       ▲                   ▲                  ▲
       │ rsync/SSH         │ OTLP gRPC        │ MCP (stdio over SSH or HTTP :7077)
       │                   │                  │
┌──────┴────────┐  ┌───────┴────────┐  ┌──────┴──────────────┐
│  NAS / Mac    │  │  AgentWeave    │  │  Each agent runtime │
│  Mini / Work  │  │  proxy on NAS  │  │  (any host, any     │
│  MacBook      │  │  k3s           │  │   harness) calls    │
│  → nexus-     │  └────────────────┘  │   nexus-hook on     │
│    collect    │                      │   session start/end │
└───────────────┘                      └─────────────────────┘
```

### 3.1 Components

Three independently testable units, each with one purpose.

#### `nexus-server`
Owns the lakehouse. Exposes one read API (MCP) and two write paths (OTLP, file watcher). Knows nothing about specific harnesses.

- **OTLP receiver**: gRPC on `:4317`. Decodes spans, applies the existing `parse_agentweave_trace` logic (port from `src/nexus/cloud_function/main.py`), upserts into `spans` Parquet via dedup_key MERGE.
- **File watcher**: monitors `~/nexus/incoming/<host>/`. On atomic rename `*.tmp → *.jsonl`, parses (using `src/nexus/parsers.py`), MERGEs into `agent_events` Parquet.
- **MCP server**: stdio (for agents on Mac Mini) and HTTP `:7077` (for agents on NAS / Work MacBook). Tool surface in §6.
- **Summarizer worker**: reads `summarize_jobs` queue (a DuckDB table), calls Anthropic API with claude-haiku-4-5-20251001, writes to `session_summaries`.
- **Maintenance loop**: nightly cron in-process — compact small Parquet files, run `nexus-server doctor` (row-count audit), prune `summarize_jobs` older than 7 days.

#### `nexus-collect`
Per-host shipper. Replaces the existing `sync_*.sh` scripts. Stateless except for a per-source cursor file at `~/nexus/state/<source>.cursor`.

Sources (one collector instance per host, all sources within run sequentially):
- Claude Code JSONL (`~/.claude/projects/`)
- Claude Code (Mac Mini variant) (`~/Library/Application Support/Claude/local-agent-mode-sessions/`)
- Hermes JSON (`~/.hermes/profiles/jenny/sessions/`)
- pi-mono SQLite (`~/max/data/task-journal.db`)
- OpenClaw JSONL (`~/.openclaw/agents/main/sessions/`)

Output: `~/nexus/staging/<run-id>.jsonl` (canonical AgentEvent format), then atomic `mv` to `~/nexus/staging/<run-id>.jsonl.tmp` → `rsync` to `mac-mini:~/nexus/incoming/<host>/<run-id>.jsonl.tmp` → server-side rename to `.jsonl` once rsync exits 0.

Cadence: every 5 minutes (down from 15) via launchd / systemd user timer.

#### `nexus-hook`
Per-agent, per-harness lifecycle hook. One small script per harness, all calling the same MCP tools. Initially:

- **Claude Code**: `~/.claude/settings.json` `SessionStart` and `SessionEnd` hooks invoke `nexus-hook session-{start,end}`.
- **OpenClaw, Hermes, pi-mono**: each has its own session lifecycle; per-harness wrapper invokes `nexus-hook` at the same boundaries.

All hooks have a 2 s budget. On timeout or MCP error, hook prints `(nexus offline)` to stderr and exits 0 — never blocks agent startup.

### 3.2 Public contract

`AgentEvent` Pydantic schema in `src/nexus/models.py` is the **stable wire format** between collectors and the server. Any third party writing a new collector targets that schema; the server doesn't change. This is the OSS pluggability story.

---

## 4. Data model

Keeps the schema PR #39 designed (agent_events, spans, pr_events, routing, sessions). Adds three things and threads `task_id` through.

### 4.1 New table — `tasks`
The new primitive that ties multi-session, multi-agent work together.

```sql
CREATE TABLE tasks (
  task_id           VARCHAR PRIMARY KEY,  -- sha256(coalesce($NEXUS_TASK_ID, repo_owner||"/"||repo_name||"@"||branch))[:16]
  repo_owner        VARCHAR,              -- "atlanhq" / "arniesaha"
  repo_name         VARCHAR,              -- "nexus"
  branch            VARCHAR,              -- "docs/local-lakehouse-migration"; null if non-git
  explicit_task_id  VARCHAR,              -- $NEXUS_TASK_ID when set; else null
  principal_id      VARCHAR,              -- "arnab" today; multi-user later
  status            VARCHAR,              -- 'open' | 'closed' | 'merged'
  title             VARCHAR,              -- generated from first session's first user prompt
  created_at        TIMESTAMP,
  last_activity_at  TIMESTAMP,
  session_count     INTEGER,
  total_cost_usd    DOUBLE
);
```

`task_id` derivation, deterministic across hosts:
```python
def compute_task_id(env_task_id, repo_owner, repo_name, branch):
    raw = env_task_id or f"{repo_owner}/{repo_name}@{branch or 'HEAD'}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

`tasks` rows are **upserted** on every event ingest where the relevant attributes are present. `last_activity_at`, `session_count`, `total_cost_usd` are recomputed by the maintenance loop, not maintained transactionally.

### 4.2 New table — `session_summaries`
The handoff payload. One row per closed session.

```sql
CREATE TABLE session_summaries (
  session_id        VARCHAR PRIMARY KEY,
  task_id           VARCHAR,                 -- FK to tasks
  agent_id          VARCHAR,
  ended_at          TIMESTAMP,
  summary_md        VARCHAR,                 -- LLM-generated, ≤ 400 tokens
  files_touched     VARCHAR[],               -- from tool_use blocks (Edit, Write, Bash with file ops)
  tools_used        MAP(VARCHAR, INTEGER),   -- {"Edit": 8, "Bash": 12, ...}
  last_user_prompt  VARCHAR,                 -- raw, last 500 chars
  last_assistant    VARCHAR,                 -- raw, last 500 chars
  next_steps_md     VARCHAR,                 -- LLM-extracted "what would I do next"
  open_questions    VARCHAR[],               -- LLM-extracted bullets
  status            VARCHAR,                 -- 'completed' | 'interrupted' | 'errored'
  generator_model   VARCHAR,                 -- e.g. "claude-haiku-4-5-20251001"
  generated_at      TIMESTAMP
);
```

### 4.3 New view — `active_sessions`

```sql
CREATE OR REPLACE VIEW active_sessions AS
SELECT
  s.session_id,
  s.agent_id,
  s.task_id,
  t.repo_owner,
  t.repo_name,
  t.branch,
  s.started_at,
  max(e.timestamp) AS last_event_at,
  count(*) AS event_count
FROM sessions s
JOIN agent_events e USING (session_id)
LEFT JOIN tasks t USING (task_id)
WHERE NOT EXISTS (SELECT 1 FROM session_summaries ss WHERE ss.session_id = s.session_id)
  AND e.timestamp > now() - INTERVAL 30 MINUTE
GROUP BY 1,2,3,4,5,6,7;
```

A session is "active" if it has no summary row AND received an event within the last 30 minutes. The 30-min window catches session crashes that never fired SessionEnd.

### 4.4 Schema additions to existing tables

- `agent_events`: add `task_id VARCHAR`, `repo_owner VARCHAR`, `repo_name VARCHAR`, `principal_id VARCHAR`. Keep `git_repo` for backwards-compatibility with the migration script's output; mark deprecated in column comments.
- `spans`: add `task_id VARCHAR`, `principal_id VARCHAR`. Existing `session_id` remains the join key against `agent_events`.
- `sessions` view: add `task_id`, `LEFT JOIN session_summaries` to expose `summary_md`/`next_steps_md` directly when present.

### 4.5 Intentionally deferred
- **Per-message embeddings / vector index.** Defer; semantic search is downstream of getting the schema right. DuckDB's `vss` extension is a clean later addition.
- **`handoffs` table.** Implicit handoff (next session on same task by different agent) is derivable from `session_summaries` self-join. Add a column (or table) only when explicit handoff events emerge.
- **`leases` / coordination tables.** Awareness-only mode doesn't need them.
- **Multi-tenant `principal_id` beyond `'arnab'`.** Column exists for future use; no auth, no namespace separation in v1.

---

## 5. Ingest paths

### 5.1 Path A — AgentWeave (live, push)

AgentWeave proxy on NAS k3s adds an OTLP exporter pointing at `mac-mini.local:4317` **in addition to** its existing Tempo writer. `nexus-server` OTLP receiver:

1. Decodes incoming OTLP gRPC spans.
2. Applies the existing AgentWeave parser logic (ported from `src/nexus/cloud_function/main.py:parse_agentweave_trace`).
3. Computes `dedup_key = (trace_id, span_id)`.
4. Computes `task_id` from span attributes (`prov.repo.owner` / `prov.repo.name` / `prov.git.branch`, with fallback to existing `prov.project`).
5. MERGE into `spans` Parquet partition for `date=YYYY-MM-DD`.

**The Tempo→GCS→Cloud-Function pull pipeline is removed.** Tempo on NAS keeps running for AgentWeave's own dashboard UI.

If AgentWeave doesn't already emit `prov.repo.owner` / `prov.git.branch`, those need to be added to AgentWeave's instrumentation (separate PR in the agentweave repo) — fall back to `prov.project` in the meantime.

### 5.2 Path B — File shippers (batch, pull)

Each host runs `nexus-collect` as a launchd / systemd user timer. Per-source flow:

1. Read source files newer than the cursor.
2. Parse with the matching parser from `src/nexus/parsers/`.
3. Compute `dedup_key` (existing logic from `cloud_function/main.py:_make_dedup_key`).
4. Compute `task_id` per event from `cwd` → `git remote get-url origin` (cached) and the agent's current branch.
5. Write JSONL to `~/nexus/staging/<run-id>.jsonl.tmp`, fsync, atomic rename to `.jsonl`.
6. `rsync -av --remove-source-files` to `mac-mini:~/nexus/incoming/<host>/`.
7. Update cursor on rsync success.

Server file-watcher uses inotify-equivalent (Python `watchdog`); on file ready, parses → dedup_key MERGE → moves source file to `~/nexus/incoming/<host>/.processed/` (kept for 7 days for audit, then pruned).

Cadence: every 5 minutes.

### 5.3 Idempotency

Both paths share **`dedup_key` MERGE semantics** identical to what `main` shipped in commit a9f1a5f. The server treats every incoming event as `MERGE … ON dedup_key MATCHED THEN DO NOTHING`. Re-running a collector or replaying an OTLP batch is always safe.

### 5.4 Backfill / repair

`nexus-server compact` rewrites Parquet partitions to enforce dedup post-hoc and combine small files (one file per partition per day). Runs nightly via the maintenance loop.

`nexus-server doctor` audits row counts: count of `agent_events` per (date, agent_id) vs expected from `~/nexus/incoming/<host>/.processed/` manifest. Flags drift > 1%.

---

## 6. Handoff flow (use case 1)

### 6.1 Session start

1. Harness `SessionStart` hook fires `nexus-hook session-start`.
2. Hook resolves:
   - `repo_owner`, `repo_name` from `git remote get-url origin` (parsed for `:owner/repo` or `/owner/repo`)
   - `branch` from `git symbolic-ref --short HEAD`
   - `agent_id` from `~/.nexus/config.toml` (`agent_id = "macmini-claude"` etc.)
3. Hook calls MCP tool `nexus_handoff(repo_owner, repo_name, branch)`.
4. Server:
   - Computes `task_id`.
   - Returns the most recent `session_summaries` rows for this `task_id`: at most 3 summaries, accumulated until the rendered output reaches ~1500 tokens, whichever limit hits first.
   - Also returns `nexus_active_sessions(task_id)` results in the same response.
5. Hook prints to stdout in the harness's expected system-context format. For Claude Code:

```
**Resuming task `nexus@docs/local-lakehouse-migration`** (task_id: a3f9...)
Last touched 2h ago by `nas-claude` (session `xyz`).

**Summary:** Reviewed PR #39 (local lakehouse migration). Identified gaps in
dedup_key reconciliation, ingest re-runnability, sessions view drift from
iac/main.tf. Drafted architecture redesign covering handoff/awareness/MCP.

**Next steps:** Address the 5 must-fix items in §4 of the PR review;
write the new design doc; build nexus-server skeleton.

⚠️ `macmini-claude` is also active on this task (last activity 4 min ago).

Tools available: /nexus-replay <session_id>, /nexus-active, /nexus-search.
```

### 6.2 Session end

1. Harness `SessionEnd` hook fires `nexus-hook session-end --session-id <id>`.
2. Hook calls `nexus_session_close(session_id)`.
3. Server:
   - Inserts a row into `summarize_jobs(session_id, status='pending')`.
   - Returns immediately. Hook does not wait.
4. Summarizer worker (separate thread/process in `nexus-server`):
   - Polls `summarize_jobs`.
   - For each pending job, reads last 30 turns from `agent_events` for that session.
   - Calls Anthropic API with claude-haiku-4-5-20251001 using a prompt template stored at `src/nexus/prompts/session_summary.md` (versioned in git).
   - Parses response into `summary_md`, `next_steps_md`, `open_questions`.
   - Computes `files_touched` and `tools_used` deterministically from tool_use blocks (no LLM needed).
   - Writes `session_summaries` row.
   - Updates `tasks.last_activity_at`, `tasks.session_count`, `tasks.total_cost_usd`.

### 6.3 On-demand drilldown — MCP tools

Exposed by `nexus-server` over MCP. Always available to in-session agents.

| Tool | Purpose |
|---|---|
| `nexus_handoff(repo_owner, repo_name, branch?, task_id?)` | Returns recent summaries + active sessions for a task. Used by session-start hook and on-demand. |
| `nexus_session_replay(session_id, last_n_turns?)` | Returns raw agent_events for one session, default last 30 turns. |
| `nexus_session_summary(session_id)` | Returns a single session's `session_summaries` row. |
| `nexus_active_sessions(task_id?)` | Lists currently-active sessions; if `task_id` omitted, all. |
| `nexus_search(query, task_id?, repo?, since?)` | Content `LIKE` search across agent_events; falls back to FTS index when added later. |
| `nexus_files_touched(task_id, since?)` | List of files modified across all sessions on this task — points the new agent at relevant code. |
| `nexus_task_status(task_id)` | Aggregate stats for a task: sessions, agents, cost, latest summary. |

---

## 7. Awareness flow (use case 2)

The session-start hook already prepends an `active_sessions` warning when peers are present (see §6.1 step 5).

Mid-session, the agent can call `nexus_active_sessions` itself to re-check (e.g., before a large refactor). No locks. The user said **awareness only** — this is the entire mechanism. The user retains control over collision avoidance.

A future v2 may add soft leases (claim a directory or file scope), revisited only if real-world collisions surface.

---

## 8. Migration path from current state

Each step is independently shippable. Stop at any point with a working system.

1. **Port parsers and dedup_key logic into a stable module.** Move `src/nexus/cloud_function/main.py` parsers + `_make_dedup_key` into `src/nexus/parsers/` and `src/nexus/dedup.py`. Make them importable without GCP dependencies. Keep existing `tests/test_parsers.py` green.
2. **Stand up `nexus-server` skeleton.** OTLP receiver only. Run on Mac Mini. Keep AgentWeave's existing Tempo writer + Cloud-Function pull pipeline running in parallel for one week to verify span counts match between BQ `lakehouse.spans` and DuckDB `spans`.
3. **Cut AgentWeave to push-mode.** Add OTLP exporter to AgentWeave's deployment on NAS k3s; keep Tempo for its own UI; turn off `nexus-agentweave.timer` on NAS. The Tempo→GCS→BQ flow stops.
4. **Replace `sync_*.sh` with `nexus-collect`.** One host at a time: NAS first (lowest blast radius), then Mac Mini, then Work MacBook. Each host's shipper now writes to `mac-mini:~/nexus/incoming/<host>/` instead of `gs://nexus-raw-logs-26/`.
5. **Run PR #39's `migrate_to_duckdb.py` once** to seed the lakehouse with all historical data. Reconcile per-CSV row counts (input → dupes-dropped → output). This is the audit trail the PR review flagged as missing.
6. **Add `tasks` and `session_summaries` tables.** Backfill `tasks` from existing `agent_events` (one row per distinct `(repo, branch)` seen since 2026-01-15). Do **not** backfill summaries — they accrue going forward.
7. **Ship `nexus-hook` for Claude Code first** — cheapest, since it's a settings.json hook. Validate handoff feels right with a real session. **Iterate on the summary prompt template before rolling to other harnesses.** Prompt-iteration cycles are where this UX lives or dies.
8. **Roll hooks to OpenClaw, Hermes, pi-mono.** Per-harness wrapper script.
9. **Decommission GCP residue.** After 30 days dual-running with no row-count drift, `terraform destroy` the BQ dataset and Pub/Sub topic. Cloud SQL is already gone per PR #39.

### 8.1 What gets ported as-is
- `src/nexus/parsers.py` (Claude Code, Hermes, pi-mono, OpenClaw, AgentWeave)
- `src/nexus/agent_aliases.py`
- `src/nexus/models.py` (the `AgentEvent` contract)
- The existing CLI commands (`search`, `replay`, `trace search`, `trace get`, `repos`) — change query backend from BigQuery to DuckDB, keep the surface
- The dedup_key MERGE logic
- AgentWeave deployment on NAS — unchanged except for adding the OTLP exporter

### 8.2 What gets deleted
- `src/nexus/cloud_function/` (entire directory)
- BigQuery resources in `iac/main.tf`
- `scripts/hydrate_lakehouse.py`
- `scripts/init_db.py` (Cloud SQL bootstrap)
- `scripts/test_vertex_*.py`
- `scripts/sync_*.sh` (replaced by `nexus-collect`)
- systemd units `nexus-agentweave.{service,timer}`
- launchd plist `com.nexus.macmini-shipper.plist`
- Top-level `nexus.duckdb` checked into the repo (see PR review)

### 8.3 What gets renamed/relocated
- `migrate_to_duckdb.py` → `scripts/migrate_to_duckdb.py` (out of repo root)
- `nexus-context-engine-26` and `arnabmac` literals → `~/.nexus/config.toml` + env vars

---

## 9. Error handling & operational concerns

| Scenario | Behavior |
|---|---|
| `nexus-server` down | Collectors keep writing to local staging; cursor doesn't advance until rsync succeeds. AgentWeave OTLP exporter buffers in-memory then drops after 30 s — Tempo on NAS remains source of truth for replay. |
| Summarizer fails / model unavailable | `session_summaries.status='errored'`, retry with exponential backoff (3 tries, max). Surfaced in `nexus-server status`. Handoff hook gracefully shows "no summary available, last activity at X" instead of failing. |
| Hook timeout | 2 s budget. On timeout, hook prints `(nexus offline)` to stderr and exits 0 — never blocks agent startup. |
| Duplicate writes | dedup_key MERGE handles it. `nexus-server doctor` audits row counts vs file inventories. |
| Disk pressure | Parquet zstd at observed 96% compression → ~2 GB/year at current volume. Mac Mini has plenty. `--max-retain-days` knob for hot partitions, default off. |
| Backups | Nightly `rclone` of `~/nexus/parquet/` to NAS. Defined in install script, not separate infra. |
| MCP server unreachable from Work MacBook | Hook logs offline, returns no handoff context. Session proceeds normally. Out-of-office mode is acceptable degradation. |
| Two collectors run on the same host | Cursor file uses advisory `flock`; second instance exits cleanly. |
| AgentWeave emits a span with malformed PROV attributes | Server logs at WARN, drops the span, increments a `nexus_ingest_dropped_total{reason="malformed_otel"}` counter. Doesn't fail the batch. |

---

## 10. Testing strategy

| Component | Tests |
|---|---|
| Parsers (`src/nexus/parsers/`) | Existing `tests/test_parsers.py`, `test_agentweave_parser.py`, `test_export_agentweave_tempo.py` keep working. Pure functions over fixture files. |
| `nexus-server` | Integration tests using `pytest` + temp DuckDB file. One test per MCP tool. One end-to-end test fires fixture OTLP spans + drops fixture JSONL into watch dir, asserts handoff output. |
| `nexus-collect` | Per-source parsers reuse existing fixtures. Cursor-resume tests: process file, kill, restart, ensure no duplicate rows in output. |
| `nexus-hook` | Snapshot tests on the rendered handoff string for a fixture `task_id`. Tests for the 2 s timeout path (mock MCP delay → hook exits clean). |
| Summarizer | **No tests against the live LLM.** Snapshot the *prompt* the summarizer constructs from a fixture session; review prompt diffs in PR. Output quality is judged by the user, not asserted. |
| Migration script | One reconciliation test: run on a fixture CSV, assert row counts match expected (input, dupes, output). |

---

## 11. Open-source / pluggability

Choices made now to keep the OSS option open without paying for it today:

- **`AgentEvent` Pydantic model is the documented public contract.** Third-party collectors target that schema and drop files in `~/nexus/incoming/<host>/`.
- **MCP server is the only agent-facing API.** No proprietary client SDK to maintain.
- **Settings via `~/.nexus/config.toml` + env vars**, not hardcoded paths or project IDs. `nexus-server init` writes a default config.
- **Apache-2.0 license file** added to repo root.
- **Parsers as Python entry points.** `pyproject.toml` exposes a `nexus.parsers` entry-point group; built-ins ship in this repo, third parties can `pip install nexus-parser-foo` and have it auto-register.
- **No GCP-specific code** in the runtime. The migration script is a one-shot tool kept under `scripts/` for archive purposes.

Deferred for v2: hosted version, multi-tenant `principal_id` separation, auth on the MCP server (today it's local-network only), web UI.

---

## 12. Out of scope for this design

- Vector search / semantic embeddings.
- Web UI for browsing sessions.
- Real-time event bus (NATS / Redis Streams).
- Soft leases / hard locks / branch isolation for concurrent agents.
- `handoffs` table for explicit handoff events.
- Multi-principal auth and tenant separation.
- Migration of pre-2026-01-15 historical data (none exists).

---

## 13. Success criteria

The redesign is successful when all five hold:

1. Starting a Claude Code session on the Mac Mini in the `nexus` repo on branch `docs/local-lakehouse-migration` automatically surfaces the most recent session summary from any agent that worked on that branch.
2. Starting a session on the Work MacBook in any repo over Tailscale gets the same handoff context within 2 seconds, or fails open without blocking.
3. AgentWeave spans land in `nexus-server`'s OTLP receiver and appear in DuckDB `spans` within 30 seconds of being emitted.
4. A session that ends has a `session_summaries` row within 60 seconds of `SessionEnd`.
5. `nexus-server doctor` reports 0 row-count drift between collector outputs and Parquet contents over a 7-day window.

---

## 14. Open judgment calls (worth user check)

These are decisions the implementer should not unilaterally reverse without discussion:

- **`dedup_key` vs `id` as the merge key.** Spec uses `dedup_key` because main shipped it (commit a9f1a5f). PR #39's migration script uses `id` instead — a divergence that needs reconciliation. Picking `dedup_key` everywhere.
- **Summarizer model = claude-haiku-4-5-20251001.** Cheap and fast, but quality matters here. At ~50 sessions/day the cost difference between haiku and sonnet is pennies. Starting with haiku; escape hatch to upgrade per-session via config.
- **30-minute active-session window.** A session crashed without firing SessionEnd will appear "active" for 30 min after its last event. Tunable.
- **5-minute collector cadence.** Down from 15. If this is too chatty, we can back it off; Mac Mini load is the constraint to watch.

---

## 15. References

- PR #39 — local lakehouse migration: https://github.com/arniesaha/nexus/pull/39
- AgentWeave repo (NAS k3s deployment): https://github.com/arniesaha/agentweave
- `docs/local-lakehouse-spec.md` — historical migration record (kept)
- `docs/architecture.md` — pre-redesign architecture (kept for context, superseded by this spec)
- `docs/agentweave-integration.md` — pre-redesign Tempo-pull integration (to be superseded by §5.1 OTLP push)
- `src/nexus/models.py` — the `AgentEvent` public contract
- `src/nexus/parsers.py` — per-source parsers, ported into the new server
- `src/nexus/cloud_function/main.py` — source of dedup_key + AgentWeave parser logic (to be deleted after port)
