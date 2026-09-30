# Analytical store recovery

A fatal DuckDB checkpoint error can invalidate the entire analytical instance
(#363). An ordinary query `OutOfMemoryException` does not invalidate it.

Analytical connections detect `FatalException` and errors containing
`has been invalidated`, including errors during connection setup, queries,
fetches, and explicit close. The first detector logs the original exception at
CRITICAL level. It marks the affected path as recovering and starts one
background recovery. Concurrent callers fail immediately; they do not start
additional recoveries.

Recovery closes the analytical handles (including cursors and the optional
pin), then reopens and probes the database. It makes at most three attempts,
with delays of 0.25, 0.5, and 1 second. A successful probe restores service and
any previous analytical pin. Exhaustion leaves the store in `failed` state and
requires operator intervention. Recovery never closes the control-plane pin or
changes the control-plane database.

Failed statements are not replayed: a write's outcome may be uncertain. Old
handles remain unusable after recovery, and their owners must open a fresh
connection for subsequent work. Runtime workers normally open a connection for
each pass.

`/healthz` now returns JSON with separate `process` and `analytical_store`
fields, for example:

```json
{"process":"ok","analytical_store":{"status":"recovering","recovery_attempts":1}}
```

It returns HTTP 503 while analytical recovery is running or has failed, and
200 otherwise. This is observed, process-local analytical health, not a query
probe: it cannot discover a failure until a connection operation encounters
it. `/readyz` retains the active checks for both stores and also respects the
recovery state. In a split deployment, check the analytical worker's health;
the API process does not own that DuckDB instance.

Cockpit and analytical HTTP routes return HTTP 503 with
`{"error":"analytical_store_unavailable"}` and `Retry-After: 1` during recovery
or after recovery exhaustion. The request that detects the error also fails;
the next request can succeed after recovery. Authentication still applies.

Recovery does not reduce process RSS (#364) or address CPU starvation (#331).
The regression tests inject fatal errors; production confirmation of recovery
from an actual checkpoint OOM remains useful.
