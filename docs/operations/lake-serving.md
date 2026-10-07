# Canonical memory backend selection

Production selected this backend on 2026-10-06 with v0.6.2. This document
describes its serving contract; it does not by itself authorize another cutover
or service restart.

`[analytics] backend = "legacy"` is the default and remains authoritative even
when lake paths or credentials are present. `exporter_enabled = false` is the
lake default. There is no backend discovery from files or environment variables.
An explicit DuckLake selection requires an absolute `data_root`,
`extension_dir`, independently pinned `engine_sha256`, reader `catalog_dsn_env`,
a nonempty configuration `epoch`, and `verification_sha256`. Export activation
additionally requires `exporter_enabled = true` and a separate
`exporter_dsn_env`. DSNs are resolved from environment variables, never
serialized into query requests. These are configuration *keys*, not instructions
to change operator settings.

## Verification authorization

Successful offline `lake verify` now holds the mutation fence for the entire
verification, rechecks ownership and the initially captured snapshot, and writes
`verification/serving-proof.json` only after all table hashes match the rebuilt
baseline. It rejects unexpected day partitions as well as count/hash mismatches.
The proof binds the absolute root, catalog main-schema UUID, verified snapshot,
rebuild report SHA-256, and verified hashes/counts for all four retained tables.
Its SHA-256 must be explicitly pinned in the selected configuration; startup
never enrolls a hash. Older checkpoints lacking this proof are unavailable until
separately authorized offline verification succeeds.

Every serving SELECT checks the pinned proof in the same child transaction as
its read. Dry-run, absent, mismatched, wrong-catalog and unverified snapshots
are rejected. Post-baseline snapshots are accepted only if tagged
`drover-export` with the immutable batch receipt's SHA-256; the matching receipt
must be present. A crash after lake commit but before PG acknowledgement does
not hide committed canonical events or create a second snapshot on recovery.
Snapshot checks inspect metadata; product code never uses time travel.

Every read checks presence and catalog-recorded size of all current referenced
data and delete files, bounded to 10,000 files, even outside its queried
partition. File listings come from the [catalog metadata
API](https://ducklake.select/docs/stable/duckdb/metadata/list_files). Retained
files are assumed immutable: full content hashes are checked offline, not
recomputed on every request. Corrupt files encountered by the engine produce
unavailable responses. Orphans are never discovered by globs. This gate does not
substitute for immutable backup verification or a storage-integrity monitor.
Maintenance/provisioning changes are deliberately not certified as exports and
invalidate serving authorization.

## Routed reads and safe failure

MCP canonical session resolution/replay/summary lookup, history search,
files-touched and recall use the selected backend. Canonical summarizer inputs
use the same cursor facade. Unscoped recall bundles use selected canonical
search; repository-scoped bundles fail explicitly with
`analytics_recall_context_not_ported` until their legacy
context-container/activity components are ported. PG remains authoritative for
session identity, summary artifacts and keyword/pgvector recall. Repository
attribution retains the legacy guarded JSON fallback for missing physical
columns. Repository recall resolves lake session/task scopes without querying
legacy task/day aggregate tables. Linked native copies are suppressed using PG
identities; exported and frozen control rows preserve their control source.
Archived legacy metadata is excluded.

Each analytical execution uses the existing disposable process: one admitted
query at a time, five seconds including admission, engine 2GB/two threads,
monitored RSS <=2 GiB, default 1,000 rows/1 MiB result. Timestamps represent the
same instants; lake serialization is UTC, legacy may use the host timezone.
Naive/date-only search bounds carry an explicit host-local offset into the lake
child to preserve legacy midnight/DST interpretation; UTC day pruning retains
its existing rule. Lake errors return `status=unavailable`,
`analytics_backend=ducklake`, the selected `analytics_epoch`, and an explicit
reason, without retrying legacy history. `reason` preserves
bounded-query/verification error codes. PG's existing semantic-to-keyword recall
fallback remains available when the lake gate succeeds. Summarizer failures
follow the existing retry ledger and do not invoke a model on unavailable input.

The PG identity snapshot is capped at 10,000 rows and the query request is
capped at 256 KiB. Receipt-chain checking is capped at 10,000 post-baseline
snapshots; exceeding these caps fails closed. Referenced-file coverage is capped
at 10,000 files and shares the child deadline/RSS limits. Incremental
verification/checkpoint renewal for longer-running lakes remains a release gate,
not an implicit bypass.

## Exporter lifecycle

Server startup selects exactly one exporter implementation. Legacy selection
keeps the existing exporter; DuckLake selection with export disabled starts
neither exporter. Enabled lake export owns its dedicated PG fence on a single
owner thread, checks that reader/exporter credentials address the same catalog
and verifies serving authorization from its explicit configuration before any
outbox claim, then runs bounded synchronous batches. Startup does no catalog
provisioning or migration. A lost fence, bad receipt, verification failure or
batch failure stops that lifecycle, releases ownership, records degraded health,
and never activates the legacy exporter. Explicit shutdown waits for the bounded
batch to finish and release its connection. Retry requires a new explicit
lifecycle instance.

## Activity routing and opt-in derived-writer retirement

Cockpit activity/day aggregation and HTTP/MCP project activity now execute the
shared activity algorithms in the verified disposable child. They read only
canonical lake events and a bounded repeatable-read snapshot of PG serving
projections; no cached legacy day summaries or span tables are attached. The
PG snapshot is capped at 10,000 rows per relation. Final model output shares
query row/byte/RSS/deadline limits. Cockpit checks selection before its legacy
cache and never returns stale legacy results when lake authorization fails.
Repository recent summaries and project-brief freshness also use selected
canonical history. Source-version reads for explicit session-close jobs,
requeue, and post-ingest scheduling use the selector. A lake error defers
post-ingest summary scheduling without changing source ingestion; legacy lock
errors retain their existing retry behavior.

Selected lake task status now reads completed PostgreSQL task generations.
The legacy task table is never consulted by selected lake requests. Missing,
stale, corrupt or incomplete generations return explicit unavailable rather
than a full-history scan or aggregate fallback. See the task-generation
procedure below.

`retire_legacy_writers = false` is independently defaulted. Only explicit
DuckLake opt-in can request retirement. Activation verifies the selected lake
and drains in-flight derived writers in all participating local processes. The
local selection lock is retained for thread draining and nested calls; an
exclusive OS `flock` spans the entire derived call, including its legacy reads.
Retirement is durably latched to the entire selected configuration. A change
requires explicit verified renewal; verification failure or an attempted switch
back to legacy never automatically re-enables writers in any participating
process. Each retired writer
entry rechecks authorization before returning without a write. The gate
covers legacy exporter passes, canonical memory projection refresh, native
usage rollup, day-summary backfill and analytical bootstrap entrypoints.
Startup with retirement requested requires existing PG and successful lake
verification before skipping legacy analytical bootstrap and its pinned
connection. The enabled lake exporter checks explicit/registered configuration
agreement at activation and rechecks retirement authorization before each pass;
a failure stops it and releases its dedicated PG fence.

This fence requires cooperating binaries and one shared, resolved store-path
namespace on a host with working `flock` and `fsync`. A running old binary or a
direct writer bypassing these entrypoints cannot be fenced by this code.
Operator fencing of all old/unconverted writer processes remains mandatory.
Raw/source collectors and ingestion are not retired by this gate. Retiring the
native usage rollup requires operational proof of native event publication and
periodic usage certification before the production cutover.

## Pre-cutover gates recorded at this checkpoint

At this historical checkpoint, this was partial serving coverage rather than a
full backend replacement. The production cutover later completed on 2026-10-06.
Authoritative source producer integration and periodic context/native certification
in production, plus the remaining legacy derived/advisory writer audit, still
need operational proof. The bounded staging certification below proves the
serving contract against independent fixture inputs.
Fleet routing now uses PostgreSQL registry liveness behind verified selection;
it does not claim coverage of native-only collector sessions. Daily
fenced maintenance, immutable paired backups and fresh restore,
exporter-watermark/rollback rehearsal, platform pin/credential installation,
audit/soak and second-machine restore were recorded as gates requiring separate
operator approval.

## Prior disposable validation checkpoint

Foreground scoped validation: **174 passed, 2 skipped in 196.64 seconds**.
This includes **18 serving/selection/lifecycle tests** plus exporter, runtime,
configuration, MCP, summarizer and recall regressions. Tests used the existing
private initdb harness and fixture lakes with `DROVER_TEST_POSTGRES_DSN` removed
from the invocation environment. The only skips were the explicitly opt-in full
backup rehearsal and a vector-unavailable scenario unreachable because pgvector
is installed. No live data, configuration, services or catalogs were accessed.

Black, isort and `git diff --check` passed. Independent read-only review found
verification fence-loss and repository-normalization issues; both were fixed
and re-reviewed without remaining critical or important findings.


## Activity/writer-gate validation checkpoint

Final foreground routing/serving validation: **31 passed in 213.06 seconds**,
including **13 focused activity/writer-gate cases**. These cover shared-repository
canonical activity parity, legacy selection, invalid/unverified lake rejection,
HTTP unavailable responses, no cached legacy retry, selected job source reads,
actual retired writer entrypoints, in-flight drain, failed verification, epoch
renewal, old-exporter shutdown and cursor reload errors. The preceding scoped
regression runs passed **220 tests in 227.69 seconds** and **258 tests with one
optional vector scenario skipped in 288.10 seconds**. Counts overlap; they are
not additive. Black, isort and `git diff --check` passed. Read-only review found
no remaining critical or important issues in this implemented scope.

All lake/PG integration used fixture roots and private throwaway PostgreSQL,
with `DROVER_TEST_POSTGRES_DSN` removed. No production configuration, data root,
catalog, hub, restart, migration, cutover or backup deletion was involved.


## PostgreSQL task generations

`lake.task_projection.provision_task_projections(path)` is an explicit staging
provisioning operation against an existing PG control store and selected,
verified DuckLake catalog. It creates only three PG projection relations and
one lookup index; no event data is migrated. It is never invoked automatically
by server startup or serving requests. Production provisioning/cutover remains
an operator-approved operation, not something performed in this session.

`refresh_task_projections(path)` certifies all canonical task facts and session
associations in bounded disposable children, using the existing two-thread/2GB
engine,
2 GiB RSS ceiling, five-second deadline and 1 MiB output cap per child.
Task and session projections use separate deterministic keyset pages of at most
500 rows, so there is no combined generation row cap. Each page is checked
against the same snapshot/epoch/proof/identity binding. Limit breaches fail
explicitly; caps are not bypassed
for larger catalogs. The build excludes archived metadata and legacy spans and
uses the same normalized canonical event view and PG identity mapping as other
selected reads. It derives distinct session/agent counts, first/last event times
and deterministic repository/branch/principal attribution. Session-to-task
association chooses the latest event with deterministic ID/task tie-breaking.

A dedicated PG advisory fence serializes projection builders. The catalog
mutation fence prevents an export from crossing a build; an exporter refresh
reuses and checks its existing dedicated owner fence. Publication checks the
catalog and identity bindings again, verifies both fences, and writes bounded
PG row batches plus a version-2 digest receipt in one transaction, receipt last.
The receipt records separate task/session counts and SHA-256 digests of ordered
key/payload-hash or session/task pairs. No whole-generation key map is retained.
Deferred foreign keys prevent rows without their receipt from surviving. Failed publication
rolls back all new rows. Generations are retained; none are deleted or replaced.

The receipt binds configuration epoch, pinned verification proof/root, catalog
snapshot and the complete identity-map hash. Task status verifies catalog
coverage in disposable metadata queries before and after its PG read, then
checks generation counts, every PG task payload SHA-256 and the complete session
mapping in ordered, bounded pages within a repeatable-read transaction.
PG projection text is byte-bounded before
fetching. Task status reads no historical events and never refreshes itself.
Ambiguous identity aliases are unavailable. Unknown task IDs in a complete
certified generation remain `status=unknown`. Latest task summaries come from
the existing authoritative PG memory repository; a PG summary-read error returns
`analytics_task_summary_unavailable`, without legacy fallback.

Available task facts report `status=observed`, `status_source=canonical_events`.
This is canonical task membership, not inferred fleet liveness; live/closed
state is not invented from old event timestamps. `total_cost_usd=null` with
`cost_coverage=unavailable` makes the absence of a canonical cost source explicit.
No legacy span costs or native usage/fleet adapters were added. Canonical counts,
attribution, timestamp instants and summary fields have focused legacy parity
coverage; legacy mutable status/cost metadata is deliberately not imported.

When these tables have been explicitly provisioned, the opt-in lake exporter
refreshes before its first claim and after acknowledged/exported batches,
including batches with zero new winning events. A refresh failure stops that
lifecycle and releases its fence; the already committed export/ack remains
durable, while stale task generations are unavailable. A new authorized
lifecycle refresh repairs them on startup. Absent tables preserve existing
exporter behavior with task status unavailable. Legacy-default selection still
uses the unchanged legacy task read path and creates no projection tables.


Task-projection validation checkpoint: final foreground **10 passed in 119.46
seconds**, including the final UUID/byte-bound and summary-error checks. The
preceding scoped task/selector/activity/lifecycle/MCP run passed **67 tests in
316.26 seconds**. These counts overlap. Black, isort and `git diff --check`
passed. Integration used only private PG fixtures and fixture lakes with
`DROVER_TEST_POSTGRES_DSN` removed. No live schema, config, store, service,
cutover, backup deletion or migration was touched. The initial combined
projection cap is now replaced by bounded pages; production-scale latency/soak
remains a cutover gate.


### Native freshness metadata

At the paged-projection checkpoint, selected lake cockpit results extended the
established `metadata` block with `native_publication` and `native_usage`. Without
the explicit certification described below, both report `freshness=unavailable`:
verification of a frozen catalog does not establish ongoing native publication
or rollup coverage. Usage includes diagnostic PostgreSQL `observed_at` (latest
rollup clock) and `source_activity_at`; even a recent legacy rollup does not
certify lake coverage (`reason=lake_coverage_unverified`). Publication reports
`reason=native_publication_not_proven`. Existing event-observation freshness
retains its meaning. This adds metadata only, with no native writers or
context/fleet adapters. Version-1 task receipts require an explicitly authorized
refresh before they can serve; they are never silently accepted or migrated.

Paged-projection validation (2026-10-02): foreground scoped task, activity and
selector tests passed **45 tests in 378.79 seconds** using only disposable PG
and fixture lakes. Coverage includes publication of 1,001 tasks plus 1,001
session mappings, SQL keyset enumeration of 1,202 keys (including an empty key),
terminal empty pages, near-limit page bytes, atomic rollback, changing identity
bindings between pages, and malformed newest receipts without older-generation
fallback. Black, isort and whitespace checks passed. No production state was
changed. Real native publication/usage coverage, context/fleet adapters,
production-scale lifecycle latency/soak and the remaining cutover gates above
still require separate work.


## Verified context/fleet adapters

Context container tools (recent, brief, open loops, resume) and repository-scoped
recall bundles pass through verified selection. Without explicitly provisioned
and certified authoritative contexts, selected lake responses report
`analytics_context_projection_unavailable`, with verified binding metadata,
rather than returning an empty set or reading a legacy container. The staging
source and receipt protocol below now enables these reads. Legacy selection
retains the existing context data and linked-summary behavior.

Selected fleet status uses PostgreSQL registry `running`/`awaiting` sessions,
excluding ended sessions and retired hosts. It reports
`status_source=postgres_registry`; historical event activity never creates a
live session. Shared repo/agent/time fields have fixture parity where registry
and legacy observations agree. Unproven task membership, event counts and user
snippets are null, with `event_coverage=unavailable`. The response is bounded to
1,000 sessions and the existing disposable child byte/RSS/deadline limits; a
breach returns unavailable, never a partial fleet or legacy fallback.

Read-model results certify the selected snapshot, epoch, verification proof,
data root and PG identity hash before and after computation. The PG copy and
freshness diagnostics share a repeatable-read snapshot and must match that
identity hash. A binding change rejects the result. Metadata adds `binding`;
native publication/usage add `coverage_binding` and `generation=null` to make
missing coverage receipts explicit. Recent or stale legacy usage clocks are
never promoted to `fresh`; their diagnostic timestamps do not establish a lake
publication/usage generation. Verification loss is unavailable and cannot
reuse a cached fresh result. No context/native publisher, configuration change,
production state operation or capacity/maintenance/cutover work is included.

Adapter validation (2026-10-03): foreground scoped adapter, activity, selector,
legacy context, recall-bundle and MCP regressions passed **81 tests in 334.84
seconds**. Final identity-tag/proof-loss assertions passed **2 focused tests in
32.10 seconds** (overlapping that scope). All integration used disposable PG
and fixture lakes with the ambient test DSN removed. Black, isort and whitespace
checks passed. No live configuration, stores, services or operational cutover
state were changed.


## Authoritative coverage certification (staging API)

`drover.server.lake.coverage` exposes explicit producer/operator APIs;
serving never provisions tables, copies legacy contexts, publishes a source,
or renews a receipt. These APIs require an already selected, verified lake and
PostgreSQL control store. They are not public MCP mutation endpoints.

1. Explicitly call `provision_coverage(path)` on disposable infrastructure to
   create the control-store source revision and generation receipt tables.
2. A trusted producer supplies a **complete**, independently obtained revision
   with `publish_source(path, kind, payload, publisher=..., watermark=...,
   observed_at=...)`. `kind="contexts"` takes the full authoritative context
   list. `kind="native"` takes an inventory containing `version=1`, `rows`,
   and `sha256`. Watermarks are publisher-local labels, not global cursors.
   Observation timestamps must include a timezone.
3. `certify(path, kind)` validates the revision and publishes an immutable
   completion receipt last in a PostgreSQL transaction. It holds the projection
   advisory fence and catalog mutation fence, rechecks both ownership and the
   snapshot/epoch/verification/data-root/identity binding, and rejects changed
   source heads. Failed certification leaves no usable generation.

The native inventory must come from the producer's independent canonical
inputs, after the repository's winner ordering and null-key/archive policy.
`native_inventory(con, relation)` defines its version-1 digest: explicitly cast
all `EVENT_SCHEMA` fields, normalize timestamps to UTC, SHA-256 each typed row,
sort those leaf hashes, and SHA-256 their newline-delimited sequence. Include
all canonical native rows, including identity-linked mirrors, and exclude
control/outbox rows and archived legacy metadata. Do not hash the serving lake
to declare its own expected inventory. Certification compares that independent
count and digest with the retained lake's native inventory in an admitted
query child. A mismatch cannot yield a receipt or retain a fresh older result.

Usage certification derives nullable typed token counters from the canonical
identity-normalized lake view. Identity-linked native mirrors are suppressed;
negative counters or missing session identity on usage rows reject the proof.
Per-session counts reconcile to the number of eligible usage events. Unknown
counters remain null. The certificate scope is
`publication_scope=canonical_native_agent_events` and
`usage_basis=typed_canonical_native_events`; it does not certify provider quota
snapshots, billed costs, or an upstream inventory the trusted producer omitted.
Legacy PostgreSQL native rollups are never substituted for certified usage;
PostgreSQL control-event usage remains authoritative.

Serving reads sources and receipts together in a repeatable-read snapshot and
accepts only the newest receipt for the newest source revision. Its binding must
match the selected lake snapshot, config epoch, verification proof, data root,
and PostgreSQL identity hash. Both source observation and receipt certification
must be no more than 300 seconds old and cannot be in the future. Missing,
stale, malformed, oversized, or incomplete proof is explicitly unavailable;
there is no older-generation or legacy fallback. Source/receipt head tokens and
bindings are checked again after computation, including composite context and
linked-summary reads.

The established metadata exposes `generation`, `coverage_binding`, `publisher`,
`source_revision`, `watermark`, `observed_at`, `certified_at`, and
`freshness_basis=registered_source_revision`. Certified native metadata also
reports covered events, usage events and usage sessions. Fresh means coverage
of that registered authoritative revision under that exact binding; a recent
legacy rollup clock is only diagnostic. Successful selected recall bundles
carry the metadata too. Context briefs retain their existing row shape;
recent/open-loop results carry context metadata, and a certified empty source
can correctly return an empty list or a missing brief.

Full context revisions and native usage generations are bounded to 1,000 rows
and 1 MiB; response byte limits apply to composite resume/bundle payloads too.
Limits reject rather than truncate proof. Native certification runs inside the
existing disposable query admission, 2 GiB RSS, 2 GB DuckDB memory, two-thread,
five-second and output limits. No production-scale throughput claim follows
from these small fixtures.

Production producer authority/credentials, independent complete inventory
collection, periodic renewal and publication transport remain integration and
operator gates. This slice does not activate a producer, modify configuration,
retire additional writers, or perform capacity/soak, maintenance, backup/restore,
cutover or rollback. Legacy remains the default backend.


Certification validation (2026-10-03): foreground scoped coverage, context/fleet,
activity, selector, legacy context, recall-bundle and MCP regressions passed
**88 tests in 439.37 seconds**. After adding successful-bundle provenance, the
affected coverage/selector/recall-bundle scope passed **34 tests in 289.91
seconds**. Final stricter source-shape and exact native usage boundary checks
passed with the full coverage module: **13 tests in 128.87 seconds**. These
runs overlap; they are not additive test counts. All integration used disposable
PostgreSQL and fixture lakes with `DROVER_TEST_POSTGRES_DSN` removed. Tests cover
context and typed-usage parity, independent inventory mismatch, source-head
renewal during a read, epoch/identity invalidation, stale/future clocks,
corrupt newest receipts, receipt rollback/fence loss, source/receipt/composite
byte caps and the 1,000/1,001 usage-row boundary. Black, isort and whitespace
checks passed. No live configuration, stores, services or operational state
were changed.


## Durable legacy-derived writer fence

Every gated writer and retirement activation uses the stable inode at
`<resolved-store-path>.legacy-derived-gate`. Gate entry creates its parent when
needed for legacy bootstrap, without creating the store itself. Empty state
means no activation has been requested, so legacy-default writes proceed.
All processes lock the inode exclusively for the complete derived write; calls
nested in the same process reuse the held descriptor. The inode is never
renamed, replaced, or unlinked by the runtime.

Activation drains competing writes, writes and fsyncs a `pending` latch **before**
checking the selected lake, then writes/fsyncs `active` only after successful
verification and unchanged selection. The record contains a version and a
SHA-256 token of the full analytics configuration, including backend, epoch,
verification digest and catalog/root selection. A failed activation leaves a
closed pending latch. Process exit releases the OS lock but retains retirement.
Writes happen before truncation, so a partial update leaves a nonempty closed
record rather than an empty legacy authorization; invalid or oversized records
fail closed and are never silently repaired.

`configure_analytics` invalidates an existing activation under the same fence
before publishing a changed local selection. Other processes with the previous
configuration also see the pending latch and cannot reuse the old activation.
Changing back to the old configuration does not restore it. Explicit verified
`activate_retirement` renewal is required. An active entry with matching config
rechecks verification before returning the existing retired no-op result;
unconfigured/stale workers receive a renewal error instead of entering a write.
There is no automatic unretirement, rollback, latch deletion or recovery reset.
Do not remove or replace the sidecar during operation.

Audited callers:

| Caller | Gated namespace and scope |
| --- | --- |
| `schema.bootstrap` | `duckdb_path`; entire directory/catalog/control setup |
| `schema.backfill_agent_event_day_summary` | `duckdb_path`; all day reads/writes |
| `memory_identity.refresh_memory_projection` | Explicit `store_path`; whole projection and job derivation |
| `native_usage_rollup.rollup_pending_native_usage` | `duckdb_path`; source reads, totals and control-store transaction |
| `ControlOutboxExporter.run_once` | Both control and analytical paths, resolved/deduplicated and acquired in sorted order; full export pass, including nested projection |
| Server bootstrap/day-summary wrappers and lake lifecycle authorization | Existing outer gates remain reentrant with the decorated calls |

Runtime projection calls provide the explicit control/store path. The optional
`store_path=None` standalone projection API has no namespace and retains its
existing ungated behavior; it is not retirement authorization for a named store.
Raw/source ingestion is unchanged. This audit does not retire additional direct,
advisory, curated-state or source writers.

The cross-process tests exec a fresh interpreter with an independent selection
registry and PostgreSQL pool. Legacy bootstrap actually writes private DuckDB
and Parquet files. A child pauses after opening its real legacy connection,
inside the decorated call; parent activation cannot complete until that child
finishes. The already-running legacy-config child and a newly exec'd default
child then fail to enter derived writes. A matching selected child receives
verified retired results from bootstrap, day summaries, memory projection and
usage rollup. Epoch/selection changes invalidate even a still-running child's
prior activation; missing, corrupt or subsequently lost fixture verification
and partial latch records stay closed. All stores and catalog namespaces are
throwaway fixtures; no live configuration, producer namespace or service is
involved.

Writer-fence validation (2026-10-03): foreground
`tests/test_lake_writer_gate.py tests/test_lake_activity_routing.py` passed
**21 tests in 138.85 seconds**. Foreground selector/exporter/native-rollup
regressions passed **24 tests in 163.03 seconds**, with the two legacy exporter
DSN-only tests initially skipped. Those two were rerun against a cluster created
and destroyed by `tests/conftest.py::postgres_dsn.__wrapped__`, supplying only
that owned cluster's DSN to `pytest.main(['-q', '-x',
 'tests/test_control_exporter.py'])`: **2 passed in 0.83 seconds**. The initial
commands removed the ambient `DROVER_TEST_POSTGRES_DSN` and used pinned extension
artifacts from `/tmp/drover-phase4-artifacts/osx_arm64`. Black, isort and whitespace
checks passed. No production configuration, stores, services, actual producer
namespace, credentials or operational lifecycle were changed.
