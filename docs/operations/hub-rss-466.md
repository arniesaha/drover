# Hub RSS fixture measurement (#466)

This is a local, synthetic measurement only. It never opens `~/.drover`, a
live hub, a mounted production volume, or PostgreSQL. It creates an isolated
DuckDB/Parquet fixture and starts the normal loopback HTTP handler, then polls
`/cockpit/overview` and `/analytics` with changing day filters so the cockpit
response cache cannot hide a query.

```sh
uv sync --extra dev
WORK=$(mktemp -d /tmp/drover-466.XXXXXX)
uv run python scripts/measure_hub_memory.py --prepare "$WORK/fixture" \
  --sessions 2000 --spans 40000 --files 24
DROVER_DUCKDB_MEMORY_DIAGNOSTICS=1 \
DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT=4GB \
  uv run python scripts/measure_hub_memory.py "$WORK/fixture" --minutes 5
```

The report is one JSON object. It contains current parent RSS at startup and
after each request, plus the hub's budget measurement (macOS physical footprint
or Linux RSS), a 10 ms parent/child peak sampler, DuckDB tags from
`duckdb_memory()`, external-file-cache bytes, Arrow-pool bytes, and a
`tracemalloc` before/after diff. macOS also records the kernel lifetime peak
footprint: RSS can retain reclaimable allocator pages and is not the budgeted
value there. With diagnostics enabled, connection close also emits the bounded
`analytical_memory` record. `--minutes 0` is the deterministic smoke mode; it
still performs one request to each endpoint: `/cockpit/overview`, `/analytics`
and `/insights`.

## Fixture result on this branch

On macOS arm64, Python 3.14.7 and DuckDB 1.5.5, using the 2,000-session /
40,000-span / 24-file fixture above and two uncached HTTP requests:

| Path | RSS after views | RSS after requests | Growth |
| --- | ---: | ---: | ---: |
| Forced in-process legacy reader (`--live-reader`) | 94.97 MiB | 182.94 MiB | 87.98 MiB |
| Default disposable reader | 97.69 MiB | 109.51 MiB | 11.83 MiB |

The default child reported 124.1–125.0 MiB RSS at close. Its DuckDB
buffer-manager usage was 0.25 MiB; the parent live instance was also 0.25 MiB
after requests. The 10 ms sampler can miss a short child, so the child's own
supervisor/RSS ceiling—not that sampler—is the enforcement source.

### Five-minute scaled comparison

To test a workload large enough to make retained analytical allocations
visible, the same local-only harness created a fresh 20,000-session /
400,000-span / 100-file fixture and polled every route every 30 seconds for at
least five minutes. It ran with
`DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT=4GB` and diagnostics enabled. The
default completed ten cycles (30 requests); the deliberately forced
non-clone fallback completed nine (27 requests) because its in-process scans
took longer. No production database, hub, home-directory store, or mounted
volume was opened.

| Reader | Parent RSS, views → idle | Hub physical footprint, views → idle |
| --- | ---: | ---: |
| Forced in-process legacy fallback | 99.31 → 653.98 MiB (**+554.67 MiB**) | 57.47 → 79.99 MiB (**+22.52 MiB**) |
| Default disposable reader | 101.11 → 116.70 MiB (**+15.59 MiB**) | 59.27 → 66.31 MiB (**+7.05 MiB**) |

The per-route figures below are the largest single before/after bracket in the
run. They are useful upper observations, not causal totals: routes run
sequentially and a bracket can include allocator activity initiated by earlier
work. The forced fallback makes the owning path clear nevertheless: both
analytical routes use its one in-process DuckDB instance, whereas `/insights`
does not scan the lake.

| Route | Default calls / max RSS step / max footprint step | Forced fallback calls / max RSS step / max footprint step |
| --- | ---: | ---: |
| `/cockpit/overview` | 10 / +3.6 MiB / +3.6 MiB | 9 / +165.0 MiB / +162.3 MiB |
| `/analytics` | 10 / +0.3 MiB / +0.3 MiB | 9 / +128.5 MiB / +128.6 MiB |
| `/insights` | 10 / +1.1 MiB / +1.1 MiB | 9 / +0.2 MiB / +0.2 MiB |

This is the Phase 2 result for the demonstrated growth path: the selected
default already takes the heavy reads out of the hub into the existing
disposable reader. Its hard child contract is five seconds, 2 GiB RSS, 1,000
rows, and 1 MiB response bytes. The test now makes that route selection and
the restored in-process Insights poll a regression contract. There is no
additional safe in-process cap to add to the no-clone fallback: its DuckDB
instance is shared with legacy server work, and lowering its instance-wide
limit would change those callers. The production Studio volume is expected to
use the clone-backed default; #482 must verify that fact and attribute any
remaining growth there.

## What was resident in this workload

| Contributor | Bytes | Evidence |
| --- | ---: | --- |
| Live DuckDB buffer manager | 262,144 | `duckdb_memory()` `BASE_TABLE`; all other nonzero tags absent after requests. |
| DuckDB external-file cache | 0 | `duckdb_external_file_cache()` and `EXTERNAL_FILE_CACHE` tag. |
| Arrow pool | 0 | `pyarrow.total_allocated_bytes()` at every sample. |
| Python growth | <1.1 MiB traced | `tracemalloc` diff; mostly import/JSON bookkeeping. |
| Disposable reader | exits after each request | Child close diagnostics, and the default parent grew 11.83 MiB rather than the forced in-process path's 87.98 MiB. |
| Remaining parent RSS | about 109 MiB total in this fixture | Not represented by DuckDB/Arrow/traced Python; native libraries and allocator-retained pages remain plausible. |

These values are not a production extrapolation and do not claim to explain the
Studio's 6–8 GB RSS. They establish the equivalent legacy fixture property:
the clone-backed disposable reader does not retain its analytical allocation in
the parent. `tests/test_hub_rss_measurement.py` additionally starts the normal
HTTP handler on a fully private, pinned DuckLake fixture and verifies both
polling routes return a bounded disposable child. The `QueryLimits` contract
remains the release bound: one admitted child, at most five seconds, 2 GiB
child RSS, 1,000 rows and 1 MiB result bytes.

## Long-lived-process audit

This audit is source-level, not a claim that a path ran on the Studio. The
scaled harness runs the two HTTP-serving rows; the remaining rows either use
the v2 child already or require external model/catalog configuration and are
left to the production instrumentation below.

| Path | Execution on selected v2 hub | Residency/cap finding |
| --- | --- | --- |
| `/cockpit/overview`, `/analytics` | `read_model` → admitted disposable query child | 5 s, 2 GiB RSS, 1,000-row/1 MiB caps. The legacy APFS reader is also disposable; only the no-clone fallback stays in-process. The scaled probe above exercises both modes. |
| `/insights` | In-process service, control-store repository read | Restored to every harness cycle. Its scope-check cache is 128 entries with 30 s success / 5 s failure TTL; listing insights does not attach the lake. |
| DuckLake exporter | In-process supervisor plus `export_worker` child | Batch payload is capped at 32 MiB; the child is RSS-monitored. It requires a private catalog fixture and is not started by this HTTP probe. |
| Advisory worker | Scheduler/repository in-process; operational facts use isolated snapshots when supported | Snapshot readers are short-lived. The response scope cache is capped at 128 entries. |
| Summary and brief workers | PostgreSQL ledger/memory writes; selected canonical history reads use the lake query child | No embedded model is loaded by the server process for this path. |
| Embedding worker | PostgreSQL ledger/vector writes plus remote Ollama/API/GPU client | The embedding model is external to `drover-server`; no unbounded in-process vector cache was found. |
| Legacy control exporter, day-summary/usage maintenance | Can open the live analytical instance | Not the selected DuckLake serving path; retirement/gates disable legacy derived writes after selected activation. |
| Metrics, readiness, cockpit/provider responses | In-process, bounded single-entry/finite-variant caches | Cockpit holds one activity and one provider response; readiness one verdict; harness rendering has finite variants. |

The harness prints `path_growth_summary` for the three exercised routes. It is
bracketed observation, not causal attribution: requests are sequential and one
route's final sample includes prior routes' work. The individual before/after
samples remain in `path_growth`.

## Scope left to #481 / #482

The fixture cannot attribute allocator-retained native pages in a long-running
Studio process or replace the required 24-hour Studio soak. #481 retains
responsibility for all analytical read paths' disposable-process/admission
contract; #482 owns the real ingest/read soak, bounded hub-memory observation,
and release evidence. For that soak, record at least once per minute and on
every child start/exit: parent RSS, physical footprint and lifetime footprint;
child PID/peak RSS; `duckdb_memory()` tags and spill; Arrow allocation; Python
tracemalloc diff; route/worker name; exporter batch size; and worker/job counts.
Correlate those samples with summary, brief, embed, advisory, exporter and
maintenance start/finish events. A monotonic parent physical-footprint increase
with no active child is the threshold for a new Phase 2 bound; high RSS alone
on macOS is not enough to attribute retained ownership.
