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
pin), then reopens and probes the database. The first three attempts follow
delays of 0.25, 0.5, and 1 second. If these fail, state becomes
`failed-retrying` and the same background worker keeps retrying indefinitely,
doubling the delay up to a 60-second cap. Callers cannot start additional
recoverers. The connect lock is released between attempts. A successful probe
restores service and any previous analytical pin. Recovery never closes the
control-plane pin or changes the control-plane database.

Failed statements are not replayed: a write's outcome may be uncertain. Old
handles remain unusable after recovery, and their owners must open a fresh
connection for subsequent work. Runtime workers normally open a connection for
each pass.

`/healthz` remains a liveness endpoint: HTTP 200 with the exact body `ok\n`,
regardless of analytical state. Backup preflight, live-hub smoke, setup, and clients
rely on this contract. The optional `X-Drover-Analytical` response header
reports `ok`, `recovering`, or `failed-retrying` without querying the store.
This header is observed, process-local health: it cannot discover a failure
until a connection operation encounters it.

`/readyz` retains active checks for both stores and reports analytical recovery
as a failure. Its analytical cursor admission never queues on the connect
lock: contention is reported as `busy`, subject to the existing busy grace
window. Ordinary cursor creation remains serialized with recovery so the
recoverer cannot miss a newly created handle. In a split deployment, check the
analytical worker's readiness; the API process does not own that DuckDB instance.

Cockpit and analytical HTTP routes refuse new requests with HTTP 503,
`{"error":"analytical_store_unavailable"}`, and `Retry-After: 1` during recovery,
including slow retries. The request that detects the error also fails; the
next request can succeed after recovery. Authentication still applies.
Already-completed 2xx responses are preserved even if another worker invalidates
the store before the response is sent, so clients are not told to retry a
successful mutation. Non-2xx responses can still be replaced with the explicit
unavailable response.

Recovery does not reduce process RSS (#364). CPU and request admission are
documented in [analytical admission](analytical-admission.md) (#331).
The regression tests inject fatal errors; production confirmation of recovery
from an actual checkpoint OOM remains useful.
