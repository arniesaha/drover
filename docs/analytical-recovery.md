# Analytical store recovery

A fatal DuckDB checkpoint error can make the whole analytical instance invalid
(#363). An ordinary query `OutOfMemoryException` does not.

## How recovery works

1. A connection detects `FatalException` or an error that contains
   `has been invalidated`. This can happen during setup, query, fetch, or close.
2. The first detector logs the original exception at CRITICAL level.
3. The detector marks the affected path as recovering and starts one background recovery.
4. Other callers fail at once. They do not start more recoveries.
5. Recovery closes the analytical handles, including cursors and the optional pin.
6. Recovery reopens the database and probes it.

The reopen uses the same memory limit as live connections. The default is `4GB`.
Set `DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT` to change it. See
[analytical memory](analytical-memory.md#checkpoint-headroom).

The first three attempts wait 0.25, 0.5, and 1 second. If they fail, the state
becomes `failed-retrying`. The same worker then retries without end. It doubles
the delay up to a 60-second cap. The connect lock is free between attempts.

A successful probe restores service and the previous analytical pin. Recovery
never closes the control-plane pin. It never changes the control-plane database.

Drover does not replay failed statements, because the result of a write can be
uncertain. Old handles stay unusable after recovery. Open a new connection for
later work. Runtime workers open a new connection for each pass.

## Check health

Drover has two health endpoints. They have different jobs.

| Endpoint | Meaning | Response |
| --- | --- | --- |
| `/healthz` | Liveness | Always HTTP 200 while the process serves. The body is `ok\nanalytical=<status>\n`. |
| `/readyz` | Readiness | HTTP 503 while analytical recovery runs or retries. The body is JSON with one result for each store. |

In `/healthz`, `<status>` is `ok`, `recovering`, or `failed-retrying`. The
`X-Drover-Analytical` header has the same value.

> **Note:** `/healthz` does not query the store. It shows only the state that the
> process already knows. It cannot find a failure until a connection hits it.

To detect a broken store, use `/readyz`. Do not use `/healthz` for monitoring or
watchers.

`/readyz` checks both stores. It reports analytical recovery as a failure. Its
analytical cursor check never waits for the connect lock. It reports `busy` when
the lock is in use, after the existing busy grace window. Ordinary cursor
creation stays serialized with recovery, so the recoverer cannot miss a new handle.

If you run a split deployment, check `/readyz` on the analytical worker. The API
process does not own that DuckDB instance.

## Requests during recovery

Cockpit and analytical HTTP routes refuse new requests during recovery. This
includes slow retries. They return HTTP 503, `{"error":"analytical_store_unavailable"}`,
and `Retry-After: 1`. Authentication still applies.

The request that detects the error also fails. The next request can succeed
after recovery.

Drover keeps a completed 2xx response, even if another worker invalidates the
store before the response is sent. A client is never told to retry a successful
mutation. Drover can replace a non-2xx response with the unavailable response.

## Limits

- Recovery does not reduce process RSS (#364).
- [Analytical admission](analytical-admission.md) describes CPU and request admission (#331).
- The regression tests inject fatal errors. A production test of recovery from a
  real checkpoint OOM is still useful.
