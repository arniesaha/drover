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
containing `{"nas": "backup-user@nas-host", "target_root": "/srv/backups/drover"}`
with the actual SSH account and approved NAS destination supplied locally.
The required `target_root` sets the absolute NAS directory above the host folder.
It is independent of the SSH login account because UGOS storage homes can differ
from login names. There is no built-in personal destination. Missing roots,
relative roots and roots containing `..` are rejected before contacting the NAS.
Existing configs containing only `nas` must add their approved `target_root`
before running the updated script. Keep this file mode 600.
The NAS requires `python3` and `rsync`, plus a `drover-studio` SSH alias in its
`~/.ssh/config` with unattended key access back to the Studio. The Studio must
allow incoming SSH for this account. Transfers run on the NAS, pulling from the
Studio: UGOS-patched rsync 3.4.1 rejects home-directory paths with `invalid path`
when used as the receiving server. Local staging still uses Studio rsync.
The Studio requires Homebrew GNU rsync
at `/opt/homebrew/bin/rsync`; the built-in openrsync did not apply private
permissions in the local snapshot check. PostgreSQL 17 clients run on the Studio,
using its owner-authenticated Unix socket on port 15432.

Successful generations live at
`<target_root>/<hostname>/<YYYY-MM-DD>/`. The hostname is sanitized for a path
component. Use the approved NAS home-directory target in the private config.
Unverified generations never update the local freshness receipt. A failed run
renames `.pending-<date>` to `.failed-<date>-<time>-<nanoseconds>` and retains its
evidence. Preparation also preserves leftover pending trees from interrupted
runs, allowing a retry on the same date. A successful date still blocks reuse.
A nonblocking local lock prevents concurrent jobs. Every command
failure exits nonzero; the FAILURE line includes the failed command's exit code,
while logs omit child diagnostics to protect credentials. Each run starts
`caffeinate -i -w <pid>` to prevent idle sleep until the backup process exits;
the Studio's idle sleep setting is one minute.

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
- `metadata/registry`: a standalone checkpointed snapshot of the home registry.
- `metadata/duckdb/<data-root>/<relative-path>`: standalone checkpointed snapshots
  of DuckDB stores in both data roots. The job atomically clones each DB and WAL
  on the source volume, checks inode, size, mtime and ctime before and after the
  pair, and retries up to four times if either file changes. DuckDB replays the
  captured WAL and checkpoints only the isolated clone. The live stores are
  never opened or checkpointed. A busy source or unavailable atomic clone fails
  the run; streamed database copies are never a fallback.
- `drover-data` and `drover-lakes`: the two `/Volumes/M2 1/` data roots. Both are
  copied before and after the Postgres dumps. Both raw passes exclude DuckDB,
  WAL, socket and lock files, plus `.drover-snapshots` and
  `.drover-backup-snapshots` scratch directories. Dependency `uv.lock` files are
  preserved. The second pass does not delete files retained by the first.
  Cross-database and catalog-to-file atomicity are
  not guaranteed while writers and garbage collection remain active. This job
  does not prove that every catalog reference can be restored.

Rsync requests file mode 600 and directory mode 700. After each successful NAS pull,
`chmod -R go=` removes group and other access from the pulled subtree because
the NAS home ACL otherwise leaves files mode 0777. Owner permissions, including
any owner execute bit left by the ACL, are retained; verification enforces that
group and other access are absent.
Rsync `--link-dest` shares unchanged files with the previous successful generation.
This permission cleanup also removes group and other access from any shared
inodes; do not edit published snapshots or add access to them. Staged metadata
is hashed before transfer and checked for size and SHA-256 on the NAS. Once the
final raw pass finishes, the frozen NAS lake trees are inventoried and hashed,
including files retained from the first pass. Verification rereads those files;
it never compares the copy against a subsequently changing live source. This
binds the manifest to the copied bytes, without proving transactional consistency
of arbitrary exporter outputs or catalog reference completeness. Expected hashes
are saved in private `MANIFEST.json` before verification, and `VERIFICATION.json`
records every missing, changed, mismatched or non-private artifact. Failure logs
contain escaped relative artifact paths and reasons, without file contents or
child diagnostics. Publication requires a successful verification report.
Published generations also include private `SUCCESS.json` and a consistency
warning. Archive listing checks structure, not a complete restore.

Retention preserves the newest seven successful daily generations and the newest
representative from each of the latest four ISO calendar weeks. These sets can
overlap. Other successful date directories are pruned only after a new generation
passes verification. Incomplete generations are retained for investigation.

Install both Python files under `~/.drover/maintenance/`, mode 700. Render
`scripts/launchd/com.drover.backup.plist.template` by replacing `__HOME__` with the
home directory, install it in `~/Library/LaunchAgents/`, validate with `plutil
-lint`, and load using `launchctl bootstrap gui/$(id -u) <plist-path>`. The plist
uses `~/.drover/maintenance/backup-python/bin/python`, a dedicated interpreter
with macOS Local Network permission. The live fix used Drover runtime Python:
Homebrew Python failed with `No route to host` under launchd, while the runtime
interpreter had access. Permission follows the executable, so a stable spelling
such as `runtime/current` alone does not preserve access when a release replaces
the underlying binary.

Before loading this template, provision the dedicated interpreter outside the
release tree. For example, use an existing authorized runtime interpreter to
create `~/.drover/maintenance/backup-python` with `python -m venv --copies`.
Install the runtime's pinned `duckdb==1.5.5` into that interpreter for isolated
snapshot checkpointing. Atomic snapshots require macOS and a clone-capable
source volume, with scratch directories located beside each source store.
Grant Local Network access to the resulting dedicated Python executable in
System Settings and verify NAS connectivity under launchd. Copying an executable
does not copy its permission grant. Keep this interpreter in place across Drover
releases; recheck permission whenever Python itself is replaced. Until it is
provisioned and authorized, retain the working live interpreter. Rendering the
template does not install Python or grant permission. A foreground manual run is
`~/.drover/maintenance/backup-python/bin/python ~/.drover/maintenance/studio_backup.py`.

Logs live in `~/Library/Logs/drover/backup.log`. The successful local receipt is
`~/.drover/backup-state/last-success.json`; `soak-probe.sh` reports its
`backup_age_h`. Missing evidence is informational before the first success;
once present, evidence older than 36 hours or invalid evidence fails the probe.

WAL/PITR instructions belong in `~/.drover/maintenance/pg-wal-archiving.md`.
They are a deferred maintenance plan, with no cluster restart or WAL changes
performed by this job.
