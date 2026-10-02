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

## Remaining cutover gates

This is partial serving coverage, not full backend replacement.
Repository-scoped recall bundles, cockpit/day aggregation, remaining activity
paths, PG task-status projections, source-version job scheduling and removal of
legacy analytical bootstrap/writers still need routing and parity proof. Daily
fenced maintenance, immutable paired backups and fresh restore,
exporter-watermark/rollback rehearsal, platform pin/credential installation,
audit/soak and second-machine restore remain gates. Operator approval for
production cutover remains separate.

## Disposable validation checkpoint

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
