# Single observed startup: proposal only

**Decision required:** authorize one diagnostic start in the production service
launch context, after the parent verifies the gates below. The earlier failed
activation and promise not to retry make this a distinct approval decision.
Nothing in this proposal has been executed. The parent owns integration,
artifact verification, approval, rollback and the later continuity canary.

## What it can establish

The accepted offline repair removes unnecessary archive traversal. Canonical
metadata shows no pending input and millisecond archive scans, so it does not
explain the 90-second failure. Empty, preinitialized local PG and tiny analytical
files exclude migrations, pool initialization, actual-volume recovery/checkpoint
work and service permissions. The parent's strict restored PG rehearsal supplies
more realistic control data, but still used fresh analytical storage.

One observed start must record which phase is executing and its blocked Python
stack before the existing 90-second gate expires. It can distinguish module
imports, config reads, analytical pin/open/bootstrap/close, PG admission/schema
work, consent, scratch sweeping, worker construction and HTTP initialization.
If HTTP initialization returns, inspect the listener's `bound` event separately:
a resilient listener can return while retrying an unavailable address. Reaching
the listener constructor alone is not proof that port 7080 bound or continuity
works. This observation is not permission for a further blind rollout.

## Prepared diagnostic capability

`DROVER_STARTUP_STACK_INTERVAL_SECONDS=15` is a transient, default-off setting.
It arms before heavy server imports. faulthandler samples all Python threads
every 15 seconds without exiting or changing retry/statement/rollout deadlines.
A daemon timer cancels sampling after 75 seconds; Click command cleanup and
process exit also cancel it idempotently. Samples at 15, 30, 45 and 60 seconds
precede the existing deadline. The final sample near cancellation is not relied
upon. Normal phase messages include PID, executable and package version; the
early marker includes PID/executable and monotonic time. Diagnostic markers and samples contain no setting value, DSN,
query, row or frame locals. Other existing application/driver logging remains
outside this guarantee; keep the whole capture private and review it before sharing. SIGUSR1 remains available after run starts.

This cannot observe a process that never reaches Python/module code. Capture
process attribution and an external process sample if no early marker appears;
that result localizes the failure before the timer, rather than proving a DB
stall. Package-version metadata also loads before this module. The parent must
keep the diagnostic stderr descriptor open for the observation window.

## Parent gates before approval is acted on

1. Build an isolated instrumented candidate from the accepted branch and record
   its source SHA, wheel hash and runtime artifact manifest. Verify the installed
   candidate owns the timer and phase code. Keep the known-good runtime intact.
   Record the launcher target and resolved readlink before/after activation, and
   match its new PID, executable and timestamps to the early marker. Package
   version alone cannot distinguish two artifacts of the same release.
2. Verify a recent restorable PG backup and a consistent analytical capture with
   explicit DB/WAL provenance and volume/permission metadata. Parent owns any
   capture. Never read/copy an actively changing DuckDB/WAL with ordinary file
   copy. Complete the agreed drain and exclusive-ownership check before the
   candidate opens analytical storage. Do not restart harnessd, Gateway or the
   scheduler; if exclusive ownership cannot be established within that scope,
   abort the gate.
3. Confirm the credential and execution fence before the candidate is permitted
   to open anything. First choice is the strict restored private PG and a guarded
   consistent analytical fixture on the same approved volume, launched under
   the actual service identity. Use an ephemeral private config; preserve the
   canonical config. Verify DSN destination from the private fixture owner,
   not by printing it. No production transcript or credential goes into Git.
   Apply and verify the rehearsal fence: watcher observer/retention/ingestion,
   exporter, push, queues, model/executor and harness execution remain inert;
   external sends are denied. The bind boundary is observed, not deployed as
   a canary endpoint. Validate the fence offline before the one service-context
   invocation. This changes no production queue, push or approval policy.
4. Private copies may not reproduce canonical-path ACLs, WAL state, ownership or
   a live network initialization stall. If those differences defeat the question,
   the parent must return for explicit approval of precisely identified canonical
   access and side effects. Do not silently replace private DSNs with production
   credentials or relax the fence. An unfenced ordinary all-role start is not
   authorized by this proposal: it starts workers before HTTP binding.
5. Use a transient environment override for this single invocation; do not
   persist a service/config change. Capture output in operator-only storage with
   restrictive permissions and bounded disk use. Stacks contain file paths and
   thread identities even without locals. Keep raw evidence private; publish
   only phase names, durations, exception classes and reviewed stack locations.
6. Prepare an external rollback controller before starting. It must restore the
   known-good d52 runtime and canonical arguments/config automatically on the
   existing 90-second limit, candidate exit, attribution mismatch, fence breach
   or completion of the diagnostic boundary. A stuck Python interpreter must
   not own rollback. Stop only the attributed candidate and verify its exit/file
   ownership release before restarting the known-good server. Do not restore
   private fixture data over a live canonical database. Confirm canonical TCP
   and authenticated health after rollback; if they fail, stop and follow the
   parent's already-approved recovery procedure.

## Evidence packet and stop condition

Keep a single timestamped private packet: source/artifact hashes, readlink and
PID attribution, backup/capture verification, fence results, volume metadata,
phase timeline, stack samples and rollback/health outcome. It should identify
the exact blocked call and storage/connection context, or demonstrate that this
fenced service-context startup passes while listing its remaining input
differences. No diagnostic fixture pass proves canonical startup repaired.

The next action after that packet is a focused repair or a specifically scoped
canonical-access approval, chosen from the observed stack. Continuity validation
is a separate later gate on the parent-approved endpoint; it has not been met.
No automatic merge, deployment, retry loop, lease acknowledgement or monitoring
worker is requested.
