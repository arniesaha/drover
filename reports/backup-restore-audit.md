# P1.1 Backup and restore audit: Studio hub

Date: 2026-10-11. Scope: read-only audit of every stateful component of the
Studio hub, cross-checked against the repo at the worktree HEAD (v0.6.6).
Nothing was written, restarted or reconfigured.

## 0. Read this first: the live half of the audit did not run

The brief allowed read-only local commands on the Studio (ls, cat of
non-secret files, launchctl print). The harness this session ran in denied all
of them. Denied, each tried once and not worked around:

- `ls` of `~/Library/LaunchAgents`, `~/.drover` and `/Volumes/M2 1`
- `launchctl print gui/501/com.drover.server`, `tmutil destinationinfo`, `crontab -l`
- an inline read-only Python listing (names, sizes, mtimes only)
- the Read tool on `~/.drover/config.toml`
- writing a scratch metadata script under `scripts/`

So this report is a **repo-side audit plus a live checklist**, not a completed
live inventory. Every statement below carries one of these evidence tags:

| Tag | Meaning |
|---|---|
| [repo] | Read in this worktree's code or docs today |
| [brief] | Stated in the task brief, not observed by me |
| [note] | From notes kept by earlier sessions on this machine (2026-10-06 to 10-10), not re-observed today |
| [inferred] | My reasoning from the above, to be confirmed |
| [UNVERIFIED] | Needs the live check in section 6 |

The single most important consequence: **I found no backup automation in the
repo, but I could not look for one on the machine.** Time Machine, a cron job,
a hand-made LaunchAgent or a manual dump habit would all be invisible to me.
Section 6 lists the exact read-only commands that close this, about 15 minutes
of work for someone with a normal shell.

## 1. Headline findings

1. **No scheduled backup exists in the product.** The repo has no timer,
   LaunchAgent, cron entry or worker that backs anything up. `docs/backup.md`
   says so directly: the R2 workflow "remains unimplemented" and "production
   execution remains blocked". [repo]
2. **The DuckLake catalog database has no backup code path at all.** The only
   prod-capable backup command, `drover-server cutover backup`, dumps the
   `drover_control` schema and copies `config.toml` and the server plist. It
   does not dump the lake catalog database (`drover_lake_v2`). `lake backup`
   takes a catalog snapshot only if the operator supplies one by hand, and it
   refuses to run against a live root. [repo]
3. **The most recent backup that the repo implies was taken is the cutover
   backup of 2026-10-06**, written by `cutover switch` step 2. By default it
   lands in `$DROVER_LAKE_ROOT/.cutover-backups/v2/<utc>/`, which is on the
   same volume as the lake it would help recover. Whether it still exists is
   [UNVERIFIED]. If it is the latest, control-store RPO today is about 5 days
   and growing. [repo] [inferred]
4. **Control store and lake cannot be restored independently.** After outbox
   retention prunes a hot payload (14 day minimum), that payload lives only in
   lake parquet. A PostgreSQL dump alone then "cannot restore complete
   history" (`docs/postgresql-control-store.md`). Losing `/Volumes/M2 1`
   loses history that no control-store dump contains. [repo]
5. **The server plist is hand-edited secret-bearing state with no drift
   detection.** The cutover runbook tells the operator to add the lake reader
   and exporter DSNs to the plist's `EnvironmentVariables`. `install.sh`
   rewrites `com.drover.server.plist` unconditionally from a template that
   contains only `HOME` and `PATH`. Any re-run of the installer's service step
   silently drops those hand-added entries. I found no plist comparison in the
   repo (I did not read all of `doctor`). This matches the shape of the Oct 10
   overwrite that went unnoticed for a day, though I could not confirm that
   was the mechanism. [repo] [inferred]
6. **No restore has been run as a recovery drill.** Restore-shaped steps have
   run incidentally (the cutover gate restores a control-schema dump into a
   scratch database; a 2026-10-03 rehearsal restored a PostgreSQL backup with
   pgvector). No timing, no end-to-end hub start on restored state, no lake
   catalog restore, and no second-machine restore are recorded. [repo]

## 2. Inventory

RPO and RTO are my estimates. Where the live state is unknown they are stated
as a range with the assumption that drives them.

| # | Component | Location | Backed up by / where / how often / retention | Restore documented? | Restore tested? | RPO estimate | RTO estimate |
|---|---|---|---|---|---|---|---|
| 1 | Control PostgreSQL (schema `drover_control`) | Port 15432 [brief]. Not the managed container default (54329), so most likely an operator-owned cluster in "existing DSN" mode [inferred]. Data directory [UNVERIFIED] | Automated: none [repo]. Manual: `cutover backup` (custom-format dump of the schema, sha256 manifest, `pg_restore --list`), last implied run 2026-10-06, kept "until the soak passes", no retention policy. Destination by default on the lake volume | Partly. Generic guidance in `docs/postgresql-control-store.md`. The only step-by-step restore is `drover-managed-postgres restore`, which targets the Docker container and probably does not apply to this hub | Incidentally: gate restore into `drover_gate_v2_control` around 10-06; pgvector restore on 10-03. Never as a timed recovery | 5+ days if the 10-06 dump is the latest and survives; unbounded if it does not | 30 to 90 min with a good dump (pgvector must exist on the target; roles and globals are not in the dump). Unknown without one |
| 2 | DuckLake v2 catalog database (`drover_lake_v2`) | Same or separate PostgreSQL cluster [UNVERIFIED] | None found. Not covered by `cutover backup` [repo] | No | No | Unbounded | Rebuild only: see row 3 |
| 3 | DuckLake v2 parquet data root | `/Volumes/M2 1/...` [brief], exact root [UNVERIFIED]. Grows about 4 files and 1 snapshot per export, no compaction [note] | None found. No copy job in the repo. The legacy parquet root `~/.drover/parquet` was to be kept through the soak and can seed a rebuild if still present [repo] [UNVERIFIED] | No for the live lake. `lake backup` / `lake restore` are documented for isolated rehearsal copies only | A full rebuild from the 2026-10-01 tar was rehearsed (890 s rebuild, 264 s verify, 5.29M rows). That is rebuild proof, explicitly "not backup/restore" | Unbounded for anything older than the outbox window. Last 14 days are replayable from the control outbox in principle | Hours. Rebuild is about 20 min of compute at Oct 1 scale, plus import, role provisioning, proof and operator time, none of it rehearsed as a recovery |
| 4 | Lake verification state (`verification/serving-proof.json`, `serving-checkpoint.json`, `cutover-state.json`, gate verdict) | Under the lake data root and `$DROVER_LAKE_ROOT/.gate`, `.cutover-backups` [repo] | None. Lives and dies with the lake volume | No | No | Same as row 3 | Regenerated by `lake verify` and the gate; included in row 3 |
| 5 | harnessd registry DuckDB | `~/.drover/drover.registry.duckdb` unless `DROVER_CONTROL_PLANE_DUCKDB` overrides it [repo]. Live path [UNVERIFIED] | None found | No | No | Unbounded | 10 to 30 min: harnessd recreates an empty registry; hosts, sessions and worktree bindings re-register, local session history in it is lost. Not rehearsed [inferred] |
| 6 | `~/.drover/config.toml` | Home directory [repo] | Copied by `cutover backup` on 10-06 only. No versioning. `[analytics]` was rewritten in place by the switch | Named as a backup part in the site operations page; no procedure | No | Since 10-06 at best | 15 to 60 min to rebuild by hand: epoch, verification digest, extension and engine pins must match the lake exactly or serving fails closed |
| 7 | `~/.drover/server.env` (control DSN) and `~/.drover/api_token` | Home directory, mode 0600 by design [repo]. Presence and use on this hub [UNVERIFIED]; the plist is reported to carry the DSNs directly [note] | None found | No | No | Unbounded | Minutes if the PostgreSQL role password is known elsewhere; otherwise a role password reset plus re-pairing |
| 8 | LaunchAgent plists (`com.drover.server`, `com.drover.harnessd`, `com.drover.mac-ollama-embeddings`, any others) | `~/Library/LaunchAgents` [repo]. Full list [UNVERIFIED] | Server plist copied by `cutover backup` on 10-06 only. Others: none | No | No | Since 10-06 for the server plist; none for the rest | 10 to 30 min to re-render and re-add env entries, if someone still has the DSNs. Detection time is the real cost: about 1 day on Oct 10 [brief] |
| 9 | Host and device credentials (hashes in `control_credentials`; raw host token in each host's `~/.drover/api_token`) | Hub: inside row 1. Hosts: each machine [repo] | Covered only by whatever covers row 1 | Rotation and re-issue are documented in `docs/multi-host.md`; restore is not | No | Same as row 1 | Per host: `pair-host` or `credentials issue-host`, a few minutes each. Phones re-pair by QR |
| 10 | Runtime installs | `~/.drover/runtime/<version>` with a `current` symlink; a shared `~/.drover-venv` is mentioned for in-place activation [repo] | Reproducible from the public release (checksummed wheel and lockfile). Earlier versions stay installed side by side | Yes: `drover-server update`, `rollback`, `install.sh` | Rollback has unit tests (not run today) | Zero (reproducible) | 10 to 20 min reinstall. Risk is that reinstall rewrites the plists (finding 5) |
| 11 | Lake engine extensions and pins | `DROVER_LAKE_EXTENSION_DIR`, engine sha256 pinned in config [repo]. Path [UNVERIFIED] | None found. Re-fetchable with `scripts/fetch-lake-extensions.sh` only while the CDN still serves that DuckDB version | No | No | Zero if re-fetchable | Minutes, but a digest mismatch fails serving closed |
| 12 | Ingest spool and collector cursors | `~/.drover/incoming` (processed copies kept 7 days), `~/.drover/state` [repo] | None; transient by design | n/a | n/a | n/a | Cursor loss causes re-shipping, which dedup keys should absorb [inferred] |
| 13 | Legacy stores | `~/.drover/parquet`, `~/.drover/drover.duckdb`, a `pre-ducklake-20261001/lake-and-catalogs.tar` referenced by the rehearsal evidence [repo]. Presence and location [UNVERIFIED] | They are themselves the only pre-cutover copy of older history | Rebuild from tar is documented and rehearsed | Yes, rebuild only (see row 3) | Frozen at 10-01 / 10-06 | See row 3 |
| 14 | Logs | `~/Library/Logs/drover/` [repo] | None found. Hub error log is about 1M lines with time-only stamps [note] | n/a | n/a | n/a | n/a. Matters for forensics, not recovery |

## 3. Cross-check: what the docs say against what the code does

| Claim in docs | Reality | Verdict |
|---|---|---|
| `docs/backup.md`: backups are immutable R2 generations with receipts, a manifest and a recorded consistency boundary between lake and control store | Design only. The file states it is unimplemented. No R2 code exists | Honest, but there is no interim procedure in its place |
| `site/.../operations.mdx`: "A complete backup has these parts: control store dump, lake catalog and files, `~/.drover/`" | Correct as a list. No command produces parts 2 or 3, and the page omits `~/Library/LaunchAgents`, which on this hub carries the lake DSNs | Incomplete for this hub |
| `operations.mdx` and `postgresql-control-store.md`: use `drover-managed-postgres backup` | Applies to the Docker-managed mode only. The Studio control store is on port 15432, not the managed default | Likely not applicable here [UNVERIFIED] |
| `postgresql-control-store.md`: a consistent backup fences API and worker writes, records the acknowledged manifest boundary, then dumps and copies archives under the fence | No command implements the fence-dump-copy sequence. `cutover backup` dumps without a fence and copies no lake files | Documented procedure has no tooling |
| `postgresql-control-store.md`: globals are outside a per-database dump; base backup and WAL archiving are "separate operational work" | Nothing in the repo configures them. Live PostgreSQL settings [UNVERIFIED] | Stated gap, still open |
| `lake-cutover.md`: backup step is "verified by the sha256 manifest and `pg_restore --list`" | True in `take_backup` / `verify_backup`. This proves the archive is readable, not that it restores | Accurate but weaker than it reads |
| `lake-cutover.md`: add reader and exporter DSNs to the plist | `render_launchd` was built to keep secrets out of the plist (it sources a 0600 env file through a shell wrapper). The runbook goes against that design, and the installer does not know about the extra entries | Conflict. Root of finding 5 |
| `docs/backup.md`: `lake backup` verifies parquet row counts against `verification/report.json` | It counts only files under `partitions/` with the rebuild layout's names and fails on any other parquet name there. Files written elsewhere in the root are copied and hashed but not counted. The live v2 lake was built by seed, import and continuous export, so the count check may not describe it, or may fail | Tool fit for the live lake is unproven [inferred] |
| `postgresql-control-store.md`: "Arbitrary worker filesystem relocation is not a supported restore operation" | `lake/runtime.py` attaches with `OVERRIDE_DATA_PATH false`. A restore to any other path is expected to fail closed [inferred] | Consistent, and it means the lake volume path is itself critical state |

Tests that exist: `tests/test_lake_backup.py` (4 tests, fixture lakes),
`tests/test_lake_cutover.py` backup, switch and rollback cases with an injected
runner, `tests/test_in_place_activation.py` rollback cases. In harness
worktrees the DuckLake-backed suites skip for lack of the extensions variable
[note]. I did not run any tests today.

## 4. Gaps, ranked by risk

Ranking is likelihood times blast radius, as I judge it from the repo.

**G1. Critical: the lake has no backup and sits on one external volume.**
Catalog database and parquet root are both uncovered. Loss or corruption of
`/Volumes/M2 1` loses every payload already pruned from the control store, and
the only recovery is a rebuild from legacy inputs that may or may not still
exist. The volume name ends in " 1", which is what macOS does when two volumes
share a name at mount time [inferred]; if the path ever changes to
`/Volumes/M2`, the configured data root no longer resolves and serving fails
closed.

**G2. Critical: control store backup is ad hoc and already 5 days old.**
No schedule, no retention, no off-volume copy, no alert on age. The last
implied dump may be on the same volume as G1.

**G3. High: no restore has ever been proven end to end.** Nobody has started
a hub on restored state. Known unknowns: pgvector on the target, roles and
grants (the dump is taken with `--no-owner --no-privileges`), lake catalog
group roles, the data path pin, and the serving proof digest in config.

**G4. High: hand-edited plist with secrets, silently overwritable.**
`install.sh` writes the plist with no comparison and no backup of the previous
file. There is no known-good baseline and no alert. Detection on Oct 10 took
about a day. A fix is small: move the lake DSNs into the env file the template
already supports, and have `doctor` compare the installed plist against a
fresh render.

**G5. High: nothing monitors backup existence or age.** Even a manual habit
would decay unnoticed. `/readyz` and `doctor` have exporter freshness now
(#564) but no backup freshness that I found.

**G6. Medium: cross-store consistency is undefined.** A control dump and a
lake copy taken at different moments are not a consistent pair while export
runs. The documented fence procedure has no tooling. Practical mitigation
exists (see drill step B2) but is unproven.

**G7. Medium: backups that do exist hold plaintext secrets beside the data.**
`cutover backup` copies the server plist, DSNs included [note], into the
backup directory with `copy2`. Off-site copies of that directory need
encryption, and the directory mode should be checked.

**G8. Medium: harnessd registry DuckDB is uncovered.** Probably recoverable
by re-registration, but live session bindings and worktree records are lost,
and that path has not been exercised.

**G9. Low: config, extension pins and credentials have no versioned copy.**
Small files, slow to rebuild by hand because of the fail-closed digests.

**G10. Low: no second machine.** The roadmap lists "second-machine restore"
as a gate; it has not happened. Single host, single volume.

## 5. Restore drill plan (scratch only, prod untouched)

This plan needs its own approval. It runs `pg_dump` against prod in read-only
sessions, which this audit was not allowed to do. It writes only under one new
scratch directory and one throwaway PostgreSQL cluster. It never stops,
restarts or reconfigures a service, never writes to `/Volumes/M2 1`, and never
points a scratch process at a prod DSN with a write-capable role.

Record wall-clock time for every step. Those timings are the first real RTO
numbers.

### Guard rails

- Scratch root: a new directory on the internal disk, for example
  `~/drover-drill-20261013/`. Confirm free space first (lake size plus 30%).
- Scratch cluster: `initdb` into `<scratch>/pg`, loopback only, on a spare
  port that is not 15432, the same pattern `scripts/lake_gate_rehearsal.py`
  uses. Same PostgreSQL major version as prod, with pgvector installed.
- Use the service's own build: `~/.drover/runtime/current/bin/drover-server`.
- Prod DSNs are read from the environment the operator already has. They are
  never typed on a command line, never echoed, never written into the scratch
  directory in clear text.
- Abort rule: if any command would need a write-capable prod role, a prod
  path as a destination, or `launchctl`, stop.
- Concurrency: lake query children share an admission lock per data root
  [note]. Run one lake reader at a time during the drill to avoid starving the
  cockpit.

### A. Control store

- A1. Dry run, read it: `drover-server cutover backup --lake v2 --backup-root <scratch>/control-backup --dry-run`.
- A2. Take the backup with the same command without `--dry-run`. It uses a
  read-only session and writes `control.dump`, `config.toml`, the plist copy
  and `manifest.json`. Set the directory to mode 0700 (it holds the plist).
- A3. Record prod reference counts in a read-only session: row counts for
  each `drover_control` table and the newest outbox sequence. Save as
  `<scratch>/control-reference.json`.
- A4. Create a scratch database and role, create the vector extension, then
  `pg_restore` the dump into it. Note every error; the roles and privileges
  gap in G3 will show up here.
- A5. Compare table counts with A3. Append-only tables must not be lower than
  in the dump; tables that retention prunes can legitimately differ.
- A6. Write a scratch `config.toml` whose control DSN variable points at the
  scratch cluster, with `[analytics]` left on the legacy default and scratch
  paths for everything else. Run `drover-server --config <scratch>/config.toml control-store status`. Pass: `"ready": true`.
- A7. Optional, most valuable: start a scratch hub in the foreground on a
  spare port with that config (not through launchd), call `/readyz`, list
  hosts and one session, then stop it. Confirms a restored control store
  actually serves.

### B. Lake

- B1. Dump the catalog first: `pg_dump --format=custom` of the lake catalog
  database in a read-only session with the reader or admin role. Record the
  newest snapshot id.
- B2. Then copy the data root to `<scratch>/lake/v2` with a read-only source
  (`rsync -a` or `cp -Rp`; never `mv`). Order matters: with no compaction or
  file expiry, the exporter only adds files, so files copied after the catalog
  dump are a superset of what that catalog references. Confirm that assumption
  still holds on the day (no compaction deployed).
- B3. Restore the catalog dump into a scratch database, and create the three
  catalog group roles with `provision_catalog_roles` as documented in
  `docs/operations/lake-catalog-roles.md`.
- B4. The data path pin. The catalog records the prod data root and the
  runtime attaches with `OVERRIDE_DATA_PATH false`. Expected first result: the
  scratch attach fails closed. Record the exact error. Then test the two
  candidate procedures on the scratch catalog only: (a) update the stored data
  path in the scratch catalog metadata, (b) present the scratch copy at the
  original path on a second machine. Whichever works becomes the documented
  restore step. This is the single biggest unknown in the whole plan.
- B5. `drover-server lake verify --data-root <scratch>/lake/v2 --catalog-dsn-env <SCRATCH_CATALOG_VAR>`. Pass: verification succeeds at the snapshot from B1.
- B6. Tool fit check. Add the `.drover-isolated-staging-copy` marker to the
  scratch copy and run `lake backup` then `lake restore` on it, exactly as in
  `docs/backup.md`. Expected risk: `lake_backup_verification_report_required`,
  `lake_backup_unexpected_partition_file` or `lake_backup_row_count_mismatch`,
  because the live lake was not built by the rebuild path. Either outcome is a
  result: it tells us whether the tool can be the basis of a real backup or
  needs work.

### C. Paired restore

- C1. Point the A6 scratch config at the scratch lake (`backend=ducklake`,
  scratch data root, scratch reader variable, digest of the scratch
  `serving-proof.json`). Start the scratch hub in the foreground.
- C2. Pass criteria: `/healthz` reports `analytical=ok`; `/readyz` is 200; a
  session older than 14 days replays as `archived`, not `unavailable` (this
  proves pruned payloads come back from the lake copy); a recent session
  replays fully.
- C3. Stop the scratch hub. Confirm prod `/readyz` was 200 throughout.

### D. Small state

- D1. Registry: copy the registry DuckDB to scratch (a file copy while
  harnessd runs may be torn; note whether DuckDB opens it). Open it read-only
  from scratch and count hosts and sessions. Separately, start a scratch
  harnessd with an empty registry against the scratch hub and record what
  comes back by itself.
- D2. Plist drift: render the plists into scratch with `render_launchd` using
  the live arguments, and diff the key set against the installed plists, keys
  only, values never printed. Any key present live and absent from the render
  is state the installer will destroy. This is also the prototype of the
  `doctor` check proposed in G4.
- D3. Runtime: `install.sh --verify-release` and `install.sh --dry-run`. Both
  are documented as non-mutating. Confirm the dry run lists the plist rewrite.
- D4. Extensions: run `scripts/fetch-lake-extensions.sh <scratch>/ext` and
  compare digests with the pinned ones in config.

### E. Close out

- E1. Fill in a results table: step, pass or fail, duration, surprises.
- E2. Replace the estimates in section 2 with measured RPO and RTO.
- E3. Stop the scratch cluster. Removing the scratch directory is a separate,
  explicit step by the operator, because it holds a full copy of prod data and
  a plist copy with credentials.

## 6. Live checklist to finish the inventory

Read-only, about 15 minutes, to be run by someone with a normal shell on the
Studio. None of these print secrets if run as written.

| Question | Command |
|---|---|
| Which agents exist, and when did each plist last change? | `ls -la ~/Library/LaunchAgents /Library/LaunchDaemons` |
| Is any backup-like job loaded? | `launchctl list` filtered for backup, dump, rsync, restic, drover, postgres |
| Is Time Machine on, to where, and are `~/.drover` and the M2 volume excluded? | `tmutil destinationinfo`, `tmutil latestbackup`, `tmutil isexcluded ~/.drover "/Volumes/M2 1"` |
| Any cron entries? | `crontab -l` |
| How is PostgreSQL on 15432 run (Homebrew service, Docker, hand-started), and where is its data directory? | `launchctl list` for postgres entries, `ls ~/.drover/bin`, and `SHOW data_directory` in a read-only session |
| Are control store and lake catalog in one cluster? | Compare host and port of the DSN variables, names only |
| Are WAL archiving or base backups configured? | `SHOW archive_mode; SHOW archive_command;` read-only |
| Does the 10-06 cutover backup still exist, and with what modes? | `ls -la "<lake root>/.cutover-backups/v2/"` |
| Any hand-made backups? | `ls -la ~/.drover` looking for `backups`, `parquet-backup-*`, `pre-ducklake-*`; `drover-server reclaim` (without the sweep flag) reports these |
| Does the legacy parquet root still exist? | `ls -la ~/.drover/parquet` |
| Lake size and file count (drives drill disk and RTO) | `du -sh "<lake data root>"` and a file count |
| Volume identity and health | `diskutil info "/Volumes/M2 1"`; `ls /Volumes` to see whether a second "M2" is mounted |
| Where is the registry DuckDB, and how big? | `ls -la ~/.drover/*.duckdb` |
| Which plist keys exist beyond the template? | Step D2 above, keys only |

## 7. Suggested order of work for the week

1. Run section 6 (15 minutes) and correct this report where it is wrong.
2. Take one control dump and one catalog dump to a location off the lake
   volume. That alone moves G1 and G2 from unbounded to the age of that dump.
3. Run drill parts A and D (low risk, about an hour).
4. Run drill part B, with B4 as the explicit research item.
5. Decide the interim schedule and the plist fix (G4) from the drill results.
