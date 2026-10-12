# Studio native PostgreSQL supervision

Prepared locally on `Arnabs-Mac-Studio.local` for issue #583 on October 12,
2026. The host runs the Drover control store and DuckLake v2 catalog on port
15432. Preparation did not stop or load any services and did not modify the
live hub plist. The scratch cluster on port 55432 is outside this operation.

## Verified inventory

| Property | Verified value |
| --- | --- |
| Owner | `arnab`, data directory mode 0700 |
| Data directory | `/Users/arnab/.drover-postgres-fresh-test` |
| Live postmaster | PID 16988, parent PID 1 |
| Start time | September 26, 2026, 17:05:24 America/Vancouver |
| Running binary | `/opt/homebrew/Cellar/postgresql@17/17.11/bin/postgres` |
| Stable binary | `/opt/homebrew/opt/postgresql@17/bin/postgres`, resolves to the same binary |
| Startup arguments | `-D /Users/arnab/.drover-postgres-fresh-test`, confirmed by process and `postmaster.opts` |
| Version | PostgreSQL 17.11 (Homebrew) |
| Port/listen address | 15432, `127.0.0.1` |
| Unix socket directory | `/Users/arnab/.drover-postgres-fresh-test` |
| Configuration | `postgresql.conf` in the data directory |
| Authentication/ident | `pg_hba.conf` and `pg_ident.conf` in the data directory |
| Current stdout/stderr | `/Users/arnab/.drover-postgres-fresh-test/postgres.log`, confirmed with `lsof` |
| Logging | Collector off; destination stderr; configured log directory `log` and filename `postgresql-%Y-%m-%d_%H%M%S.log` are inactive |
| Supervision before switch | Manually started orphaned postmaster; matching `pg_ctl status` confirms running; no loaded Postgres agent |
| Administrator access | Local socket, role `arnab`, superuser; no password or DSN in command arguments |
| Hub | `com.drover.server`, PID 63808 at inspection, exit timeout 300 seconds |
| Hub health endpoints | `http://127.0.0.1:7080/healthz` and `/readyz` |

The historical launching shell cannot be recovered from the orphaned process.
Its exact PostgreSQL invocation and existing log destination are verified;
manual rollback reproduces them using the matching `pg_ctl`.

Six non-template databases were enumerated: `drover_fresh_test`, `drover_lake`,
`drover_lake_r5`, `drover_lake_v2`, `drover_v052_test`, and `postgres`. Backups
re-enumerate all non-template databases at execution and abort if any dump
fails. Database names outside simple alphanumeric/underscore names require an
explicit encoding update before execution; they are never silently skipped.

## Staged files and hub startup ordering

The private host files are:

- `~/Library/LaunchAgents/com.drover.postgres.plist`
- `~/.drover/maintenance/wait-for-postgres.sh`
- `~/.drover/maintenance/pg-switchover.sh`
- `~/.drover/maintenance/pg-rollback.sh`
- `~/.drover/maintenance/studio_pg_supervision.py`
- `~/.drover/maintenance/pg-supervision-prepared.json`
- `~/.drover/maintenance/pg-supervision.lock`

The agent directly runs the stable `postgres` executable with `-D` and
`-p 15432`, in the foreground. It has `KeepAlive=true`, `RunAtLoad=true`,
`ThrottleInterval=10`, `ExitTimeOut=300`, and absolute stdout/stderr paths
`~/Library/Logs/drover/postgres.out.log` and `postgres.err.log`. The existing
logging collector setting stays off, so these become the database log files.
The plist and state are mode 0600; scripts are mode 0700. Nothing was loaded.

The live hub plist is unchanged. It currently runs an environment-loading
`/bin/sh` wrapper, the private `server.env`, and the runtime's `drover-server
run`. During switchover, Python backs up its original bytes, parses the plist,
and changes exactly this dictionary entry:

```python
patched = dict(original)
patched["ProgramArguments"] = [
    "/Users/arnab/.drover/maintenance/wait-for-postgres.sh",
    *original["ProgramArguments"],
]
```

Every other dictionary entry is preserved, including environment, KeepAlive,
logs, RunAtLoad and ExitTimeOut. A `Program` key would bypass the wrapper, so
preflight rejects that unexpected shape rather than rewriting it. The wrapper
waits up to approximately 120 seconds for `pg_isready` on both TCP and the
verified socket, using one-second connection timeouts, then `exec "$@"`.
The original environment-loading command receives its original arguments.
This creates a real readiness dependency; launchd job labels do not establish
startup order. Installer updates can overwrite this host-specific hub change;
recheck it after any installer run.

The repository maintains the helper and entry scripts under
`scripts/operations/`. The installed helper is pinned to this host, owner,
cluster, binary version and port. Preparation pins SHA-256 hashes of the hub
plist, PostgreSQL plist, wait wrapper, soak probe and three configuration files.
Changed artifacts cause preflight to refuse execution. Included PostgreSQL
configuration files, if introduced later, need separate hash coverage.

`install.sh` provisions PostgreSQL only in managed-container mode, with Docker
`--restart unless-stopped`. It does not provision this native cluster, so no
native LaunchAgent installer change is appropriate. Existing/native clusters
remain operator-owned. The Docker runtime also needs its own reboot startup.

## Scheduled switchover commands

Safe preflight, with no service mutations or backup creation:

```sh
~/.drover/maintenance/pg-switchover.sh
```

Only in Arnab's scheduled maintenance window:

```sh
~/.drover/maintenance/pg-switchover.sh --execute
```

The command performs these steps in order, under a nonblocking exclusive lock:

1. Verify owner, hostname, exact cluster identity, stable binary resolution,
   original manual startup arguments, configuration hashes, both plist
   dictionaries, loaded hub, unloaded Postgres agent, health/readiness and the
   existing read-only `~/.drover/soak-probe.sh`.
2. Create a mode-0700 timestamped `pg-switch-*` directory. Save original hub
   bytes and durable stage markers in `pg-supervision-active.json`.
3. Dump globals with `pg_dumpall --globals-only` and each non-template database
   with `pg_dump --format=custom`. Use numeric archive names plus a private
   database-name manifest. Run `pg_restore --list` on each archive, require
   nonempty files, write SHA-256 hashes and recheck them. Any failure aborts
   before stopping either service. Dump timeouts are 600 seconds for globals
   and 3600 seconds per database.
4. Repeat preflight after backup. Record downtime start. Boot out
   `gui/UID/com.drover.server`, waiting for its process to exit cleanly.
5. Run the stable `pg_ctl -D DATA stop -m fast -w -t 300`. Verify the original
   postmaster exits, its PID file disappears and port 15432 closes.
6. Bootstrap `com.drover.postgres.plist` into `gui/UID`. Wait up to 120 seconds
   for TCP reachability, `pg_isready`, authenticated `SELECT 1`, exact data
   directory/port/role identity and launchd ownership of the postmaster PID.
7. Apply the backed-up hub's argument prefix atomically, lint the result, and
   bootstrap `com.drover.server`. Wait up to 300 seconds for HTTP 200,
   `ok` plus `analytical=ok`, and readiness JSON `ready=true`.
8. Run the existing soak probe twice, 30 seconds apart, also checking SQL
   identity, endpoint readiness, stable PostgreSQL/hub PIDs and stable launchd
   run counts. Suppress raw probe output; print only fixed pass/fail messages.
   Mark complete only after both probes pass.

The agent is never bootstrapped until the manual postmaster is confirmed gone.
The helper never uses immediate shutdown or process kills. On failure after a
stop it prints the rollback command and retains progress; it does not perform
an automatic data restore or automatic service rollback.

Backups precede downtime. Estimate 1 to 3 minutes for a healthy restart, with
30 seconds between soak probes. This is an unmeasured estimate. Hub checkpoint,
PostgreSQL shutdown and startup waits can extend the outage; schedule a
15-minute window plus rollback allowance. User-visible downtime ends when
both hub health and readiness pass, before the additional soak completes.
The private state records downtime start/end timestamps for measurement.

## Rollback commands

Verify the saved attempt without changing services:

```sh
~/.drover/maintenance/pg-rollback.sh
```

During the scheduled window, if switching or either soak check fails:

```sh
~/.drover/maintenance/pg-rollback.sh --execute
```

Rollback checks the saved hub checksum, binary and configuration hashes. A
backup-only failure causes no restart. Otherwise it verifies the current
cluster identity, stops the hub, boots out the supervised PostgreSQL job and
waits for its postmaster to exit before starting the original manual invocation
with `pg_ctl -D DATA -l DATA/postgres.log start -w -t 120`. If the original
manual server survived a partial switch it reuses that verified process.
It restores the original hub bytes atomically, bootstraps the hub, waits for
health/readiness and performs both soak checks. The new Postgres plist moves
into the attempt directory as `com.drover.postgres.disabled.plist`, preventing
an accidental login-time start after rollback. A completed rollback is a no-op
on another execution. A partially interrupted rollback resumes only with
verified PIDs; unexpected processes cause refusal and need operator review.

Rollback changes supervision only and reuses current cluster data. Globals
contain role password hashes: backups are private and must never be printed
or committed. These logical backups are individually consistent, not a
coordinated cross-database recovery point, and do not include external DuckLake
data files. Restoring dumps is separate coordinated recovery work. Keep the
original plist, manifests, checksums and backups. Archive the active-state file
only after operator review before preparing a new attempt; another switch
refuses to overwrite an existing attempt.

## Validation and remaining gates

Preparation passed `plutil -lint`, exact Python `plistlib` dictionary checks,
`zsh -n` on both entry scripts and the wait wrapper, read-only preflight and
two baseline soak probes. Failure-path unit tests run with all service actions
mocked. The live hub plist hash remained identical, original PostgreSQL PID
16988 remained running and the new Postgres job remained unloaded.

Actual backup creation, service transfer, rollback execution and reboot testing
are reserved for the scheduled window. A per-user LaunchAgent runs after that
user logs in, not before login after a reboot, including a FileVault reboot.
Verify the Studio's login arrangement then. Pre-login unattended availability
requires separately approved LaunchDaemon or login configuration work.
