# Hub physical-footprint fixture measurement (#466)

This is a local, synthetic measurement only. It never opens `~/.drover`, a
live hub, a mounted production volume, or PostgreSQL. It creates an isolated
DuckDB/Parquet fixture and starts the normal loopback HTTP handler, then polls
both `/analytics` rankings for 7, 30, 90, and 365 days, followed by
`/cockpit/overview?days=365` (including popular projects) and `/insights`.
The distinct query keys prevent a cockpit response cache hit from hiding an
analytical query.

```sh
uv sync --extra dev
WORK=$(mktemp -d /tmp/drover-466.XXXXXX)
uv run python scripts/measure_hub_memory.py --prepare "$WORK/fixture" \
  --sessions 2000 --spans 40000 --files 24
DROVER_DUCKDB_MEMORY_DIAGNOSTICS=1 \
DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT=4GB \
  uv run python scripts/measure_hub_memory.py "$WORK/fixture" --minutes 5 --interval-seconds 30
uv run python scripts/measure_hub_memory.py "$WORK/fixture" --startup-view-peak
```

The report is one JSON object. Its primary budget metric is current physical
footprint on macOS and RSS on Linux. It records that metric and its lifetime
kernel high-water mark before and after every app route, plus a 10 ms
parent/child sampler, DuckDB tags from
`duckdb_memory()`, external-file-cache bytes, Arrow-pool bytes, and a
`tracemalloc` before/after diff. macOS RSS can retain freed allocator pages
(`Malloc Small (empty)`) and therefore over-reports resident ownership; physical
footprint is the budgeted value there. With diagnostics enabled, connection close also emits the bounded
`analytical_memory` record. `--minutes 0` is the deterministic smoke mode; it
still performs the complete ranking matrix, overview, and insights. Its
`peak_hub_memory` must remain below the 1.5 GiB release budget.

The same run also sends a header-only, oversized `/harness/events` replay.
The hub must return 413 before reading the declared body; this protects the
long-lived process from simultaneously materialising a replay's raw bytes,
decoded text, and JSON graph. Normal event batches and their responses are
unchanged. The ingress limit is 16 MiB, well above the pusher's normal
100-event reconciliation page.

## Studio root-cause evidence and fix

Read-only Studio logs from the 0.6.17 restart identify a startup peak rather
than steady-state ownership. The parent was 221.5 MiB physical footprint when
the listener completed at 11:55:19, the DuckLake proof checkpoint ran at
11:55:20, and `analytical Parquet views ready` logged 1.83 GiB at 11:55:26.
At 11:55:52 the memory guard sampled 5.13 GiB; `vmmap --summary` later showed
378 MiB current footprint, 5.7 GiB lifetime peak, and 6.8 GiB of reclaimable
`Malloc Small (empty)` RSS. The only named log events between the proof and
the guard are the legacy view bind; subsequent unnamed startup work includes a
best-effort cockpit warm-up, which can resolve those views. No exporter merge,
embedding, recap, or client insights request is logged in that interval.

The cause is the legacy asynchronous Parquet bootstrap running even when
DuckLake is selected but legacy-writer retirement is not enabled. It creates
broad `read_parquet(..., union_by_name=true)` views and
`_refresh_span_partition_activity()` scans every historical span in the hub
process. DuckLake serves reads through its disposable reader and exports through
its own fenced child, so it does not need those views. This branch now skips
that legacy view bind whenever `analytics.backend == "ducklake"`; the bounded
catalog/control bootstrap still runs before listeners bind.

## Fixture result on this branch

All figures below are from a fresh local 20,000-session / 400,000-span /
100-file fixture on macOS arm64 (Python 3.14.7, DuckDB 1.5.5), with
`DROVER_DUCKDB_MEMORY_DIAGNOSTICS=1` and
`DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT=4GB`. No production database, hub,
home-directory store, or mounted volume was opened.

### Startup peak: before and after the DuckLake selection fix

The probe runs each mode in a new process, so the kernel lifetime high-water
mark is comparable. The *before* mode calls the old legacy view bind; the
*after* mode is the DuckLake-selected startup, which does not call it.

| Startup mode | Footprint before → after | Lifetime peak |
| --- | ---: | ---: |
| Before: legacy Parquet view bind / full span-activity refresh | 50.53 → 73.92 MiB (**+23.39 MiB**) | 73.92 MiB |
| After: DuckLake-selected bounded startup | 50.59 → 63.80 MiB (**+13.20 MiB**) | 63.80 MiB |

The fixture is deliberately much smaller than Studio, so it does not claim to
recreate the 5.7 GiB production high-water mark. It does execute the same broad
view bind: selecting DuckLake lowers this fixture's startup high-water mark by
10.12 MiB. The log sequence above attributes the production-scale source; the
regression test prevents a DuckLake-selected server from calling that bind again.

### Baseline app-route comparison

The historical figures below are a three-route baseline: each process polled
`/cockpit/overview`, `/analytics`, and `/insights` every 30 seconds for at
least five minutes. The current harness is stricter: it runs both analytics
rankings for every production day window, then the 365-day overview and
insights. The baseline default completed ten cycles (30 requests). A second
process deliberately disabled clone support (`--live-reader`) to exercise the
in-hub fallback; because its scans take longer it completed nine cycles (27
requests).

| Reader | Parent footprint, views → idle | Lifetime footprint peak | Parent RSS at idle |
| --- | ---: | ---: | ---: |
| Selected disposable reader | 59.78 → 67.08 MiB | **73.11 MiB** | 117.91 MiB |
| Forced in-hub fallback | 57.16 → 76.91 MiB | **305.19 MiB** | 634.06 MiB |

The table reports the largest before/after physical-footprint bracket for a
single request. Lifetime steps retain a short spike; route calls are sequential,
so they are upper observations rather than additive causal totals.

| Route | Disposable reader: calls / max current step / max lifetime step | Forced fallback: calls / max current step / max lifetime step |
| --- | ---: | ---: |
| `/cockpit/overview` (popular projects) | 10 / +3.44 MiB / +4.06 MiB | 9 / +171.11 MiB / +171.73 MiB |
| `/analytics` (distribution) | 10 / +0.31 MiB / +0.00 MiB | 9 / +65.66 MiB / +65.09 MiB |
| `/insights` (advisory) | 10 / +0.48 MiB / +0.75 MiB | 9 / +0.25 MiB / +0.73 MiB |

The selected path has no endpoint-sized footprint spike on this fixture. The
fallback can transiently reach 305.19 MiB: overview and analytics materialize
against its long-lived instance; insights does not scan the lake. The selected
DuckLake implementation already routes the two heavy reads through the
existing disposable reader, whose hard contract is five seconds, 2 GiB RSS,
1,000 rows, and a 1 MiB response. This change fixes the independent startup
peak source rather than weakening that fallback's shared legacy instance. #482
must verify clone support and attribute any remaining Studio peak.

## What was resident in this workload

| Contributor | Bytes | Evidence |
| --- | ---: | --- |
| Live DuckDB buffer manager | 262,144 | `duckdb_memory()` `BASE_TABLE`; all other nonzero tags absent after requests. |
| DuckDB external-file cache | 0 | `duckdb_external_file_cache()` and `EXTERNAL_FILE_CACHE` tag. |
| Arrow pool | 0 | `pyarrow.total_allocated_bytes()` at every sample. |
| Python growth | <1.1 MiB traced | `tracemalloc` diff; mostly import/JSON bookkeeping. |
| Disposable reader | exits after each request | The selected parent reached 73.11 MiB lifetime footprint; the forced fallback reached 305.19 MiB. |
| Remaining parent memory | 67.08 MiB footprint / 117.91 MiB RSS at idle | Not represented by DuckDB/Arrow/traced Python; native libraries and allocator-retained pages remain plausible. |

These values are not a production extrapolation and do not claim to explain the
Studio's 6–8 GB RSS. They establish the equivalent legacy fixture property:
the clone-backed disposable reader does not retain its analytical allocation in
the parent. `tests/test_hub_rss_measurement.py` additionally starts the normal
HTTP handler on a fully private, pinned DuckLake fixture and verifies every
configured app poll, including insights. Its two heavy route families use
bounded disposable children. The `QueryLimits` contract
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

The harness prints `path_growth_summary` for the three exercised route
families. It is
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
