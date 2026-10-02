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
is capped at 64 hashed identities and total samples at 64 per process per minute.
Instance identifiers are truncated SHA-256 hashes; paths and diagnostic exception
payloads are not logged. Short-lived snapshot paths may each produce a sample.
Reader-child diagnostics are forwarded to the parent's log with the child's
PID. Forwarding accepts only bounded numeric memory records, excluding unrelated
child stderr and malformed records. Do not add RSS values from two records of the same PID; they each describe
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

## RSS investigation (#466, 2026-10-01)

The reference hub's **6.6 GB one minute after restart / 8.3 GB before a hang**
are operator-reported observations, not measurements from this checkout. Its
explicit **6 GB analytical budget** is a buffer-manager ceiling, not evidence
that 6 GB was allocated. We have no reference-hub memory samples or access to
its data in this investigation. The cause of that hang remains unproven.

DuckDB 1.5.5 enables an in-memory external file cache by default. Drover now
sets `enable_external_file_cache=false` for live analytical and private snapshot
connections. This avoids retaining local Parquet contents in DuckDB in addition
to the OS file cache. It neither changes query results nor reduces the explicit
6 GB budget. The separate control store's configuration is unchanged.
[DuckDB configuration reference](https://duckdb.org/docs/stable/configuration/overview)
describes this setting. No cache opt-in or new instance is introduced. The
existing 1 GB analytical default stays: previous 512 MB cockpit OOM evidence
is stronger than this synthetic workload as justification for its floor.

The #438 diagnostics now also report nonzero DuckDB tags, Arrow pool bytes and
Python traced bytes (or `None` when tracing is off). They do not import Arrow,
start tracemalloc, collect garbage, or touch another owner's cursor. They remain
opt-in and close-only, now with an additional process-wide limit
of 64 samples per minute even when private snapshot paths churn. They still
exclude idle pins that never close. Python tracing and the Arrow pool do not
cover all native allocations, and neither should be subtracted from RSS to
claim an exact residual breakdown.

### Historical synthetic measurements (stopped commit `b4a3819`)

`scripts/measure_hub_memory.py` uses 50,000 sessions and 1,000,000 spans in
4,000 Parquet files (~32 MiB on disk), with persistent views over those files.
Data generation happens in a separate process. A fresh measurement process
opens and holds the live instance (like a runtime pin), rebinds the views,
then makes three uncached cockpit overview and three insights list service
calls. Insights findings are empty; this verifies the bounded control-store
read path, not a large content-analysis workload. The schema comes from the
analytics test fixture. The recovered probe explicitly sets `spans_enabled=True`
because current main defaults span telemetry off; it models the optional span
workload, not the default deployment. Its Python allocation summaries contain
only byte/count totals, without source paths. **Startup here means open/view
binding, not the full server CLI bootstrap**: background ingestion, provider refresh, summarization,
production JSON payloads and startup rollups are excluded.

psutil samples parent and child RSS every 10 ms. tracemalloc starts after module
imports, so its numbers attribute incremental workload Python allocations;
they exclude import-time Python memory and do not trace the reader subprocess.
The Arrow default pool count covers live pool buffers, not allocator retention.
Child DuckDB close samples come from #438 diagnostics. Peaks are sampled lower
bounds. Parent and child peaks are independent and must not be added as an
exact simultaneous process-tree peak.

The stopped work recorded the following **synthetic** results, MiB
(1 MiB = 1,048,576 bytes), baseline
`4e1b434`, macOS arm64 / Python 3.14.7 / DuckDB 1.5.5 / Arrow 24.0.0. Both
revisions used the hub's **6 GB live analytical budget**, one live thread and
the existing default 1 GB private-reader budget:

| Measurement | Before | After |
| --- | ---: | ---: |
| Clone-supported startup parent RSS | 209.91 | 172.06 |
| Clone-supported parent RSS after requests | 219.98 | 181.62 |
| Reader child sampled peak RSS | 936.11 | 526.67 |
| Reader DuckDB memory at close | 360.03 | 0.25 |
| Live fallback startup parent RSS | 212.98 | 170.25 |
| Live fallback parent RSS after requests | 1282.27 | 824.75 |
| Live fallback DuckDB memory after requests | 360.03 | 0.25 |
| Live fallback parent RSS after final close | 1266.31 | 824.75 |

The cache accounts for **359.78 MiB** of retained DuckDB memory after the live
scan. At view binding it accounts for **31.25 MiB**, plus ~0.41 MiB of object
cache and 0.25 MiB of base-table pages. The cache's logical `nr_bytes` was only
5.92 MiB at binding; use the memory tag to see allocated storage, not just
logical file bytes. After disabling it the sampled live DuckDB memory is
0.25 MiB. Incremental traced Python stayed below 0.8 MiB after requests
(<1 MiB traced peak); live Arrow pool usage was zero at every sample.
The remaining **~825 MiB fallback RSS is not explained by buffer-manager,
traced Python or live Arrow-pool bytes**. Native query/Parquet allocations,
imported libraries and allocator-retained pages are plausible contributors;
this probe cannot distinguish their exact shares. Closing the instance did
not return most pages to the OS. This is evidence against claiming connection
closure alone fixes RSS.

Cockpit times were 2.21–2.28 s before / 2.08–2.09 s after with isolated readers,
and 2.59–2.87 s before / 2.54–2.81 s after with the live fallback. These are
three warm-local-disk calls, not a latency guarantee. Disabling the cache can
increase repeated I/O and CPU on cold or remote storage. Startup RSS decreased
~38–43 MiB, reader peak ~409 MiB, and live retained RSS ~458 MiB in this probe;
none is a claimed production saving. Raw samples and close diagnostics are in
[measurements/drover-466.json](measurements/drover-466.json).

To run the recovered probe on current sources (this does not reproduce the
historical before/after numbers), run from the repository with no inherited `DROVER_DUCKDB_*` values. The
following runner explicitly clears them, then sets only the two probe settings:

```sh
uv sync --extra dev
# Pick a new, absent workload path; preparation refuses an existing directory.
```

```python
import os
import subprocess

env = {k: v for k, v in os.environ.items()
       if not k.startswith("DROVER_DUCKDB_")}
python = ".venv/bin/python"
probe = "scripts/measure_hub_memory.py"
root = "/tmp/drover-466-new-workload"
subprocess.run([python, probe, "--prepare", root], env=env, check=True)
env.update(DROVER_DUCKDB_MEMORY_DIAGNOSTICS="1",
           DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT="6GB")
for mode in [[], ["--live-reader"]]:
    with open(f"/tmp/466-current-{'live' if mode else 'clone'}.json", "w") as out:
        subprocess.run([python, probe, root, *mode],
                       env={**env, "PYTHONPATH": os.path.abspath("src")},
                       stdout=out, check=True)
```

### Scope and remaining risks

In the historical synthetic run on a clone-capable volume, the ~527 MiB reader
exited after each uncached request, while the parent retained ~182 MiB.
The fallback retained much more native memory. Extending #364's split
on non-clone volumes still requires the consistent publisher/lease design
above; copying a mutable file or checkpointing on each foreground refresh
would risk #363 recovery semantics. This change does not enable either.

Inspection found request closes, snapshot detaches, bounded insights scope
cache, single-response cockpit caches and throttled Arrow release already on
main. Their invariants stay intact. Lazy view binding alone would defer rather
than remove the measured scan/cache cost and could move it into the first
foreground request, so no new lazy-attachment behavior is added. `/healthz`,
#331 admission gates, and #363 generation-based recovery/no-replay are unchanged.
For the hub follow-up, collect per-PID diagnostics through startup and uncached
requests, alongside process-tree RSS; include every live instance and avoid
summing repeated whole-process RSS records. A native allocation profiler is
needed to attribute the untracked residual before attempting allocator-specific
reclamation or a new cross-platform snapshot mechanism.

### Historical validation for #466 (`b4a3819`)

Both test invocations ran in the foreground via `subprocess.run(...,
check=True)`, with every inherited `DROVER_DUCKDB_*` key removed from the child
environment before launch (including the diagnostics and 6 GB probe settings):

```sh
.venv/bin/python -m pytest -q tests/test_analytical_memory.py tests/test_db.py tests/test_db_self_heal.py tests/test_cockpit_analytics.py tests/test_analytical_admission_http.py tests/test_analytical_recovery_http.py tests/test_control_plane_isolation.py tests/test_control_plane_store.py
.venv/bin/python -m pytest tests/ -n 2 -q
```

Focused: **201 passed in 35.89 s**. Full backend: **4,419 passed, 69 skipped,
9 warnings in 342.74 s**, exit 0. Warnings concern multithreaded `fork()` and
the MCP `streamable_http_client` rename. The new four-role regression verifies
view binding and repeated Parquet scans return the expected aggregate and leave
zero `EXTERNAL_FILE_CACHE` bytes, including with a second handle to the same
instance. Existing admission tests cover `/healthz` under analytical pressure;
recovery tests cover generation invalidation and no statement replay.

Black, isort and `git diff --check` passed. The probe also completed a separate
7-session / 13-span / 3-file-per-view smoke run to check preparation with
non-divisible row counts and all eleven sampling phases.


### Recovery review on current main (`97849f2`, 2026-10-01)

Recovered from `b4a3819` in a new isolated worktree; the stopped worktree was
read only and remains at its original clean commit. In addition to the cache
setting and allocation counters, recovery redacts instance paths, suppresses
exception payloads in memory-sample failures, caps process-wide diagnostic
samples, and forwards only bounded numeric memory records from reader stderr.
The probe explicitly enables the optional span workload and emits only numeric
Python allocation summaries. The raw large-run artifact above is historical,
with source-commit provenance; no new production or large-workload memory
improvement is claimed.

Foreground Studio validation (macOS arm64, Python 3.14.7, DuckDB 1.5.5):
**209 passed in 34.61 s**, using the eight-module focused command above with
all inherited `DROVER_DUCKDB_*` values removed and `PYTHONPATH` pointing to this
worktree's `src`. This covers cache semantics for all four analytical roles,
unchanged control-plane cache settings, admission/recovery behavior, opt-in
sampling, throttling under path churn, failure redaction, loaded allocation
pools, and filtering malformed/unrelated reader stderr.

Probe smoke commands used a new workload under this worktree's `.venv`, with
that same clean environment and current `PYTHONPATH`:

```sh
.venv/bin/python scripts/measure_hub_memory.py --prepare .venv/probe-466-fixture --sessions 7 --spans 13 --files 3
# Both measurements added only MEMORY_DIAGNOSTICS=1 and ANALYTICAL_MEMORY_LIMIT=6GB
# under the DROVER_DUCKDB_ prefix:
.venv/bin/python scripts/measure_hub_memory.py .venv/probe-466-fixture
.venv/bin/python scripts/measure_hub_memory.py .venv/probe-466-fixture --live-reader
```

Both modes passed: eleven phases, optional spans enabled, zero external-cache
logical bytes and memory-tag bytes at every sample, numeric-only Python top
summaries, and no workload path in diagnostic stderr. The default mode used an
isolated reader on this Studio volume; the forced fallback did not. Black,
isort, lock consistency and `git diff --check` passed. The full backend suite
and large historical comparison were **not rerun** during recovery. This is
ready for PR review as a targeted cache/observability change; #466's reported
6–8 GB production residency and hang remain unproven and unresolved.
