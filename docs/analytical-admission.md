# Analytical admission and control-plane responsiveness (#331)

The HTTP server uses `ThreadingHTTPServer`: each request gets a thread, and
`/harness*` does not share an executor queue with analytical requests. Heavy
analytical requests retain a single slot by default, since concurrent scans
still compete for instance-wide CPU and memory. Callers wait up to one second
for that slot before receiving HTTP 503 with `Retry-After: 1`. Authentication
runs before admission. Fleet admission remains nonblocking.

`GET /metrics` and `POST /insights/{id}/acknowledge|dismiss|check` are exempt from
the heavy slot. They have separate nonblocking limits of two scrapes and four
mutations, so a cockpit build cannot reject a scrape or user tap, and a flood
of small requests cannot create unbounded concurrent work. The existing cheap
Prometheus cache path and bounded insight check-scope probe remain in place;
these limits bound admission, not every underlying database operation.

The same classification and independent capacity apply in the public handler,
internal worker dispatcher, and API-to-worker transport. Transport reservations
are additional to its configured heavy/archive capacity, preventing a split
installation from reintroducing the shared bottleneck. The heavy wait is one
second at each admission boundary; transport retains its total request deadline.
Both HTTP listeners attach retry hints to analytical 503 responses, including
recovery responses from #363. Liveness and successful-response preservation
from #363 are unchanged.

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
| `DROVER_ANALYTICAL_HTTP_CONCURRENCY` | `1` | Concurrent heavy analytical requests per listener/dispatcher |
| `DROVER_FLEET_HTTP_CONCURRENCY` | `4` | Concurrent fleet listing requests per listener |

`DROVER_DUCKDB_ANALYTICAL_THREADS` chooses the shared analytical thread count
within the ceiling. Raising the ceiling alone does not raise its one-thread
default. Legacy `WORKER`, `SUMMARIZER`, and `DIAGNOSTIC` settings are aliases for
that same count and must agree. Snapshot readers on private copies retain
`DROVER_DUCKDB_SNAPSHOT_THREADS`; the control-plane instance retains
`DROVER_DUCKDB_CONTROL_PLANE_THREADS`.

DuckDB `threads` and `memory_limit` are **instance-wide**, not per-query or
per-worker allocations. See [analytical memory](analytical-memory.md) for #364's
shared-budget validation, RSS diagnostics, resource cleanup, and the remaining
foreground-instance split design. Admission still bounds CPU contention across
instances and processes.

## Background work

The existing foreground gate now refuses maintenance for the entire foreground
build, even after many skipped ticks. Native usage rollup,
advisory scheduling/sweeps, and the recurring event-day-summary backfill use it.
Heavy analytical HTTP handling registers foreground work too; activity workers retain
their existing gate lifetime if a build outlives its requesting thread.

Only one admitted maintenance pass runs at a time. Skipped ticks return without
opening stores and retry at their normal interval. Day-summary backfill retries
skipped passes every 30 seconds instead of waiting its full 900-second refresh
interval; once admitted it returns to that normal cadence. Harness usage rollup
only touches the control store and transcript payloads, so it runs independently
of the analytical gate. Already-running maintenance
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
and verify bounded analytical waits, immediate fleet overload responses, and
slot release. Both all-in-one and split-runtime tests cover scrape/mutation
exemptions, their separate capacity limits, and heavy-slot handoff.
