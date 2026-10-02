# Backup design

**Implementation: Phase 4 (#481), planned and not implemented.** This design
belongs to the memory-integrity program (#476). Pond has been removed from
Drover in Phase 1 (#478); its former backup and restore commands are unavailable.
DuckLake replaces it in Phase 4.

## Backup boundary

A backup will be an immutable generation in R2 containing the **DuckLake
catalog and every data file referenced by that catalog**, plus a **Postgres
dump** of the Drover control store. Copying data files alone is insufficient:
the catalog identifies the snapshot, schemas, and referenced file set. The
generation must record the consistency boundary between the analytical snapshot
and control-store dump, including export/replay watermarks. This coordination
still needs implementation in Phase 4.

R2 will hold backup generations, not serve live recall. Each generation must
use a fresh immutable prefix and must be published as verified only after
all required artifacts have been copied and checked. Partial or failed copies
must never become the latest verified backup. Credentials belong outside
receipts and logs; manifests and diagnostics must remain private.

## Receipts and verification

The receipt/verification principles from the retired backup design carry
forward; its implementation and format are not reused as a working DuckLake
backup. Phase 4 must implement:

- A versioned receipt binding generation ID, parent receipt hash, creation time,
  catalog snapshot/version, Postgres dump format/version, consistency boundary,
  and manifest digest. Include artifact sizes and cryptographic checksums,
  table/row counts, and the verification results.
- A complete manifest of catalog, dump, and referenced data files. Verify
  exact membership, sizes, checksums, and that catalog references resolve
  within the generation; reject missing or unexpected artifacts.
- Fail-closed preflight and post-copy checks. Capture a consistent snapshot,
  detect changes during copying, bound memory and subprocess resources, retain
  private diagnostics, and publish a success receipt only after verification.
- Receipt-chain validation and explicit inspection of one verified generation.
  A receipt must describe observed verification, never substitute for it.

## Restore drills and retention

Restore drills must use a fresh isolated catalog/files location and a separate
Postgres database. Validate the receipt chain and artifact hashes before loading,
then verify catalog readability, referenced files, schemas, row counts, control
store readiness, and recall results against the recorded snapshot. Record drill
results in a separate verification receipt. Do not overwrite or cut over live
stores as part of a drill.

Retention and deletion require a separately implemented policy that respects
catalog references and verified generation dependencies. Incomplete generations
need explicit cleanup, and operators must review R2 lifecycle and cost settings.
No backup, restore, receipt, or automated retention workflow described here is
available yet; all are Phase 4 (#481) work.
