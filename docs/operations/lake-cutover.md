# DuckLake cutover and rollback

**Status: runbook drafted; production execution is blocked.** The current branch
contains pinned runtime/process primitives and offline rebuild/verify tooling.
Serving routing/exporter lifecycle activation, maintenance,
backup/restore and config epochs must be implemented and proven before these
steps can be executed. Production cutover requires separate operator approval.

This is a rebuild, with an analytical outage allowed, for one operator. No shadow
exporter or zero-downtime mechanism is required. Keep durable PostgreSQL ingress
available during the fence; it buffers events accepted after the final watermark.
Do not migrate spans, legacy analytical job tables, or optional Pond data.

## Rehearse offline

Use a freshly created catalog database and a new data root. Never supply a control
store DSN as the catalog DSN. The commands do not load the hub configuration.
Provision separate catalog-admin, exporter and reader credentials first; only the
admin initializes the catalog. Installer-verified artifacts and engine digest
must be supplied explicitly:

```sh
export DROVER_LAKE_EXTENSION_DIR=/path/to/verified/extensions
export DROVER_LAKE_ENGINE_SHA256=<installer-verified-engine-digest>
# Set SCRATCH_CATALOG_DSN using the operator's secret manager.
drover-server lake rebuild --from-tar /path/to/frozen.tar \
  --data-root /tmp/new-lake --catalog-dsn-env SCRATCH_CATALOG_DSN --dry-run
```

A dry run creates source inventory, normalized hashes, dedupe accounting and
lineage under the new root; it never contacts PostgreSQL. Use a different new
root to perform an actual rebuild (omit `--dry-run`), then:

```sh
drover-server lake verify --data-root /tmp/published-lake \
  --catalog-dsn-env SCRATCH_CATALOG_DSN
```

Rebuild preserves the explicit historical event schema, including
VARCHAR timestamps, before adding file/row lineage and normalized SHA-256.
Provider snapshots and outbox exports use explicit schemas and remain
unpartitioned. Null keys with content are backfilled with the repository fingerprint
and `dedup_key_source=rebuild_backfill`. Null keys with empty content and a null
role enter `agent_events_legacy_metadata`, retained for verification and excluded
from the serving events table. Remaining empty-content null-key rows keep
`legacy_null` lineage. Non-null keys use repository
attribution/timestamp/source-ID ordering, then normalized payload hash and stable
file/ordinal tie-breaks. Verification recomputes payload hashes rather than
trusting the stored per-row digests, and checks the evidence manifest.

Each day is staged, deduplicated and published in a disposable process. Workers
use a 1GB engine limit, one thread, and a spill directory on the new data volume.
The supervisor samples aggregate coordinator/child RSS every 20ms and stops above
2.5 GiB. Verification uses one day per process. A keys-only external sort checks
original and backfilled identities across partitions before catalog creation; a
cross-day duplicate fails explicitly instead of silently choosing two winners.

Output: `verification/report.json`, source file SHA-256 inventory, raw/canonical
counts per UTC day/session, and winner/loser file/ordinal/hash mappings. Retain
these and the frozen input until audit sign-off. Dry-run accounting alone is not
a backup generation, restore proof or cutover gate.

The supplied October 1 tar actually contains 5,288,617 raw events, 5,350 above
the spike baseline. The same 77,378 excess rows collapse to 5,211,239 canonical
events under original-key ordering. The 357,236 null-key rows are accounted for
under the decided backfill/archive policy; the report separates original-key
baseline counts from serving, archive and additional losers. A baseline mismatch must be investigated
and recorded, never forced to match by deleting rows.

## Production gates

Before scheduling the separately approved cutover, require all of the following:

- Every release condition in #481, including least-privilege roles, installer
  pins, exporter crash/replay/connection-loss tests, query parity and hard caps.
- A monitored bounded rebuild plus independently verified catalog/files backup
  (including delete files), two retained verified generations and fresh restore.
- Fixed 25-session memory audit, 24-hour ingest/read/maintenance soak,
  second-machine restore, installer proof and rollback rehearsal.
- Explicit accounting for cutover-deferred incoming files, duplicate parse
  variants, and unarchived payloads. Export acknowledgement is not archive coverage.

## Fence → watermark → epoch → one exporter

1. Disable retention/pruning. Fence exporters, derived workers and maintenance.
   Drain in-flight lake reads/transactions and PG job leases. Acquire the catalog
   mutation fence and exclusive reader/lifecycle fence. Never fence by merely
   closing a pooled connection.
2. Freeze input and take the final committed export watermark **H**, preserving
   source offsets, batch membership, immutable lake receipt/hash coverage and
   configuration epoch together. Verify that all events accepted through H are
   accounted for. Events accepted after H stay in the durable PG outbox.
3. Rebuild the new catalog/data root; verify counts, hashes, winners/losers and
   null lineage against the final frozen source. Rebuild derived PG memory from
   canonical events; keep the authoritative control state. Capture the paired
   immutable backup generation and verify it independently before publishing
   the final receipt.
4. Atomically select `analytics.backend = "ducklake"` and advance the read/state
   configuration epoch, identifying the new catalog/root and watermark H.
   This configuration flip is **not implemented at this checkpoint**. Do not
   use it on the current branch or assume an unknown option changes serving.
5. Release the lifecycle fence and resume **one** authoritative exporter/worker
   set. Confirm exclusive exporter ownership, receipt-first recovery and
   acknowledgement only after lake commit. Drain post-H ingress. Verify fleet
   and liveness from PG, summary freshness, and capped recall identity/watermark.
6. Keep the old immutable data/catalog and legacy read implementation for at
   least seven days and two verified backups. Leave payload pruning disabled
   until archive coverage, deferred-file accounting and operator sign-off pass.

## Rollback

Fence and drain the new exporter/workers/readers before changing epochs. Preserve
the new catalog/files, receipts and derived-state deltas as failure evidence.
Switch the read epoch back to the retained legacy implementation. Replay durable
post-H ingress into the legacy path and verify counts/hashes before resuming
workers. Preserve PG control state and accepted events; regenerate derived memory
from canonical events or apply its versioned deltas. Swapping in an old mutable
DuckDB file cannot recover PG jobs/summaries accepted after H.

Verify both events accepted during the fence and events accepted after the first
resume in the rollback rehearsal. Do not resume retention until two verified
paired generations and complete per-event archive coverage are established.
