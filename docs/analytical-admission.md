# Analytical admission and control-plane responsiveness (#331)

The HTTP server uses `ThreadingHTTPServer`: each request gets a thread, and
`/harness*` does not share an executor queue with analytical requests. Nonblocking
semaphores now limit analytical HTTP work and fleet listings independently.
Excess callers receive HTTP 503 with `Retry-After: 1` before rendering, scanning,
or waiting for another render. Authentication runs before admission.

The analytical limit applies to `/analytics`, `/cockpit/overview`, `/insights*`,
`/observability`, and `/metrics`, including mutations and forwarding in an API
process. The internal analytical dispatcher also enforces admission in an
analytics process. Both public and internal HTTP listeners attach a retry hint
to analytical 503 responses, including the recovery responses from #363.

Fleet admission covers `/harness` (including custom archived limits) and
`/harness/hosts`. Both routes also translate `HarnessRenderBusy` to 503 with
`Retry-After: 2`. Existing render coalescing and its two-second follower budget
remain in place. Other control requests, health, and terminal websockets do not
consume analytical or listing slots.

## Configuration

All values below are positive integers, read from the environment:

| Variable | Default | Effect |
| --- | --- | --- |
| `DROVER_DUCKDB_ANALYTICAL_MAX_THREADS` | `1` | Ceiling on live analytical roles, including worker, summarizer, and diagnostic, after role settings and explicit overrides |
| `DROVER_ANALYTICAL_HTTP_CONCURRENCY` | `1` | Concurrent analytical requests per listener/dispatcher |
| `DROVER_FLEET_HTTP_CONCURRENCY` | `4` | Concurrent fleet listing requests per listener |

Existing `DROVER_DUCKDB_<ROLE>_THREADS` settings choose a role's thread count
within the ceiling. Raising the ceiling alone does not raise the one-thread role
defaults. Snapshot readers on private copies retain their existing separately
configurable `DROVER_DUCKDB_SNAPSHOT_THREADS` budget; the control-plane instance
retains `DROVER_DUCKDB_CONTROL_PLANE_THREADS`.

DuckDB `threads` and `memory_limit` remain **instance-wide**. These are not
per-query or per-worker allocations. Role opens may still lower parallelism for
other connections; none of the live analytical roles can raise it above the
shared ceiling. Memory settings are unchanged. This change does not implement
#364's instance split.

## Background work

The existing foreground gate now refuses maintenance for the entire foreground
build, even after many skipped ticks. Harness usage rollup, native usage rollup,
advisory scheduling/sweeps, and the recurring event-day-summary backfill use it.
Analytical HTTP handling registers foreground work too; activity workers retain
their existing gate lifetime if a build outlives its requesting thread.

Only one admitted maintenance pass runs at a time. Skipped ticks return without
opening stores and retry at their normal interval. Already-running maintenance
is not interrupted when a foreground request arrives. Continuous foreground
traffic can delay maintenance indefinitely; deferred-pass counters on workers
and existing gate gauges help distinguish this from a failed pass. This favors
control responsiveness over freshness instead of forcing competing scans after
ten skips.

## Limits and follow-ups

This bounds admitted work, not total process CPU or HTTP connection count. It
does not preempt a stuck leading fleet render, impose a new query deadline, or
isolate Python's GIL. Existing cockpit query deadlines and single-flight behavior
remain responsible for the lifetime of activity builds.

No listing contract changed. `/harness` already returns active sessions plus
20 archived sessions by default, with `?archived=N` capped at 100. Active sessions
and the harness daemon's `/sessions` listing remain unbounded. Complete
pagination/filter semantics remain #224 work.

The iOS `DroverClient.validate` discards response headers when converting 503
into `DroverError.httpStatus`; `SessionStore.pollDelay` uses its ordinary cadence
for failures and a bounded faster cadence for cancellations. It does not honor
`Retry-After`. The web fleet page's `load()` also ignores the header (and lacks a
status check), but has no automatic retry loop. Client changes are deferred.

Regression tests use events to hold an analytical request in flight, verify
both fleet routes respond in under one second, exercise actual rollup deferrals,
and verify immediate analytical/fleet overload responses and slot release.
