# Analytical memory: instance budgets and resident memory (#364)

This change delivers the budget correction and resource/observability work in
#364, with a design for the remaining reader split. It does **not** complete
foreground isolation on volumes without atomic clones. It builds on main
`12f3b8b` (#437 admission control) and `c4daa3b` (#435 recovery).

## Budget contract

DuckDB `memory_limit` and `threads` are **instance-wide**, not per connection or
per role. Opening the same file shares its scheduler and buffer manager. Worker,
summarizer and diagnostic therefore use `ANALYTICAL_INSTANCE_DEFAULTS`: **one
1GB budget and one thread**, not three budgets. This is not a process RSS cap:
Python, Arrow/parquet buffers, allocator-retained pages and other instances add
to RSS; DuckDB also has allocations outside its buffer-manager limit.

Use `DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT` and
`DROVER_DUCKDB_ANALYTICAL_THREADS`. Legacy `WORKER`, `SUMMARIZER` and `DIAGNOSTIC`
variables remain accepted as aliases, now applying to **all** shared roles.
All explicitly set aliases must agree (case-insensitive, surrounding whitespace
ignored; use the same unit spelling). Conflicts raise `ValueError` before any
SET, including at the startup analytical open/pin. Defaults do not count as
conflicts. Per-call budget overrides cannot change shared budgets.
`DROVER_DUCKDB_ANALYTICAL_MAX_THREADS` still caps shared parallelism at 1 by
default, as in #331. No memory or thread ceiling was increased.

The `snapshot` profile is only for private files. Its existing independent
`DROVER_DUCKDB_SNAPSHOT_*` settings remain. Do not apply it to the live file.
The control-plane profile and `/harness*` behavior are unchanged: their separate
store remains outside analytical locks, admission and cleanup.

## Lifetime and observation

A closed monitored cursor now drops its parent reference, allowing the parent
connection and registered Arrow buffers to die when their actual owner is done.
The fallback cockpit worker drops its cancellation handle after cleanup. It
still owns the connection and foreground gate until the query finishes; a timed
out request must not close another thread's active connection.

Heavy analytical HTTP requests (including the internal analytics listener) ask
an already-loaded Arrow pool to release **unused** pages at most once a minute.
This does not collect Python garbage, evict live buffers, change the control
store, or promise RSS will immediately drop. Existing request-owned connections
close in their finally blocks and control snapshot contexts detach attachments.
The cockpit's last-good activity and provider caches each hold one response;
this change does not introduce a result cache or snapshot cache.

Set `DROVER_DUCKDB_MEMORY_DIAGNOSTICS=1` and enable INFO logging to emit:

```
analytical_memory pid=... instance=... rss_bytes=... duckdb_memory_bytes=... duckdb_spill_bytes=... phase=before_close
```

Samples come from an owner's idle connection just before close, **never** from
borrowing a worker's active cursor. `duckdb_memory()` reports all attached
catalogs in that instance. RSS is current process RSS from `ps`, not the
lifetime peak. Sampling is limited to once per file per minute; the bookkeeping
is capped at 64 paths. Short-lived snapshot paths may each produce a sample.
Reader-child diagnostics are forwarded to the parent's log with the child's
PID. Do not add RSS values from two records of the same PID; they each describe
the whole process. Samples at different times cannot be summed into an exact
process memory breakdown. Idle pinned instances with no closes are not sampled.
Diagnostics are opt-in because the query and bounded `ps` subprocess have cost;
failures never prevent cleanup. They are observations, not OOM predictions.

## Existing reader isolation and remaining design

Main already runs cockpit **activity** in a one-shot process on volumes that
support atomic `clonefile`. It makes a private database/WAL pair without forcing
a live checkpoint, verifies the pair's signatures, retries capture at most four
times, and removes the scratch directory after child exit (including timeout).
Only one activity reader is admitted per service, and #331's HTTP and maintenance
gates remain held while it runs. The 20-second single-entry response cache
amortizes capture; a miss captures again. A child failure cannot poison the live
instance; a new request gets a fresh instance. Parent/live recovery remains
#363's generation-based recovery, with no statement replay. Provider capacity
and other smaller analytical reads still use live request-owned connections.

The remaining split is deliberately deferred: replacing the non-clone fallback
with `shutil.copy` would copy a mutable DuckDB file non-atomically, and forcing a
checkpoint on each refresh would reintroduce the #363 failure trigger. Copying
all provider and auxiliary readers also requires routing their control-store
lookups using the original source identity, not a scratch filename.

Proposed follow-up:

1. Add a foreground snapshot publisher for platforms without atomic clones.
   Produce a consistent export in a bounded maintenance window under #331's
   gate, with a deadline and disk-space budget. Prove its consistency under
   concurrent writes/checkpoints before enabling it; do not fall back to copying
   a live mutable file or raising memory limits.
2. Keep at most one published generation and one replacement. Readers lease
   the published generation; retire it only after all leases close. Skip refresh
   while retirement would require a third copy. Refresh on the existing 20-second
   activity TTL; serve explicitly stale last-good data if capture fails and use
   retry hints on cold failure. WAL/data freshness and external parquet-file
   retention must participate in the lease contract, since catalog copies alone
   do not freeze parquet globs.
3. Route read-only foreground consumers through this file while retaining the
   source path for control-plane projections and writes. Give the new instance
   an explicit budget; adding an instance increases potential total resident
   memory even though it isolates contention. Keep CPU admission shared.
4. Recover by resolved instance path. A disposable generation should be dropped
   and republished on invalidation, not retried forever after its directory is
   removed. Preserve #363's fail-fast/no-replay semantics and #331's bounded
   admission. Test both directions of invalidation, timeout cleanup, freshness,
   generation lease limits, and auxiliary-reader routing on Linux and macOS.

## Reproduction

`uv sync --extra dev` installs the test environment. Clear **all** inherited
`DROVER_DUCKDB_*` variables before testing; the validation tests set their own.
The full backend suite uses `python -m pytest tests/ -n 2 -q` (the Linux CI
arrangement). macOS CI alternatively runs one module per process to avoid
cross-module recovery timing interference.

The synthetic probe is `scripts/measure_cockpit_memory.py`: 50,000 session/span
pairs, ten uncached cockpit service calls, parent RSS before and after each call.
It uses the test schema, not production data, and does not measure child peaks
or the full HTTP server. Run the same script with baseline and changed sources:

```sh
mkdir -p /tmp/drover-364-baseline
git archive 12f3b8b src | tar -x -C /tmp/drover-364-baseline
PYTHONPATH=/tmp/drover-364-baseline/src .venv/bin/python scripts/measure_cockpit_memory.py
PYTHONPATH="$PWD/src" .venv/bin/python scripts/measure_cockpit_memory.py
```

Use a clean `DROVER_DUCKDB_*` environment for these commands too. The workload
exercises the existing isolated reader when supported; do not attribute that
pre-existing isolation to this change or infer a production RSS saving from a
small synthetic difference.

## Studio validation (2026-09-30)

Synthetic results (one fresh process per revision, DuckDB 1.5.5, Python 3.14.7):

| Revision | Initial parent RSS | After ten requests | Growth |
| --- | ---: | ---: | ---: |
| Base `12f3b8b` | 106.188 MiB | 106.250 MiB | 64 KiB |
| This change | 107.531 MiB | 107.734 MiB | 208 KiB |

Both runs used the already-existing isolated reader. This workload shows **no
RSS reduction** from this change. It excludes child peaks, HTTP cleanup, large
Arrow ingestion and background workers, and cannot reproduce or explain the
reported 3.9GB hub RSS. The close/Arrow lifetime regression tests are the evidence
for resource release; the synthetic run is a reproducible observation, not a
performance claim. Follow up with opt-in memory logs on a representative loaded
server and sample parent plus child RSS concurrently.

Exact test invocations used this environment-clearing wrapper (stdout/stderr
were captured in `/tmp/drover-364-*.log`):

```python
import os
import subprocess

env = {k: v for k, v in os.environ.items()
       if not k.startswith("DROVER_DUCKDB_")}
focused = [
    "tests/test_analytical_memory.py", "tests/test_db.py",
    "tests/test_db_self_heal.py", "tests/test_cockpit_analytics.py",
    "tests/test_analytical_admission_http.py",
    "tests/test_analytical_recovery_http.py",
    "tests/test_control_plane_isolation.py", "tests/test_control_plane_store.py",
]
subprocess.run([".venv/bin/python", "-m", "pytest", "-q", *focused], env=env, check=True)
subprocess.run([".venv/bin/python", "-m", "pytest", "tests/", "-n", "2", "-q"],
               env=env, check=True)
# Added final buffer/scratch-lifetime checks while the full suite ran:
subprocess.run([".venv/bin/python", "-m", "pytest", "-q",
                "tests/test_analytical_memory.py", "tests/test_cockpit_analytics.py",
                "tests/test_analytical_admission_http.py"], env=env, check=True)
```

Full backend result: **4,331 passed, 68 skipped, 9 warnings in 314.61s**
(exit 0). Warnings were deprecations for multithreaded `fork()` and the MCP
`streamable_http_client` rename. The full run collected before the final four
HTTP/buffer/scratch tests were added; the focused runs above cover those additions.

Focused results: **195 passed in 32.78s**. The final three-module resource and
cockpit run: **93 passed in 17.98s**. These cover budget conflict validation,
private-file budget independence, recovery in both directions, closed cursor
ownership and Arrow release, HTTP cleanup in both listener modes, and scratch
removal after successful and timed-out requests.

Formatting/check commands (all passed):

```sh
uv run black --check src/drover/server/db.py src/drover/server/memory.py src/drover/server/cockpit/activity_reader.py src/drover/server/cockpit/service.py src/drover/server/web/app.py tests/test_analytical_memory.py tests/test_analytical_admission_http.py tests/test_db_self_heal.py tests/test_cockpit_analytics.py scripts/measure_cockpit_memory.py
uv run isort --check-only src/drover/server/db.py src/drover/server/memory.py src/drover/server/cockpit/activity_reader.py src/drover/server/cockpit/service.py src/drover/server/web/app.py tests/test_analytical_memory.py tests/test_analytical_admission_http.py tests/test_db_self_heal.py tests/test_cockpit_analytics.py scripts/measure_cockpit_memory.py
git diff --check
```
