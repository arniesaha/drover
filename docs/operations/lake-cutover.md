# DuckLake cutover and rollback

**Status: restart-selected routing is implemented; production execution still
requires separate operator approval.** `analytics.backend = "legacy"` remains
the default. `analytics.backend = "ducklake"` selects verified DuckLake reads
and its exporter at process startup. Invalid DuckLake configuration fails startup
without falling back. Roll back by restoring `legacy` and restarting; do not
modify legacy files.

## Restart cutover checklist

1. Stop the hub and all workers.
2. Create and retain a tar backup of the legacy root.
3. Rebuild a new DuckLake data root and fresh catalog from that tar, including exporter provisioning.
4. Verify rebuild counts, hashes, and serving proof.
5. Set `analytics.backend = "ducklake"` and its catalog DSN environment reference.
6. Start the hub and workers.
7. Check `/healthz`, summaries, recall, and the event count against verification.
8. To roll back, set `analytics.backend = "legacy"` and restart.

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
2.5 GiB by default. For an offline `lake rebuild` only, operators may set
`DROVER_LAKE_REBUILD_RSS_CEILING_BYTES` to a whole-byte value from 512 MiB through
12 GiB; it does not change standalone `lake verify` or server limits. The rebuild
report records both `rss_ceiling_bytes` and peak RSS. On a supervisor or worker
failure, the retained root also contains `admin-supervision-failure.json` with the
cap, peak, last coordinator/child sample, and failure attribution. Verification uses one day per process. A keys-only external sort checks
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

## Deferred safeguards

Persisted configuration epochs, CAS publication, live handover, replay protocol,
and zero-downtime rollback are explicitly out of scope for this restart-only
mechanism and are tracked in #509. Do not infer any of them from the startup
switch. Retain the old catalog/data and the legacy implementation until the
separate operational gates and rollback rehearsal are approved.
