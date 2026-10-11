# Exporter recovery investigation, October 11, 2026

Base: `1e72586fc01773d204259c7f5b58af38c6282fc0` (current main).
Sources: issue #578, issue #564, and read-only Studio server stderr.
No live lake or control database was opened. No service was changed or restarted.

## Confirmed failure mechanism

Studio stderr lines 980249 through 980262 show `lake_export_deadline` at
07:06:01, a cockpit `analytics_deadline_exceeded` at 07:06:04, and replacement
failures at 07:06:07, :15, :24, :37 and :58. These intervals fit five-second
analytics attempts plus the supervisor's 1/2/4/8/16-second backoff. The old
supervisor returns after five replacements without requesting server shutdown.

`ExporterLifecycle._run` creates a new `LakeOutboxExporter`, then calls
`_check_export_catalog` before setting the replacement running. That check
opens `HistoryConnection` and executes `SELECT 1`. This is not a cheap health
probe: the disposable child runs `check_proof` first, including the receipt
chain and every referenced lake file. Provisioned task projections also use
analytics queries before publication resumes. A deadline in either prerequisite
kills recovery before it reaches the frozen outbox input.

Previously those queries, checkpoints and cockpit reads shared the same
file-backed admission lock, keyed by lake root. Waiting consumes the query's
five-second end-to-end deadline. Thus contention can leave too little time for
a newly admitted child to finish, even though the caller sees
`analytics_deadline_exceeded` rather than `analytics_admission_deadline`.
The regression reproduces this admission coupling with a slow child.
The timestamps alone do not distinguish this from a child that independently
takes five seconds to attach, verify or project. They do establish that the
five-replacement cap leaves recovery stopped rather than escalating.

This is not evidence of a surviving DuckDB connection or executor. `query`
launches and reaps a fresh OS process for each read. `_publish` does the same
for each batch. `export_worker.export` creates its own DuckDB engine, bounded
to 1 GB and one engine thread; the parent imposes a 30-second deadline and
2 GiB RSS limit. Mutation fences use dedicated PostgreSQL connections and are
closed on owner exit. No cached attach or analytics executor is reused here.
State that does survive owner replacement includes the analytics admission
namespace and competing requests, the parent process's allocations and other
workers, and durable lake files, snapshots and frozen batches. A full server
restart resets parent workers and allocations, but not lake files. The logs
show successful checkpoint 4037 at 07:50:26 after the manual restart.

## Limits of the historical evidence

The original 30-second publication failure has no phase or child memory trace.
It cannot be attributed conclusively to catalog lock waits, disk/CPU contention,
memory pressure or batch contents. Analytics cannot block publication through
the Python admission lock because publication does not acquire it. Separate
children can still contend for catalog, storage and host resources. The default
batch is 100 rows, capped at 1,000 and 32 MiB input; actual failing size was not
logged. `_merge_events` compares affected keys, while `refresh_activity_daily`
reaggregates the affected days, so batch row count alone does not bound SQL cost.
`check_proof` scans referenced files even with a current checkpoint.
The reported thousands of seconds of stuck-batch age include the durable
unacknowledged batch remaining after the worker stops; they are not evidence
that a single disposable publication child ran for that long.

The reported 7.5 GB RSS sample at 07:08 is in the issue body but not in the
available stderr. The available v0.6.5 stderr instead records physical footprint
5,030,073,792 bytes at 20:22:37, seconds after startup and hours before failure
(line 976640). Another 4,328,331,192-byte footprint warning occurs at 07:50:44,
after recovery (line 980623). Memory exceeding budget therefore predates the
failure, but these sparse samples do not establish a leak or causation. On
macOS the current guard budgets physical footprint; RSS can retain freed
allocator pages. Parent profiling and a memory time series remain necessary.

The NAS protocol error means a framed response arrived without a registered
pending request (`RelayManager._read_started_response`). It occurs well before
the exporter failure and continues after recovery, for example 07:04:14,
07:07:12 and 07:56:06. Neither the export child nor its restart prerequisites
use this relay. No direct causal link is supported; late-response handling is
a separate investigation, with shared host pressure still a possible common
factor.

## Change and validation

Exporter startup checks, checkpoints and task projections use a separate
single-child admission lane. Both lanes retain their five-second deadlines,
2 GiB RSS limits, full proof validation and disposable connections. At most
two analytical children can now run concurrently, in addition to publication;
this is a deliberate capacity tradeoff and does not eliminate host pressure.

After the recovery cap, or cancellation that cannot unwind within its grace,
the lifecycle logs the escalation and sets the server shutdown event. Existing
server cleanup runs; launchd KeepAlive restarts the exited process. Foreground
operators must start an unmanaged server again. No replacement overlaps a
stuck fence owner. Frozen inputs and committed receipts retain their existing
replay semantics.

Export deadline diagnostics report an allowlisted last phase, input row and
byte counts, and peak child RSS. They contain no event content or credentials.
A simulated daily-rollup timeout verifies diagnostics and child kill/reap.

Tests cover a real slow analytics child holding admission, isolated exporter
startup/checkpoint/projection capacity, fresh-owner recovery, cap escalation,
and stuck-cancellation escalation. Scoped lake and freshness packages are run
against a disposable PostgreSQL cluster and fixture lakes using pinned
extensions. Production contention/lock tracing, parent memory profiling,
compaction and the independent NAS relay repair are deferred.

Changed runtime files: `src/drover/server/lake/query_process.py` (admission),
`serving.py` (exporter check marker), `lifecycle.py` (shutdown escalation and
sanitized deadline detail), `exporter.py` (parent timeout diagnostics), and
`export_worker.py` (child phase reporting). Changed tests:
`tests/test_exporter_freshness.py`, `tests/test_lake_runtime.py`,
`tests/test_lake_exporter.py`, and `tests/test_lake_task_projection.py`.
The runbook is `docs/operations/lake-exporter.md`.

Validation results: the seven scoped packages (`test_exporter_freshness`,
`test_lake_runtime`, `test_lake_exporter`, `test_lake_serving`,
`test_lake_serving_checkpoint`, `test_lake_task_projection`,
`test_lake_restart_switch`) initially returned 139 passed, 1 skipped and 1
failure against an intermediate version that cleared `last_error` after
recovery. That change was removed to preserve the existing diagnostic contract.
The final rerun of freshness, runtime and exporter packages plus the changed
projection test returned 87 passed, 1 skipped, including the formerly failing
receipt-replay case and both subsequently added tests. In total, 142 distinct
scoped cases have passing results for their final behavior and 1 is skipped.
The skip is the opt-in local backup rehearsal, which requires an explicit tar.
Black, isort and `git diff --check` passed. No full suite or live soak was run.
