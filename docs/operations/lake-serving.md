# Canonical memory backend selection

This slice is code and disposable-infrastructure proof only. Production remains
legacy. It does not authorize a cutover or a service restart.

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
and drains process-local in-flight derived writers under a lock. Retirement
is latched to the entire selected configuration. A change requires explicit
verified renewal; verification failure or an attempted switch back to legacy
never automatically re-enables writers in that process. Each retired writer
entry rechecks authorization before returning without a write. The gate
covers legacy exporter passes, canonical memory projection refresh, native
usage rollup, day-summary backfill and analytical bootstrap entrypoints.
Startup with retirement requested requires existing PG and successful lake
verification before skipping legacy analytical bootstrap and its pinned
connection. The enabled lake exporter checks explicit/registered configuration
agreement at activation and rechecks retirement authorization before each pass;
a failure stops it and releases its dedicated PG fence.

This is a process-local drain, not permission for a running old binary to write
across cutover. Operator fencing of all old writer processes remains mandatory.
Raw/source collectors and ingestion are not retired by this gate. Retiring the
native usage rollup requires a separately proven replacement for native event
publication/usage freshness before production cutover.

## Remaining cutover gates

This is partial serving coverage, not full backend replacement.
Authoritative context publication and proven native publication/usage coverage,
plus the remaining legacy derived/advisory writer audit still need proof.
Fleet routing now uses PostgreSQL registry liveness behind verified selection;
it does not claim coverage of native-only collector sessions. Daily
fenced maintenance, immutable paired backups and fresh restore,
exporter-watermark/rollback rehearsal, platform pin/credential installation,
audit/soak and second-machine restore remain gates. Operator approval for
production cutover remains separate.

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

Selected lake cockpit results extend the established `metadata` block with
`native_publication` and `native_usage`. Both report `freshness=unavailable`:
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
recall bundles now pass through verified selection. The frozen lake and PG
schema have no authoritative `context_containers` publication. Selected lake
responses therefore report `analytics_context_projection_unavailable`, with
verified binding metadata, rather than returning an empty set or reading a
legacy container. Context publication remains a release gate. Legacy selection
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
