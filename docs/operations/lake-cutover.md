# DuckLake v2 cutover runbook

**Status:** production completed this scripted, gated DuckLake v2 procedure on
2026-10-06 with v0.6.2. The steps remain the operator runbook for a new lake,
rehearsal, recovery, or explicitly approved future cutover. The earlier
rebuild-based procedure, rolled back on 2026-10-04, is retired. See git history
for it.

The order is fixed: **preflight → gate → backup → fence/switch → verify → soak**,
with **rollback** available throughout the soak. Every step is a command that
prints JSON or exact actions. Nothing below is improvised at the keyboard.

## One variable names everything

Every database, path and environment name comes from `--lake` (here `$LAKE`)
and `--lake-root` (or `DROVER_LAKE_ROOT`). Nothing is hard-coded:

| Name | Derived value |
|---|---|
| Target data root | `$DROVER_LAKE_ROOT/$LAKE` |
| Target catalog database | `drover_lake_$LAKE` |
| Catalog group roles | `drover_lake_${LAKE}_{reader,exporter,admin}` |
| Service DSN env vars (in the plist) | `DROVER_LAKE_${LAKE^^}_READER_DSN`, `..._EXPORTER_DSN` |
| Operator DSN env var (shell only) | `DROVER_LAKE_${LAKE^^}_ADMIN_DSN` |
| Gate scratch databases | `drover_gate_${LAKE}_control`, `drover_gate_${LAKE}_lake` |
| Gate directory / verdict | `$DROVER_LAKE_ROOT/.gate/$LAKE/`, `.gate/$LAKE.verdict.json` |
| Backups and switch record | `$DROVER_LAKE_ROOT/.cutover-backups/$LAKE/` |

`drover-server gate --preflight` prints the full mapping under `resources`.

```sh
export LAKE=v2 DROVER_LAKE_ROOT=/path/to/lakes
export DROVER_LAKE_EXTENSION_DIR=/path/to/verified/extensions
export DROVER_LAKE_ENGINE_SHA256=<installer-verified-engine-digest>
DS="$HOME/.drover/runtime/current/bin/drover-server"   # the service's own build
```

Run every command with the service's own `drover-server` (`$DS`). Engine
verification hashes the running interpreter's DuckDB, so a different venv
proves nothing about the hub.

## 0. Build the target lake (while production stays on legacy)

The v2 lake is fresh. It is rebuilt from an empty seed, then 60 days are
imported. Production is not stopped for this step.

```sh
$DS lake seed-tar --output /tmp/$LAKE-seed.tar
$DS lake rebuild --from-tar /tmp/$LAKE-seed.tar --data-root "$DROVER_LAKE_ROOT/$LAKE" \
  --catalog-dsn-env DROVER_LAKE_${LAKE^^}_ADMIN_DSN
$DS lake provision-exporter --data-root "$DROVER_LAKE_ROOT/$LAKE" \
  --catalog-dsn-env DROVER_LAKE_${LAKE^^}_ADMIN_DSN --exporter-role drover_lake_${LAKE}_exporter
# Grant roles: provision_catalog_roles(spec, prefix="drover_lake_$LAKE"),
# see lake-catalog-roles.md; then grant the login users to the groups.
$DS lake verify --data-root "$DROVER_LAKE_ROOT/$LAKE" \
  --catalog-dsn-env DROVER_LAKE_${LAKE^^}_ADMIN_DSN
$DS lake import --since "$(date -u -v-60d +%F)" --data-root "$DROVER_LAKE_ROOT/$LAKE" \
  --catalog-dsn-env DROVER_LAKE_${LAKE^^}_ADMIN_DSN --legacy-root ~/.drover/parquet
```

Add the reader and exporter DSNs to the service plist's `EnvironmentVariables`.
Leave `[analytics]` on legacy; the switch flips it.

## 1. Preflight (read-only)

```sh
$DS gate --lake $LAKE --preflight --stage gate   --release v0.6.0   # before the gate
$DS gate --lake $LAKE --preflight --stage switch --release v0.6.0   # before the switch
```

Preflight reads `~/Library/LaunchAgents/com.drover.server.plist` (`--plist`)
and the config its `--config` argument names. It checks everything in the
service's own environment. One JSON verdict; exit 0 only if every check passes:

| Check | Fails when |
|---|---|
| `service_config` | label is not `com.drover.server`, or the control store is not PostgreSQL |
| `service_env` | reader/exporter/control DSN missing from the plist; a DSN names another catalog; `DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT` unparseable |
| `catalog_role_reader` | connecting **as the reader** fails, or it can INSERT, or it has database CREATE |
| `catalog_role_exporter` | connecting **as the exporter** fails, it cannot INSERT, or it has database CREATE |
| `control_store` | read-only session cannot read migrations; > 10,000 identity sessions; any unacknowledged `lake_export_batches` row |
| `lake_root_disk_free` / `ram` | below `--min-free-gb` (50) / `--min-ram-gb` (16) |
| `installed_version` | the service binary's `--version` or `runtime/current` differs from `--release` |
| `updater_state` | updates enabled and not pinned to the release, or `pending_verification.json` exists (harnessd re-flips `runtime/current` on start) |
| `lake_runtime_pins` | extension/engine digests do not verify |
| `target_paths` | gate stage: gate directory not empty. Switch stage: no serving proof, data root inside the legacy parquet dir, backup root not writable, or no passing gate verdict from the last 24 h |

## 2. Gate (read-only on production)

```sh
export SCRATCH_PG_DSN="host=/tmp/scratch-pg port=55432 dbname=postgres user=$USER"
$DS gate --lake $LAKE --legacy-root ~/.drover/parquet \
  --source-dsn-env DROVER_CONTROL_DSN --scratch-admin-dsn-env SCRATCH_PG_DSN \
  --memory-limit 4GB > gate-$LAKE.json; echo "exit $?"
```

The gate never writes to the source, the target lake, the service or any
production path:

- **Control store:** `pg_dump --schema=drover_control` in a read-only session,
  restored into `drover_gate_${LAKE}_control` on the scratch cluster. The gate
  refuses a scratch cluster that is the source's cluster unless
  `--allow-shared-cluster` is passed. Host URLs are blanked in the copy, so the
  spare hub never calls real harnessd hosts.
- **Lake:** a disposable rehearsal built exactly like step 0: seed rebuild,
  `provision_exporter`, verify, then `lake import --since <60d>` from
  `--legacy-root`, which is only read. The gate never exports into the target.
  An exporter fed by a copied outbox would leave receipts there that the real
  outbox could collide with.
- **Spare hub:** `drover-server run` on free loopback ports with its own HOME
  and its own incoming, parquet and data paths. Auth, updates, APNs, spans,
  MCP, summarizer, briefs and embeddings are off. The model policy is `cloud`
  with every credential stripped, so nothing copied leaves the host.
  `XPC_SERVICE_NAME` and every production DSN are removed from its env.

Verdict checks (all must pass; exit 1 on a failed check, 2 if the gate could not
complete):

| Check | Evidence |
|---|---|
| `identity_limit` | sessions in the copy vs the 10,000 identity limit (`open_history`) |
| `stale_export_batches` | unacknowledged `lake_export_batches` rows and catalog ids |
| `fresh_lake_seed_rebuild` | seed rebuild + exporter provisioning + verify succeeded |
| `lake_import` | rows selected/inserted, seconds, **import peak RSS** |
| `A5_startup_migrations` | released migration rows preserved; versions applied by startup |
| `A1_collector_to_lake` | collector JSONL visible in the lake ≤ 30 s; MCP replay; summary; recall |
| `A2_outbox_dedup_no_parquet` | each event once in the outbox; no ingest parquet |
| `A4_cockpit_overview` | 5 × `/cockpit/overview?days=30`, activity `ok`, p95 < 2 s |
| `A3_summarize_largest_session` | summary job for the largest imported session (by stored rows) succeeded |
| `A3_summarize_most_recent_session` | summary job for the most recently active session with a user/assistant turn succeeded |
| `A7_pre_watermark_archived` | a session older than the watermark replays as `archived` |
| `A6_rollback_outbox_replay` | `outbox replay --sink legacy` restores gate events; `/healthz` ok |
| `hub_rss_budget` | **spare hub peak RSS** ≤ `DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT` |

The hub is sampled every 50 ms from start to shutdown. `rss.hub_readyz_memory`
adds the hub's own guard state and its query-children peak. Sizes use DuckDB
units: `4GB` = 4,000,000,000 bytes, `4GiB` = 4,294,967,296.

### Scratch rehearsal (no production input at all)

```sh
uv run python scripts/lake_gate_rehearsal.py --extensions ~/.cache/drover-lake-ext
```

This starts two throwaway initdb clusters on spare ports: a "source" restored
from the committed production-schema fixture, and a separate scratch cluster.
It then runs the gate against the synthetic 50k-event acceptance lake.
On 2026-10-05, every check passed. Results:

| Measure | Value |
|---|---|
| Spare hub peak RSS | 223,166,464 bytes (0.21 GiB; 363 samples) against a 4,000,000,000-byte budget |
| Import step peak RSS | 1,353,170,944 bytes (1.26 GiB; 44,362 rows since 2026-08-06, 1.9 s) |
| Seed rebuild + provision + verify | 7.7 s, peak 301,875,200 bytes |
| Cockpit overview p95 | 1.34 s (limit 2 s) |

That workload is about 1% of production. It shows the gate and the v2 paths
work. It does **not** show that v2 fixes the ~8 GiB production RSS. Only a gate
run with production's control copy and the real legacy root answers that.

The first rehearsal found an S4 regression, fixed in the same change:
`open_history` hashed four identity columns, but the read-model child re-read
three. With any registered session, cockpit lake activity was therefore always
`unavailable` (`analytics_identity_changed`). `task_status` also unpacked those
rows into three names. Both now use `serving.IDENTITY_QUERY`, and
`test_a4_cockpit_activity_with_registered_sessions` pins it.

### A3/A4 at production scale (2026-10-06)

A gate run on a copy of production (3,331,991 events imported) failed only
`A3_summarize_largest_session`, with `{"error": "analytics_deadline_exceeded"}`.
`A4_cockpit_overview` passed at p95 1.99 s against the 2 s limit. The largest
session, `3aa811ed-…`, has **578,239 events** over 16 days. Its newest ~8 days
are attachment/bridge events with no substantive turn. The design assumed
about 40k events.

The failure was not the summarizer. A3's own picker
(`GROUP BY session_id … LIMIT 1`) ran through the serving `agent_events` view.
That view parses every row's `raw_data` JSON, which took 6.7 s for 3.3M events,
so the 5 s query-child deadline killed it. The picker now counts stored rows on
`lake.agent_events`: 0.5 s, 121 MB.

The summarizer did complete on that session, but its cost grew with session
size. Every 1,000-event page re-ran the dedup window over the whole session:
26 children, about 1.3 s and 0.9–1.05 GB each, 35 s in total. It also kept the
*oldest* 25,000 raw events, and its prompt-window, task and repository reads
each scanned the whole session (1.8 s and 1.05 GB at 578k). At about 1M events
those reads reach the 2 GiB child cap. The read path is now bounded:

- One narrow query on the base table returns the timestamps of the newest
  25,000th stored event and of every 50,000th (`SESSION_SLICE_EVENTS`).
- **Prompt window:** walks those time slices newest first and stops once it
  has 30 substantive turns and a final assistant reply. On all three
  >300k-event production sessions, the result matched the unbounded window
  exactly. Prompt turns are searched at most `MAX_PROMPT_SCAN_EVENTS` (2M)
  back. A longer session with no substantive turn in that span is quarantined
  as `no_events`.
- **Files and tools:** derived from the **most recent**
  `MAX_RAW_EVENTS_PER_SESSION` (25,000) stored events, aggregated as distinct
  tool facts with counts in a single child. The summary then says
  *"Files and tools are derived from the most recent 25,000 of this session's
  N stored events."*, and the worker logs `Truncated session …`.
- Task id and repository use `any_value`, so they stop at the newest slice
  that has one.

Measured on the production v2 lake through the reader DSN with
`scripts/measure_a3_read_path.py`:

| Session (events) | Before | After |
|---|---|---|
| A3 picker | 6.7 s, deadline exceeded | 0.53 s, 121 MB |
| 578,239 | 35.2 s, 26 children, max 1.83 s / 1.05 GB | 7.1 s, 10 children, max 0.93 s / 409 MB |
| 569,357 | not measured | 5.7 s, 8 children, max 0.83 s / 312 MB |
| 321,929 | not measured | 4.0 s, 7 children, max 0.63 s |

A 100k-event slice took 3.5 s on a cold file cache, hence 50k slices.

**A4.** Each overview spent about 2 s in three serialized query children: a
snapshot token child, the read model, and a second token child. Every child
also loaded the identity table one row at a time (0.21 ms a row). The read-model
child now binds the lake snapshot itself. It reads the newest snapshot before
its transaction, and requires that snapshot to still be the newest inside the
transaction before and after the read. Identities load in one statement. On the
production lake, the lake side of the overview is 0.50–0.55 s (gate-like, 1,000
identities; it was 1.93–2.21 s). With a populated 30-day rollup and 3,000
identities it is 0.85–0.98 s.

CI's scale job (`scripts/acceptance-scale.sh`) now has a 640k-event session in
the 5M synthetic lake (`HUGE_SESSION_ID`), shaped like the production one. Its
substantive turns are only in its first 24k events. The A3 scale test picks
the largest session with the gate's own query and summarizes it. It requires
success, no child error, every child under 1 GiB, and the truncation note.

### The live session after the switch (2026-10-06)

The switch ran 7/7, then every summary of the live Claude Code session
`4c298e83-…` failed with `analytics_unavailable`. That session had 57,936
events, straddling the fenced delta import and the exporter. A3 had passed
because it summarizes the largest *imported* session, which is old history.

The real cause was hidden by the query child. Any non-`LakeError` exception
was reported as a bare `analytics_unavailable`. Driving the same read path
in-process against the production lake (reader DSN) showed
`OutOfMemoryException: … (721.1 MiB/732.4 MiB used)` in the tool-facts query,
at the child's 768 MB `memory_limit`. The 25k-event tail held only 17 MB of
`raw_data`. The projection ran ~40 separate `json_extract*` calls per event
under a `CASE WHEN json_valid(…)` guard, and with megabyte-sized events in a
vector the parsed documents exhausted the limit. That failed 4 of the 25 most
recently active production sessions.

- The projection now extracts every field with one multi-path
  `json_extract_string(raw_data, [paths])`, wrapped in `try()` instead of
  the `CASE` guard. The failing query went from OOM to 0.58 s at 256 MiB
  child peak RSS. On the other 21 recent sessions the files and tools are
  identical to the old projection.
- The child replies with a stable code plus a sanitized `detail` (class and
  first message line, DSN material scrubbed). DuckDB OOM gets its own code,
  `analytics_memory_limit_exceeded`. The summarizer logs and stores
  `code: detail`.
- New gate check `A3_summarize_most_recent_session`. On the production lake
  its picker selects `4c298e83-…` (0.98 s, 156 MiB).

## 3–5. Backup, fenced switch, verify

```sh
$DS cutover switch --lake $LAKE --legacy-root ~/.drover/parquet --dry-run   # read it
$DS cutover switch --lake $LAKE --legacy-root ~/.drover/parquet
```

Ordered steps (the dry run prints each one with its exact command or file):

1. `require_gate`: refuse unless `.gate/$LAKE.verdict.json` passed within 24 h.
2. `backup`: read-only `pg_dump --format=custom --schema=drover_control`, plus
   copies of `config.toml` and the plist, into
   `.cutover-backups/$LAKE/<utc>/`. Verified by the sha256 manifest and
   `pg_restore --list`. `drover-server cutover backup --lake $LAKE` runs it alone.
3. `stop_service`: `launchctl bootout gui/$UID/com.drover.server` (skipped if
   not loaded). Ingest stays durable in the PostgreSQL outbox while stopped.
4. `fenced_delta_import`: `lake import --since <newest day in the lake>` under
   the exclusive catalog fence, as the admin role. Dedup keys make it
   repeatable. A live exporter makes it fail with `lake_mutation_fenced`.
5. `flip_backend`: atomically rewrite only `[analytics]`: `backend=ducklake`,
   the reader/exporter env names, `data_root`, the extension/engine pins,
   `verification_sha256 = sha256(serving-proof.json)` and `epoch`. The new file
   is loaded before it replaces the old one. The first switch time is recorded
   in `cutover-state.json`.
6. `start_service`: `launchctl bootstrap gui/$UID <plist>`.
7. `verify`: `/healthz` must be `ok\nanalytical=ok` and `/readyz` 200 within
   300 s. The output includes `/readyz` memory.

Every step is idempotent, so rerun the command after fixing a failure. A
failure stops the procedure at that step with `<step>_failed: <cause>`.

## 6. Soak criteria (24 h before declaring success)

- `/readyz` stays 200; `memory.state` never `over`; peak RSS ≤ the analytical
  budget and no worse than the gate's `hub_rss_budget` evidence.
- `/cockpit/overview` `activity.status == "ok"`. Collector events reach the
  lake ≤ 30 s (A1). Summaries succeed. No `lake_export_*` errors in the log.
- `control_outbox_events` drain: no unacknowledged backlog older than 5 min.
- Recall/replay of a pre-watermark session reports `archived`, not `unavailable`.

Keep the legacy parquet root, the backups and the old config until the soak
passes and the release owner signs off.

## Rollback triggers

Roll back immediately on any of:

- `verify` fails, or `/readyz` is non-200 for more than 5 minutes.
- RSS over budget (`memory.state == "over"`) for more than 15 minutes, or any
  OOM or restart loop.
- An exporter error (`lake_export_*`), outbox backlog growth, or lost events.
- Cockpit activity, replay or recall `unavailable` for current sessions.

```sh
$DS cutover rollback --lake $LAKE --dry-run
$DS cutover rollback --lake $LAKE      # --since EPOCH overrides the recorded switch
```

Steps: `backup` → `stop_service` → `flip_backend` (`backend=legacy`) →
`outbox_replay` (`drover-server outbox replay --sink legacy --since <switch
epoch>` in the service's env; it dedupes by `dedup_key`, so it is safe to
repeat) → `start_service` → `verify`. Replay covers the outbox retention
window (`control_store.outbox_retention_days`, 14 days by default). Roll back
within that window.

## Not covered here

- Production execution, the v0.6.0 release, and installer provisioning of the
  login roles and plist DSNs remain separate, approved steps.
- `launchctl` steps are tested with an injected runner only. Their first real
  use is the production switch; read the dry run first.
- Persisted configuration epochs, CAS publication and zero-downtime handover
  are still out of scope (#509).
