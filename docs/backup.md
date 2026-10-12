# Backup design

**Implementation: isolated rehearsal tooling is available; production execution remains blocked.** This design
belongs to the memory-integrity program (#476). Pond has been removed from
Drover in Phase 1 (#478); its former backup and restore commands are unavailable.
DuckLake replaced it in the production analytical path on 2026-10-06; the R2
backup workflow described below remains unimplemented.

## Isolated backup rehearsal

`lake backup` accepts only a copied data root carrying a literal
`.drover-isolated-staging-copy` marker containing `DROVER_ISOLATED_STAGING_COPY`.
It never reads hub configuration or a catalog DSN. Supply a separately-created,
non-empty catalog snapshot (for example an operator-produced `pg_dump` from the
isolated catalog) and a fresh backup root. The command verifies parquet row
counts against `verification/report.json`, hashes every copied artifact, and only
then writes the immutable receipt. A generation ID can never be reused.

```sh
# Every path below is a newly created rehearsal path, never a live root.
drover-server lake backup \
  --staging-root /tmp/studio-data-copy \
  --catalog-snapshot /tmp/studio-catalog-copy.dump \
  --backup-root /tmp/lake-backups \
  --generation-id rehearsal-20261003

drover-server lake restore \
  --generation /tmp/lake-backups/rehearsal-20261003 \
  --data-root /tmp/restored-lake \
  --catalog-destination /tmp/restored-catalog.dump
```

Restore validates the receipt-chain entry, exact artifact membership, sizes and
SHA-256 values before writing. It requires both data-root and catalog-destination
to be new, separate paths and recounts parquet rows after restoration. Missing
markers, reports, snapshots, tampered artifacts, existing destinations and row
count mismatches fail closed. This tool is a copy-only restore drill; importing
the restored catalog snapshot into an isolated database is an operator step and
is not a cutover mechanism.

## Backup boundary

A backup will be an immutable generation in R2 containing the **DuckLake
catalog and every data file referenced by that catalog**, plus a **Postgres
dump** of the Drover control store. Copying data files alone is insufficient:
the catalog identifies the snapshot, schemas, and referenced file set. The
generation must record the consistency boundary between the analytical snapshot
and control-store dump, including export/replay watermarks. This coordination
still needs implementation.

R2 will hold backup generations, not serve live recall. Each generation must
use a fresh immutable prefix and must be published as verified only after
all required artifacts have been copied and checked. Partial or failed copies
must never become the latest verified backup. Credentials belong outside
receipts and logs; manifests and diagnostics must remain private.

## Receipts and verification

The receipt/verification principles from the retired backup design carry
forward; its implementation and format are not reused as a working DuckLake
backup. A production backup implementation must provide:

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
No production R2 backup, restore, receipt, or automated retention workflow
described here is available yet.

## Studio NAS snapshots

The operator-authorized machine backup in `scripts/backup/studio_backup.py` is
separate from the R2 rehearsal design above. It runs as `com.drover.backup` at
03:30 local time and uses SSH key authentication to the configured NAS account.
The target is read from private `~/.drover/maintenance/backup-config.json`,
containing `{"nas": "backup-user@nas-host"}` with the actual user and hostname.
Keep this file mode 600.
The NAS requires `python3` and `rsync`. The Studio requires Homebrew GNU rsync
at `/opt/homebrew/bin/rsync`; the built-in openrsync did not apply private
permissions in the local snapshot check. PostgreSQL 17 clients run on the Studio,
using its owner-authenticated Unix socket on port 15432.

Successful generations live at `/volume1/backups/drover/<hostname>/<YYYY-MM-DD>/`.
Unverified generations remain under `.pending-<date>` and never update the local
freshness receipt. An existing generation or pending directory blocks a second
run for the same date. Inspect failed pending directories before removing them
and retrying. A nonblocking local lock prevents concurrent jobs. Every command
failure exits nonzero; logs omit child diagnostics to protect credentials.

Contents:

- `metadata/postgres`: globals, custom-format dumps of every non-template database,
  local archive lists, database filename mapping, and cluster configuration files.
  Each dump is listed locally before transfer, hashed, checked on the NAS, read
  back from the NAS, and listed again with the Studio's `pg_restore`.
- `metadata/configs`: `~/.drover`, including env files, credentials, config,
  maintenance scripts, and historical operational artifacts. Exclusions are
  rebuildable `runtime`, `worktrees`, `rollout-worktrees`, the `incoming` symlink,
  backup scratch state, sockets, and DuckDB files. Symlinks are preserved.
- `metadata/launchagents`: the user's LaunchAgents, including their historical copies.
- `metadata/registry`: a live copy of `drover.registry.duckdb` and its WAL if present.
  Metadata stability is checked and the copy retried up to three times. This is
  **not a transactional DuckDB export**, and consistency is not guaranteed.
- `drover-data` and `drover-lakes`: the two `/Volumes/M2 1/` data roots. Both are
  copied before and after the Postgres dumps. The second pass does not delete
  files retained by the first. Cross-database and catalog-to-file atomicity are
  not guaranteed while writers and garbage collection remain active. This job
  does not prove that every catalog reference can be restored.

Files are mode 600 and directories mode 700. Rsync `--link-dest` shares unchanged
files with the previous successful generation. Do not edit published snapshots
or recursively chmod them, because hard links share inode metadata. All current
source data files and staged metadata are checked for size and SHA-256 on the
NAS. The manifest permits extra data files retained from the first pass.
Published generations include private `MANIFEST.json`, `SUCCESS.json`, and a
consistency warning. Archive listing checks structure, not a complete restore.

Retention preserves the newest seven successful daily generations and the newest
representative from each of the latest four ISO calendar weeks. These sets can
overlap. Other successful date directories are pruned only after a new generation
passes verification. Incomplete generations are retained for investigation.

Install both Python files under `~/.drover/maintenance/`, mode 700. Render
`scripts/launchd/com.drover.backup.plist.template` by replacing `__HOME__` with the
home directory, install it in `~/Library/LaunchAgents/`, validate with `plutil
-lint`, and load using `launchctl bootstrap gui/$(id -u) <plist-path>`. A foreground
manual run is `/opt/homebrew/bin/python3 ~/.drover/maintenance/studio_backup.py`.
Logs live in `~/Library/Logs/drover/backup.log`. The successful local receipt is
`~/.drover/backup-state/last-success.json`; `soak-probe.sh` reports its
`backup_age_h`. Missing evidence is informational before the first success;
once present, evidence older than 36 hours or invalid evidence fails the probe.

WAL/PITR instructions belong in `~/.drover/maintenance/pg-wal-archiving.md`.
They are a deferred maintenance plan, with no cluster restart or WAL changes
performed by this job.
