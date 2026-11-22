# Lake pressure recovery and prevention

The serving file limit counts current catalog-referenced files, not disk usage.
Code rollback cannot repair a shared lake that exceeds that limit. Never raise
the cap, disable verification, or fall back to legacy analytical data.

## Before a deployment stops a working service

Run `drover-server lake preflight` with the **effective service environment** and
configuration, including launchd EnvironmentVariables as well as its env file.
This invokes the same selected-lake serving verification as startup. A failed
preflight preserves the running service: do not drain, switch or restart to
discover whether the same shared state breaks the replacement. The host updater
also performs this prerequisite before its drain.

## Recovery when the hub cannot start

Use an independent trusted operator connection and a destination-managed one-shot
job. Preserve runtime/config/environment/plists and original service registration.
Exclude competing recovery owners and writers. Use the supported mutation and
reader fences, not a process-name guess. Keep the server stopped during coherent
capture and do not restart a harness with active work.

Capture the complete PostgreSQL lake catalog, the control store, every lake
payload file needed by retained snapshots, and verification proof/checkpoint/
receipts under one frozen boundary. Stream file hashes. Independently restore the
catalog and payload to private isolated storage; verify the scratch PostgreSQL
data directory, and read actual restored table content with typed-row multiset
fingerprints. Archive readability or metadata-only counts are not a restore test.
On macOS, explicitly use a valid locale (`LC_ALL=C`, `LANG=C`) for scratch PG.
Use a configured authorized owner for complete dumps when runtime roles cannot
read historical operator-owned tables; do not broaden application privileges.

Only after that evidence, and explicit authority for its retention effects, run
`drover-server lake compact` with the administrator credential and the fresh
verified backup receipt/acknowledgement. This operation also expires snapshots
and cleans obsolete files: it retains the earlier of the serving baseline and
30-day cutoff, with seven-day old-file cleanup. It attests unchanged row counts
and full typed-row fingerprints and advances the serving checkpoint. Retain its
receipt and prove authenticated, PID-bound old-runtime health before retrying a
candidate update. A restored runtime symlink alone is not recovered health.

## Retuning an already-serving catalog

Raw `provision-exporter` is for explicit initial provisioning and invalidates a
serving proof. For a backed-up serving lake, use the explicit
`lake compact --retune-exporter-role <existing-exporter-group-role>` option.
It holds one exclusive drained writer/reader fence across two separately
unchanged-content-attested, receipted transactions: first apply storage policy
and prime append-only inline mappings as admin, then compact/expire/clean up.
DuckLake forbids mixing row changes and file compaction in one transaction.
Only after both phases succeed does it advance the serving checkpoint and
refresh exporter DML grants, without giving the exporter schema CREATE.
If compaction fails after retuning commits, its completed retune receipt remains
valid; compaction rolls back and readers still validate that receipt (subject to
the unchanged file limit). Retry normal `lake compact` under the same backup and
exclusion safeguards to finish compaction; repeat the explicit retune option if
its final exporter-grant refresh is still required. A crash before a committed
phase's receipt is published remains fail-closed and needs explicit recovery,
not an anonymous snapshot or a proof bypass.
The administrator needs TEMP on this catalog before any retune mutation.
Mutable `agent_events` and `activity_daily` stay at zero inlining; only
append-only outbox, event-version and receipt tables inline batches up to ten
rows. Snapshot retention is 30 days. Test real least-privileged exports after
retuning; do not infer role readiness from options alone.

## Accountable maintenance

Configure a separate maintenance credential with only the necessary catalog
administration privileges, using `analytics.maintenance_dsn_env`. Do not put a
superuser credential in the server environment. Size
`auto_compaction_max_runtime_seconds` from measured production duration and
headroom, not the historical 300-second default.

The exporter owns its mutation fence for its entire lifetime. An empty batch
does not relinquish it. The scheduler first admits maintenance, then asks the
exporter's owner thread to finish its current batch and release its context.
It requires the explicit pause acknowledgement before running the independently
fenced compactor. Deadline/fence refusal never permits concurrent mutation; the
exporter resumes after the maintenance context exits, including failures.

Monitor `/readyz` lake health and
`verification/maintenance-status.json`: current reference count/ratio, configured
state, last attempt/completion, error, skip reason and first sustained deferral.
Completion/deferral timestamps survive restarts. Investigate disabled maintenance,
repeated pause/fence/backlog refusals, errors, or increasing counts despite no
successful completion. Measure batch-size/file-growth rate to estimate headroom;
configured=true alone is not execution evidence. Keep the independently runnable
offline maintenance path available if the hub is unavailable.

## Explicitly degraded control startup

With PostgreSQL control storage, an all-role hub whose selected lake verification
fails starts the existing control/API role in recovery mode instead of exiting.
Authentication, pairing and local control remain reachable. Analytical routes
and cold archived payload resolution use an empty internal boundary token, so
even a stale worker cannot answer them; there is no lake or legacy fallback.
Analytical/export workers and background work admission are not started. Health
reports analytics unavailable with its safe error code. The dedicated analytics
role remains fail-closed. Recovery mode requires a normal restart after storage
repair to restore the full all-role runtime; it does not silently auto-promote.
