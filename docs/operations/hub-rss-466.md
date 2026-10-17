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
still performs one request to each endpoint.

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

## Scope left to #481 / #482

The fixture cannot attribute allocator-retained native pages in a long-running
Studio process or replace the required 24-hour Studio soak. #481 retains
responsibility for all analytical read paths' disposable-process/admission
contract; #482 owns the real ingest/read soak, bounded hub-memory observation,
and release evidence.
