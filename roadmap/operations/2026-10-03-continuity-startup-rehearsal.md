# Continuity activation: bounded private prebind investigation

Session: harness-ad8c3b34-ea5c-4802-84d8-04d1c58560b9. Base e07f9a2;
parent instrumentation 5d32b6fb98344b192258876b208c0947a9c49381 preserved.
No live connections, deployment, service actions, configuration changes or push.

## Causal status

**Proved defect, repaired:** IncomingWatcher.start executes backlog ingestion on
main's foreground prebind path. sorted(incoming.rglob("*.jsonl")) enumerated the
entire .processed archive before filtering it. A regression fails on the parent
commit when scandir enters .processed; it passes after pruning those directories
with os.walk. Pending paths retain global sorted order, file symlinks work,
directory symlinks are not followed, and one failed file does not stop the pass.
This defect predates e07f9a2. Its contribution to PID 20843's 90-second activation
failure is **a hypothesis**, not proof that continuity caused or fixed the stall.

**Separate reproduced mechanism, policy unchanged:** default ingestion lock
retries wait 1+2+4+8+16 = 31 seconds per pending batch. Three batches produce
93 seconds of configured waits. A synthetic test runs the real backlog and
handler with 18 injected file-lock failures and records sleep calls without
waiting or opening any database. This could account for the activation window
only if three pending batches and corresponding contention actually existed.
An isolated DuckDB file-lock rejection returning quickly does not rule out
these application-level retries after bootstrap has completed.

**Not reproduced in fixtures:** slow consent initialization, control readiness,
PG pinning, auth initialization or service construction. Existing persistent
views plus an intentionally invalid private historical Parquet footer do not
prevent prebind completion; historical binding is deferred. This does not rule
out actual-volume catalog/WAL recovery, checkpoint, filesystem or permission
problems, or contention with production background work.

## Rehearsal fence and limits

The tests invoke the actual main Click run path and stop with a BaseException
at start_resilient_metrics_server, before any listener exists. All scheduled
analytical/warmup threads are inert; exporter and worker starts are replaced.
Watcher start replays only real backlog enumeration, without observers,
retention, ingestion or queue effects. Network connect/connect_ex/bind,
subprocess execution, executor submission, ingestion and push delivery are
forbidden and asserted unused. Push configuration receives only an inert mock.
Auth uses a synthetic token and private credential storage.

Private PostgreSQL is a disposable Unix-socket cluster owned by the test fixture;
caller-provided test DSNs are rejected. Its empty schema is explicitly initialized
before replay. PG pool creation, first schema initialization and fixture creation
are excluded from the recorded timeline. PGvector is not required for these
startup contracts; this is not the parent's strict pgvector backup rehearsal.
All DuckDB, parquet, incoming and consent paths are private test fixtures.
MCP/OTLP are skipped; no model is executed. Background contention is intentionally
excluded, not claimed falsified. No production inputs were copied into Git.

Each measured phase has a 10-second faulthandler deadline with all-thread stack
capture and process exit. The hang test independently verifies dump/exit in a
short-lived child. Phase error logs contain names, elapsed times and exception
classes, never exception/query payloads. Remaining unmeasured constructor gaps,
imports and actual-volume I/O require activation evidence.

## Exact representative timeline

Seconds, private PostgreSQL, existing analytical views, analytical pin off.
Rounded to four decimals; 0.0000 denotes below measurement resolution.

| Phase | Seconds |
| --- | ---: |
| resolve_startup_config | 0.0004 |
| pin_analytical_connection | 0.0000 |
| bootstrap_catalog_and_control_store | 0.0136 |
| require_control_store_ready | 0.0003 |
| initialize_central_consent | 0.0008 |
| build_worker_archive_resolver | 0.0000 |
| pin_control_plane_connection | 0.0001 |
| sweep_orphaned_snapshot_scratch | 0.0001 |
| watcher_start_and_backlog | 0.0001 |
| start_control_outbox_exporter (patched) | 0.0001 |
| start_advisory_worker (patched) | 0.0001 |
| start_usage_rollup (patched) | 0.0000 |
| start_native_usage_rollup (patched) | 0.0000 |
| embeddings_configuration | 0.0000 |
| load_metrics_auth | 0.0008 |
| configure_metrics_push (patched) | 0.0001 |
| construct_metrics_services | 0.0002 |

Across eight variants: catalog/control bootstrap 0.0063–0.0298 seconds;
analytical pin off below resolution, enabled 0.0056–0.0088 seconds;
central consent 0.0007–0.0008 seconds; auth 0.0003–0.0008 seconds;
service construction 0.0002–0.0003 seconds. HTTP binding was intercepted.

## Foreground validation

112 passed in 21.93 seconds: test_startup_rehearsal.py, test_watcher.py,
test_server_cli.py, schema deferred-view binding contract and the preserved
PostgreSQL bounded-migration-lock/recovery contract. A separate focused run of
the 13 rehearsal tests passed in 1.86 seconds and printed the timelines above.
Runtime dependencies came from Studio's existing e07f9a2 Python 3.14 environment;
pytest/formatters ran in an isolated uv tool environment. No global installation.

## Next input, in priority order

1. Existing PID 20843 activation stderr/log excerpt: last completed startup
   message, watcher backlog count/duration and any DuckDB lock-retry records.
   Parent can redact paths; no payloads, query text or credential DSNs needed.
2. Metadata-only incoming topology at activation: pending JSONL count outside
   .processed, archive directory/file counts, and volume identity. Together with
   retry logs this separates archive traversal from the 31-second-per-file waits.
3. Snapshot scratch directory counts/ages and stat type/size/volume metadata for
   analytical DuckDB/WAL and the adjacent consent file. Recursive scratch deletion,
   DuckDB recovery/checkpoint and direct consent-file reads remain unbounded I/O.
4. If evidence points to pin/bootstrap rather than watcher: a parent-provided,
   verified private logical analytical copy plus minimal synthetic parquet with
   equivalent schema/topology, excluding transcripts and credentials. Existing
   file/WAL size and volume/permission context are necessary; another empty lakehouse
   cannot reproduce that input. No live read-only connection is authorized here.

No rollout retry is requested. Canonical continuity remains unverified.
