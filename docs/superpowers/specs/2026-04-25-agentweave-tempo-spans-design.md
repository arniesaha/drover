# AgentWeave → Nexus: Tempo spans pipeline (Stage 5a)

Status: Approved 2026-04-25
Owner: Arnab
Supersedes: nothing (extends `docs/agentweave-integration.md`)

## Goal

Pull AgentWeave OTel spans from the on-NAS Tempo instance into the Nexus
lakehouse so agents can query trace, token, and cost facts via the existing
`nexus` CLI. This is the first slice of Stage 5; advanced features
(session-graph reconstruction, decision extraction, pgvector enrichment) are
explicitly deferred.

## Non-goals

- Reconciling AgentWeave agent/session identifiers with the existing
  `agent_events` shippers (separate follow-up).
- Streaming, tailing, or live trace views.
- Replacing or extending `agent_events`.
- Writing AgentWeave-derived text into pgvector.
- Decision extraction (`lakehouse.decisions`).

## Architecture

```
Tempo (k3s, NAS)            NAS local                 GCS raw archive             BigQuery lakehouse        nexus CLI
─────────────────           ─────────────             ─────────────────────       ──────────────────        ─────────────
agentweave-proxy   ──pull──► /var/lib/nexus/   ─sync──► gs://nexus-raw-logs-26/  ─ETL─►  spans              nexus trace search
mux-router                   tempo-export/             agentweave/tempo/                  traces            nexus trace get
                                                       dt=YYYY-MM-DD/                     agent_events
                                                         search-window-*.json             (existing)
                                                         traces/<trace_id>.json
```

The puller and shipper run on ARNABSNAS as a systemd user timer. Tempo's
NodePort is reachable only on the LAN, so the NAS is the only sensible host;
this also keeps Tempo private. The Cloud Function ETL is extended to dispatch
on the GCS object prefix and writes a new `lakehouse.spans` table. The CLI is
extended with a `trace` command group that reads BigQuery directly.

## Components

### `scripts/export_agentweave_tempo.py` (new)

Pure puller. No GCS calls.

- Env: `TEMPO_BASE` (default `http://192.168.1.70:31989`),
  `NEXUS_TEMPO_EXPORT_DIR` (default `/var/lib/nexus/tempo-export`),
  `NEXUS_TEMPO_WINDOW_MIN` (default 30).
- Calls
  `GET /api/search?q={resource.service.name="agentweave-proxy" || resource.service.name="mux-router"}&start=&end=&limit=1000`,
  paginating using whatever cursor Tempo returns if the result set is capped.
- Writes one search response per run:
  `dt=YYYY-MM-DD/search-window-<startISO>-<endISO>.json`.
- For each unique `traceID` in the response, fetch
  `/api/traces/<id>?format=json` and write
  `dt=YYYY-MM-DD/traces/<trace_id>.json`. **Skip if the file already exists
  locally** (cheap dedupe before GCS, avoids re-hitting Tempo).
- Tempo unreachable → log + exit non-zero. Per-trace fetch failures are logged
  but don't fail the run.

### `scripts/sync_agentweave_logs.sh` (new)

`gsutil -m rsync -r /var/lib/nexus/tempo-export/ gs://nexus-raw-logs-26/agentweave/tempo/`.
No `-d` (issue #3 lesson — don't let local TTLs delete the GCS archive).
Symmetric with `sync_nas_logs.sh`.

### `scripts/systemd/nexus-agentweave.service` and `.timer` (new)

Oneshot service + 15-min timer with `Persistent=true` and
`RandomizedDelaySec=60`. Mirrors `nexus-shipper.timer`. ExecStart runs the
puller then the shipper.

### `scripts/install_agentweave_shipper.sh` (new)

Idempotent installer mirroring `install_nas_shipper.sh`. Copies puller,
shipper, and unit files; reloads and enables the timer.

### `src/nexus/cloud_function/nexus/parsers.py` (extend)

Add `parse_agentweave_trace(trace_dict) -> list[dict]`. Handles the
`{batches: [{resource, scopeSpans:[{spans:[…]}]}]}` shape returned by
`/api/traces/<id>?format=json`. Pulls resource-level attributes onto every
span (e.g. `service.name`). Resilient to missing optional attributes.

### `src/nexus/cloud_function/main.py` (extend)

Dispatch on GCS object prefix:

- `agentweave/tempo/traces/...json` → AgentWeave parser → BigQuery
  `lakehouse.spans` MERGE.
- `agentweave/tempo/search-window-*.json` → archive only (no parse). Useful
  for audit and re-pull.
- All other prefixes → existing sessions ETL path, untouched.

### `src/nexus/cli.py` (extend)

Add `trace` Click group with `search` and `get` subcommands. Both are pure
BigQuery reads; no Tempo dependency at query time.

## Data model

`lakehouse.spans`, partitioned on `DATE(start_time)`, clustered on
`(agent_id, session_id, trace_id)`.

| Column | Type | Source |
|---|---|---|
| `trace_id` | STRING | OTel `traceId` |
| `span_id` | STRING | OTel `spanId` |
| `parent_span_id` | STRING NULLABLE | OTel `parentSpanId` |
| `name` | STRING | span name |
| `service_name` | STRING | resource attr `service.name` |
| `start_time` | TIMESTAMP | from `startTimeUnixNano` |
| `end_time` | TIMESTAMP | from `endTimeUnixNano` |
| `duration_ms` | FLOAT64 | derived |
| `activity_type` | STRING | `prov.activity.type` |
| `agent_id` | STRING | `prov.agent.id` |
| `agent_type` | STRING | `prov.agent.type` |
| `session_id` | STRING | `session.id` ∥ `prov.session.id` |
| `parent_session_id` | STRING | `prov.parent.session.id` |
| `project` | STRING | `prov.project` |
| `task_label` | STRING | `prov.task.label` |
| `llm_provider` | STRING | `prov.llm.provider` |
| `llm_model` | STRING | `prov.llm.model` |
| `prompt_tokens` | INT64 | `prov.llm.prompt_tokens` |
| `completion_tokens` | INT64 | `prov.llm.completion_tokens` |
| `total_tokens` | INT64 | `prov.llm.total_tokens` |
| `cache_read_tokens` | INT64 | `tokens.cache_read` |
| `cache_write_tokens` | INT64 | `tokens.cache_write` |
| `cost_usd` | FLOAT64 | `cost.usd` |
| `prompt_preview` | STRING | `prov.llm.prompt_preview`, truncated 2000 chars |
| `response_preview` | STRING | `prov.llm.response_preview`, truncated 2000 chars |
| `attributes_json` | JSON | full span attribute map (catch-all) |
| `raw_object_uri` | STRING | `gs://…/agentweave/tempo/dt=…/traces/<trace_id>.json` |
| `ingested_at` | TIMESTAMP | ETL insert time |

The full attribute map is preserved in `attributes_json` so AgentWeave can add
new typed fields without losing data while we wait to add them as columns.

## Dedup contract

Three layers, in order:

1. **Local file** — puller skips `/api/traces/<id>` if
   `traces/<trace_id>.json` already exists locally. Cheap; avoids re-hitting
   Tempo across overlapping windows.
2. **GCS** — `gsutil rsync` (no `-d`) is content-aware; identical files are
   skipped on upload.
3. **BigQuery** — ETL uses
   `MERGE INTO lakehouse.spans USING <staged> ON trace_id=… AND span_id=…`.
   The staging side filters to spans with `start_time >= NOW() - 24h` so MERGE
   reads at most one day of partitions, regardless of how far back the trace
   data claims to go.

Pull cadence: every 15 min, 30-minute overlap window.

## CLI

```
nexus trace search [--agent X] [--session Y] [--project Z]
                   [--since 24h] [--limit N] [--format text|json|jsonl]
nexus trace get <trace_id> [--format text|json|jsonl]
```

`trace search` groups by `trace_id`, returns one row per trace using the root
span (lowest `start_time` with NULL `parent_span_id`) for display:
`trace_id, service_name, name, agent_id, session_id, start_time, duration_ms,
total_tokens, cost_usd`.

`trace get` returns the full span list for a single trace, ordered by
`start_time` and parent/child topology. JSONL is one span per line so output
composes with `jq` and `grep`.

## Error handling

- **Tempo unreachable** at puller: log, exit non-zero; systemd retries.
  No partial search-window file.
- **Malformed span** in ETL: log trace_id and GCS URI, skip the span,
  continue. Persistent recording is issue #5's job, not this slice's.
- **Empty search response**: still write the search-window file (proves the
  puller ran), skip the trace-fetch loop.
- **Schema drift**: `attributes_json` preserves everything; missing typed
  columns are NULL but the data is recoverable.

## Identifier reconciliation (intentionally deferred)

Sample comparison:

- `lakehouse.agent_events.agent_id` uses `<host>-<tool>` →
  `nas-claude`, `nas-openclaw`, `macmini-hermes`, `macmini-pimono`,
  `macmini-claude`, `unknown-claude`.
- AgentWeave `prov.agent.id` uses `<tool>-<host>` → `claude-code-nas`.
- Session IDs differ in scope: Claude JSONL session UUIDs are file-scoped;
  `session.id` in AgentWeave is a long-lived logical handle (e.g.
  `claude-code-nas-main`).

Joining `spans.agent_id = agent_events.agent_id` returns zero rows today.
This slice does not attempt to reconcile. Follow-ups will land as a separate
`agent_aliases` mapping and a derived `session_links` table.

## Testing

- **Unit**: parser converts a fixture trace JSON (sampled live trace
  `7b15218059664734d870ec48b999e97f`) into the expected list of span dicts.
  Covers nested resource attrs, multi-batch traces, missing optional
  attributes.
- **Integration (manual)**, documented in this spec:
  1. Run puller against live Tempo; verify local files appear under
     `/var/lib/nexus/tempo-export/dt=…/`.
  2. Run shipper; verify GCS objects appear under
     `gs://nexus-raw-logs-26/agentweave/tempo/dt=…/`.
  3. Wait for Cloud Function ETL; verify rows land in `lakehouse.spans`.
  4. Run `nexus trace search --since 1h --limit 5` — expect ≥1 trace.
  5. Run `nexus trace get <id>` — expect full span list.
- No automated end-to-end test: Tempo is not reachable from CI.

## Follow-ups (file as separate GitHub issues)

1. `agent_aliases` reconciliation — bridge `nas-claude` ↔ `claude-code-nas`.
2. `session_links` derivation — relate Claude JSONL session UUIDs to
   AgentWeave logical sessions.
3. `nexus trace tail` — streaming/recent-traces command.
4. `nexus session graph <session-id>` — span-tree reconstruction.
5. Embed selected span text into pgvector (prompt/response previews,
   decision summaries).
6. Decision extraction job — derive `lakehouse.decisions` rows from trace
   topology.
