# Continuity activation: bounded private prebind investigation

Baseline e07f9a2; the accepted archive-pruning and parent instrumentation
checkpoints remain in branch history. Operational identifiers stay outside Git.
No live connections, deployment, service actions, configuration changes or push.

## Causal status

**Proved defect, repaired:** IncomingWatcher.start executes backlog ingestion on
main's foreground prebind path. sorted(incoming.rglob("*.jsonl")) enumerated the
entire .processed archive before filtering it. A regression fails on the parent
commit when scandir enters .processed; it passes after pruning those directories
with os.walk. Pending paths retain global sorted order, file symlinks work,
directory symlinks are not followed, and one failed file does not stop the pass.
This defect predates e07f9a2. Its contribution to the failed candidate's 90-second activation
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

## Initial foreground validation

112 passed in 21.93 seconds: test_startup_rehearsal.py, test_watcher.py,
test_server_cli.py, schema deferred-view binding contract and the preserved
PostgreSQL bounded-migration-lock/recovery contract. A separate focused run of
the 13 rehearsal tests passed in 1.86 seconds and printed the timelines above.
Runtime dependencies came from the existing candidate Python 3.14 environment;
pytest/formatters ran in an isolated uv tool environment. No global installation.

## Updated canonical evidence and decision

Parent-provided, read-only metadata for the actual configured external volume:
pruned incoming scan: six directories, zero pending files, five nested processed
roots, 0.022 seconds. Archive scan: five directories, 387 files, 0.003 seconds.
These counts do not support archive traversal or pending-file retries as the
canonical cause. Historical topology is unavailable; the 93-second synthetic
mechanism is not activation evidence and does not justify changing retry policy.
The retained activation log proves artifact/PID attribution and 90 unsuccessful
socket probes, but contains no application phase trace or stack.

The parent separately restored the PostgreSQL backup with strict pgvector 0.8.6
and 289 sessions; fresh-analytical bootstrap took 11.760 seconds. That includes
initialization work excluded by this worker's empty, already-initialized PG pool
fixtures. The strict restored database is not available to these local tests;
this worker has not replayed it. Small local fixtures cannot reproduce actual
analytical file/WAL recovery, checkpoint cost, volume permissions, service-launch
context, or the original network connection path. Cold PG pool configuration
runs several session setup statements before its statement timeout is installed;
pool admission has a bound, but native connection/setup work needs stack evidence.
No causal repair to that path is justified by the evidence available.

Independent review of archive pruning: ordering and file-symlink semantics are
preserved, directory links are not followed, failures leave pending files intact,
and retention/observer/worker ordering is unchanged. Review of migration locking:
transaction-local lock_timeout is stricter than statement_timeout; the advisory
lock still serializes schema history, failed migrations roll back and successful
ones are skipped on replay. New checks verify lock_timeout resets after both
rollback and commit and statement_timeout remains unchanged. Driver exceptions
in the bootstrap logger are now reduced to their type to avoid row/query leakage.

The next decision is approval of one bounded diagnostic start under the parent's
integration ownership, after the artifact/backup/drain and credential fences in
[the profiling proposal](continuity-startup-profiling-proposal.md) pass. It is a
new diagnostic operation, not a blind rollout retry. No activation has been
executed or requested by a tool. Canonical startup and continuity remain unmet.

## Follow-up review validation

32 affected tests passed in 5.42 seconds after the additional diagnostic/logging
changes: new startup-diagnostic contracts, fenced prebind variants, SIGUSR1
contracts, bounded migration lock/rollback and concurrent schema starters.
No full-suite or NAS test run was performed. The initial 112-test proof is
preserved; the affected prebind variants were repeated because their phase and
command-cleanup code changed. Default-off diagnostics start no thread and perform
no I/O. A child-process test proves sampling begins before a deliberately blocked
heavy server import; another proves a stack sample does not terminate startup.
Scoped and tracked-tree public release audits reported zero findings. Public
reports contain no private paths or operational session/process identifiers.
