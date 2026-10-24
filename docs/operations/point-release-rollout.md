# Point-release rollout

Use this path for a release with unchanged lake schema and analytical runtime.
It also admits a strictly appended control-DB migration only when every new SQL
statement is mechanically proven additive. A patch version number alone does not
establish compatibility. This runbook describes operator actions; the preflight
itself changes no service, runtime symlink, config, lake catalog or database.

Run tooling from a build containing `cutover release-preflight`. An older running
service cannot expose a command added by a newer build. Set `TOOL` to that build's
`drover-server` executable, and `TOOL_PY` to its Python interpreter. Use explicit
`--plist` and the existing production lake name/root. Keep release artifacts and
checksum manifests from the same published release.

## Sequence

1. Quiesce work. Disable updates in the hub and affected harness configurations
   and restart the relevant daemons so the frozen policy is in memory. Set the
   pin to the intended release while updates remain disabled. Do not remove an
   outstanding verification marker without investigating its activation.
2. Download the published release wheel, hash-pinned requirements lock and
   checksum manifest. Stage the target beside the current runtime using
   `install_version`; do not rerun the full installer to stage a release because
   that installer also selects `runtime/current` and regenerates service units.
   `install_version` verifies the downloaded wheel and lock digests and installs
   dependencies with `--require-hashes`, retaining the verified artifact cache.
   Its Python API accepts `RuntimeLayout(home, root=...)` and
   `ReleaseArtifact(version, wheel_url, wheel_sha256, lock_url, lock_sha256)`.
   Supply the configured runtime root when it differs from `~/.drover/runtime`.
3. Take a fresh verified backup with the existing command. It includes the
   control DB custom dump plus the current config and complete service plist.
   Preserve the prior installed runtime and the existing lake data directory.

   ```sh
   "$TOOL" cutover backup --lake "$LAKE" --lake-root "$LAKE_ROOT" \
     --plist "$PLIST" --pg-bin "$PG_BIN"
   ```

   Use the returned backup directory in the next command. Because config/plist
   hashes must match their backups, take the backup after freezing the policy.
4. Run the point-release preflight before selecting the staged runtime:

   ```sh
   "$TOOL" cutover release-preflight \
     --lake "$LAKE" --lake-root "$LAKE_ROOT" --plist "$PLIST" \
     --release "$RELEASE" --wheel-sha256 "$WHEEL_SHA256" \
     --lock-sha256 "$LOCK_SHA256" --backup "$BACKUP" --pg-bin "$PG_BIN" \
     --device-token-env DROVER_RELEASE_GATE_DEVICE_TOKEN
   ```

   Obtain the two digests from the published checksum manifest, independently
   of the local artifact cache. Defaults: 5 GiB free at the runtime filesystem,
   backup age at most 24 hours. Exit 0 means every check passed, 1 means a failed
   check, and 2 means invalid/unreadable inputs. No failed check is a waiver.
   Invalid-input errors name the bad input; quote any path containing spaces.
   Set `DROVER_RELEASE_GATE_DEVICE_TOKEN` in the invoking environment to a
   device credential before running this command; do not place its value in the
   shell command, plist, or release evidence. The check lists online hosts and
   requires an authenticated hub `GET /harness/hosts/<id>/fs/list` to return
   200 for every one. A `preflight`-scope credential is not enough; pair a
   dedicated device (for example labelled `release-gate`), keep its token in a
   mode-600 file such as `~/.drover/release-gate.env`, and load it first with
   `set -a; . ~/.drover/release-gate.env; set +a`. Hosts whose harnessd predates
   folder listing answer 404 and fail this check until they are upgraded.

### NAS snapshot + release-preflight evidence ledger

When a point-release operator also needs the supported Studio NAS snapshot in
the same evidence trail, append these options to the command above:

```sh
  --nas-snapshot-receipt "$HOME/.drover/backup-state/last-success.json" \
  --nas-snapshot-manifest-sha256 "$NAS_MANIFEST_SHA256" \
  --ledger-root "$EVIDENCE_DIR" \
  --ledger "$EVIDENCE_DIR/release-preflight.json"
```

This is an evidence adapter around the existing snapshot receipt and the
existing `release-preflight` checks. It does **not** start a NAS backup, write
to the NAS, publish a release, select a runtime, restart a service, or execute
a rollout. The ledger path is created once, mode 600, and cannot be rewritten;
use a new path for a new attempt. The evidence root must already be
owner-only (mode 700) and is the sole permitted parent for the ledger; symlinks,
group/world-writable roots, and nested traversal are rejected. Creation fsyncs
both the private ledger file and its evidence directory.

The receipt must be produced by the current Studio job: it carries explicit
`verification_ok: true`, the immutable NAS `MANIFEST.json` SHA-256, and a
versioned `studio_backup` HMAC provenance record. Supply the manifest digest as
`NAS_MANIFEST_SHA256` and make the matching key available only through
`DROVER_NAS_SNAPSHOT_RECEIPT_KEY` (or `--nas-snapshot-key-env`). The Studio
backup process uses that same private key when it writes its local receipt.
Old receipts without this explicit correlation evidence are intentionally
blocked; do not add fields by hand. A valid receipt has explicit
`verification_ok: true`, a matching local/NAS manifest SHA-256 pair, and a
versioned Studio producer HMAC. By default the check also rejects receipts
older than 36 hours. `--max-nas-snapshot-age-hours` changes only that age bound.

The JSON document has one explicit state for each phase:

| Phase | What a successful state proves | What it never implies |
| --- | --- | --- |
| `nas_snapshot_execution` | The existing local receipt attests to a verified snapshot run. | A new snapshot was run by this command. |
| `nas_snapshot_publication` | The same receipt attests that its verified generation was published. | Any release artifact was published. |
| `release_preflight` | Every existing read-only preflight check passed. | Runtime selection, rollout, or acceptance. |
| `release_publication` | Always `skipped` here. | Release publication happened. |
| `rollout` | Always `blocked` here with a `human_release_operator` next action. | Approval or execution of a rollout. |
| `acceptance` | Always `blocked` until a separate owner records it. | Production acceptance. |

An invalid or stale receipt records snapshot failure, snapshot publication and
preflight as blocked, and does not call the preflight checks. A preflight check
failure is preserved with every failed check ID and a `release_operator`
next-action. Do not edit the ledger to change either outcome; repair the real
condition and create a new evidence ledger. This wrapper is deliberately not a
work-item creator: use the configured Drover/GitHub work-item mechanism only
when an authorized live hub is available, then link its externally assigned
reference in the operator's approved evidence system rather than inventing one
locally.
5. If `release_kind` reports `lake_schema`, stop this sequence and follow the
   [full lake cutover gate](lake-cutover.md). `control_migration` is eligible
   for this path only when its `control_migrations_allowed` evidence is true.
   The evidence records `new_migration_ids` and one verdict per SQL statement;
   retain it in the release ledger. A refused statement, edited/removed shipped
   migration, non-appended id, or unknown metadata blocks this path. Do not
   override the selector based on a version label.
6. For a passing point release, record the bounded planned stop **before**
   stopping the quiescent server and local harness. The marker authorizes one
   check-in recovery attempt for at most two hours; it does not change config,
   lake data, or credentials:

   ```sh
   "$TOOL" planned-stop begin --step point-release-rollout --actor release-operator
   ```

   For symlink activation, use `RuntimeLayout.flip(RELEASE)`
   with the same home/root that passed preflight. For configured `in_place`
   activation, first use `install_cached_into_venv(layout, RELEASE, venv)` on the
   explicitly configured venv and require success, then flip the runtime record.
   Keep service argv and its environment unchanged.
7. If any lake command in this release window published a serving proof
   (`lake verify` or a non-no-op `lake deprecate-legacy --apply`), copy the
   command's printed
   `[analytics] verification_sha256 = "…"` line into the selected config.
   Validate with the hub loader before starting the hub:

   ```sh
   "$TOOL_PY" -c 'from drover.server.__main__ import _resolve_config; import sys; _resolve_config(sys.argv[1])' "$CONFIG"
   ```

   Do not start the hub until this succeeds; a stale pin deliberately blocks
   startup.
8. Start the services. Hub bootstrap runs the normal ordered PostgreSQL control
   migration runner before workers start, applying each missing approved version
   transactionally and recording it in `control_schema_migrations`; no separate
   rollout command is needed. Then call `verify_hub(base_url)` or inspect
   `/healthz` and `/readyz`. Require `ok\nanalytical=ok` and readiness HTTP 200.
   On failure, stop services and explicitly restore the retained prior runtime;
   in-place mode must also reinstall the prior cached wheel. Do not attempt to
   reverse an approved additive control migration: the prior runtime ignores the
   extra nullable column and new table, so it remains rollback-compatible. Start
   and verify again. Re-enable updates only after health succeeds and the pin
   matches the release, then clear the marker:

   ```sh
   "$TOOL" planned-stop complete
   ```

## Planned-stop check-in recovery

While the marker is active, the thin external check-in should run exactly:

```sh
drover-server recover-planned-stop
```

It prints one line. `healthy` clears a stale marker; a down hub gets one
launchd/service-manager restart only when the unexpired marker has no prior
attempt. It prints `recovered` on readiness, or `failed: ...` with the first
server-log startup error. Missing, expired, or already-attempted markers print
`unplanned outage: human decision required` and return non-zero. Check-ins do
not decide whether to restart and must not loop or call the service manager.

For the Python API actions above, use the tooling interpreter and import
`RuntimeLayout` from `drover.server.runtime`, `ReleaseArtifact`,
`install_version` and `install_cached_into_venv` from `drover.server.updates`,
and `verify_hub` from `drover.server.lake.cutover`. Check boolean return values;
never proceed after a failed install. The preflight is evidence at the time of
its run, not a lock against concurrent operator changes. Keep updates frozen and
rerun after any configuration, artifact or runtime change.

## Checks and release selection

| Check | Required evidence |
| --- | --- |
| `plist_validity` | Complete parsed job dict with Label, nonempty argv, HOME/PATH environment, boolean KeepAlive/RunAtLoad, timeout and log paths. |
| `updater_state` | Updates disabled or pin equals target, and no verification marker. |
| `runtime_installed` | Cached wheel/lock match independently supplied SHA-256 values; target interpreter reports target version; installed Drover files equal the wheel. DuckLake deployments also verify target engine against configured digest and extension files against pinned digests. |
| `release_kind` | Checked current interpreter is the one named by service argv and activation config. Lake schema and engine/runtime pins must match. A control-only change must be a strict append whose new SQL statements are each admitted as additive; evidence includes migration ids and statement verdicts. |
| `disk` | Free bytes at configured runtime filesystem meet the threshold. |
| `backup` | Exact required files, matching lake/control schema, recent timestamp, verified file hashes, readable dump, and unchanged current config/plist. |
| `health` | Current service liveness includes analytical=ok, and readiness returns 200. |
| `host_proxy` | A device credential lists the fleet, then each online host answers hub-proxied `GET /harness/hosts/<id>/fs/list` with HTTP 200. |

The repo has no separate numbered lake migration ledger. The selector therefore
uses a derived lake schema version: a SHA-256 over AST declarations and DDL
producer functions across installed lake modules, including calls to
`create_table` and `configure_catalog`. Comments and whitespace do not change
it. A changed lake schema or runtime pin always requires the full gate.

Control migrations are compared as an ordered ledger. Existing entries must be
byte-for-byte unchanged and new ids must be strictly appended above the current
maximum. The point-release classifier allows only `CREATE TABLE`, `CREATE INDEX`
(including `CONCURRENTLY`), `CREATE SCHEMA IF NOT EXISTS`, `GRANT`, and `ALTER
TABLE ... ADD COLUMN` when the column is nullable or has a constant default. It
also accepts one narrow inline check on a newly added nullable direct built-in
integer column (`SMALLINT`, `INTEGER`, or `BIGINT`):
`CHECK (column BETWEEN <numeric literal> AND <numeric literal>)`. The check
must reference that same unquoted new column exactly and contain no other
expression. Domains, user-defined or qualified types, named or table-level
constraints, multiple checks, cross-column
checks, functions, subqueries, `OR`, nonliteral bounds, and `NOT NULL` with a
check are refused, along with drops, renames, type/constraint changes, data
writes/backfills, functions, triggers, and any SQL it cannot parse into that
subset. Runtime
compatibility includes declared DuckDB/extension pins and the installed DuckDB
binary hash. Different engine bytes, including a changed interpreter ABI,
require the full gate. Changes to DDL-producing functions can conservatively
require the full gate even when resulting SQL is unchanged. New schema helpers
must be added to this detection contract and its source-mutation tests.

The selector runs the same read-only script in both installed interpreters with
`-I -B`, avoiding checkout/PYTHONPATH substitution and bytecode writes. It does
not attach or migrate a lake. The full cutover gate remains unchanged for schema
releases, including its installed-version, runtime pins and target-path checks.

## Safe plist inspection and edits

Never run `plutil -extract ProgramArguments xml1 "$PLIST"` as a read-only probe.
Without an explicit output destination it replaces the input with an array.
`plutil -lint` still considers that valid plist syntax. Read-only extraction is:

```sh
plutil -extract ProgramArguments xml1 -o - "$PLIST"
```

Use `validate_launchd` for semantic validation. Use `write_launchd` for every
service plist edit. It rejects fragments and incomplete jobs, checks Label
against the destination filename, round-trips serialization, reloads the staged
file, fsyncs its contents and atomically replaces the destination. Failed
validation/replacement preserves the original file and removes staging. Existing
permissions survive. Preserve the full job when changing argv, for example:

```python
from pathlib import Path
import plistlib
from drover.server.service_units import validate_launchd, write_launchd

path = Path.home() / "Library/LaunchAgents/com.drover.server.plist"
job = validate_launchd(path.read_bytes())
# Set the complete intended argv, preserving the environment wrapper if used.
job["ProgramArguments"] = intended_arguments
write_launchd(path, plistlib.dumps(job))
```

The installer now uses this writer. Updater activation moves the runtime symlink
or installs into a configured venv; it does not rewrite service plists. Cutover
backs up plists and changes analytics config, without rewriting the service job.

## What the updater pin means

`[update].enabled` defaults to true. A nonempty `pinned_version` is the hub's
fleet target instead of the latest-release feed. It carries no artifact URLs,
so a host can converge only if the pinned version is already installed. It is
not a statement of what is currently running, and does not automatically change
when an operator rolls out another version.

A 0.6.4 pin can therefore coexist with running 0.6.5. Before these fixes, the hub
published that older target, but a host's `observe` rejected targets at or below
its active runtime. The hub planner itself never flips `runtime/current`.
Harnessd can flip it after verified installation, smoke test and quiescence,
then request restart. Its registration watchdog can intentionally roll back a
failed activation from `pending_verification.json`.

The planner now suppresses pins older than its active runtime. Activation also
rechecks the pending target after smoke/quiescence, preventing a deferred older
target from undoing a newer manual rollout. Recovery discards a marker superseded
by a newer runtime. Intentional watchdog rollback of its own failed activation
and explicit operator rollback remain available.
