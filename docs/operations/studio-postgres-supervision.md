# Studio native PostgreSQL supervision

Issue #583: the Studio cluster on port 15432 serves both the Drover control
store and the DuckLake v2 catalog. The operator reports that it was started
manually on September 26 and has no LaunchAgent. A scheduled maintenance
window must transfer it to launchd; never load a second postmaster against a
running data directory.

## Preparation status, October 12, 2026

This branch's preparation attempt could not access `ssh drover-studio`: the
name did not resolve in the Studio workspace, and localhost SSH rejected the
available public key. No cluster inspection, service stop, bootstrap, or hub
restart was performed. Binary, data directory, configuration, logging, owner,
and startup flags remain unverified. Do not infer them from Homebrew defaults.
The host plist and maintenance scripts remain to be prepared after SSH access
is restored or local preparation is explicitly authorized.

The installer in `install.sh` provisions PostgreSQL only in managed-container
mode. `scripts/lib/managed_postgres.sh` uses Docker `--restart unless-stopped`.
Existing/native PostgreSQL is operator-owned. Installing a native LaunchAgent
from that installer would incorrectly claim an external cluster; this Studio
setup is an explicit operator maintenance operation. The Docker runtime must
also return after reboot. Do not run the installer as a substitute for this
maintenance operation: it starts services unless configured otherwise and can
rewrite hub service definitions.

## Read-only inventory

Run through `ssh drover-studio` as the cluster owner. Inspect process owner,
PID, parent PID, executable, start time, and only the PostgreSQL process's
startup arguments. Never dump the whole process environment, server.env,
config, hub plist, or unrestricted process arguments, because they may contain
passwords or DSNs. Use the discovered matching `pg_ctl -D DATA status`.

Using the matching `psql`, local socket access and the administrator role,
query only these settings: `data_directory`, `config_file`, `hba_file`,
`ident_file`, `port`, `listen_addresses`, `unix_socket_directories`,
`logging_collector`, `log_destination`, `log_directory`, `log_filename`,
`server_version`. Check `postmaster.opts` privately for startup overrides,
resolve the actual executable, and check data directory ownership. Inspect
open stdout/stderr descriptors to locate the current `pg_ctl -l` log. Verify
all discovered paths and options before writing the agent.

## Prepared service definitions

Write `~/Library/LaunchAgents/com.drover.postgres.plist` with `plistlib`, using
absolute paths and the same binary, data directory, port and any verified
startup overrides. Its `ProgramArguments` must run `postgres -D DATA -p 15432`
in the foreground. Set `Label=com.drover.postgres`, `RunAtLoad=true`,
`KeepAlive=true`, `ThrottleInterval=10`, and a generous `ExitTimeOut=300`.
Set stdout/stderr to `~/Library/Logs/drover/postgres.out.log` and
`postgres.err.log` with expanded absolute paths. Preserve existing PostgreSQL
collector settings, which may send database logs elsewhere.

Back up the original hub plist privately under `~/.drover/maintenance/`.
Parse its dictionary and preserve every entry except `ProgramArguments`:
prefix the existing argument array with a wait wrapper. If it has `Program`,
account for launchd's executable selection explicitly rather than letting it
bypass the wrapper. The wrapper must wait, with a bounded timeout, for the
verified PostgreSQL endpoint to accept connections and for matching
`pg_isready` to report ready, then `exec` the original argument vector without
shell interpolation. Preserve the existing environment-loading wrapper.
launchd does not impose an order merely from job labels or `RunAtLoad`.
The live job continues using its loaded definition until the scheduled reload.
Future installer runs can overwrite this host-specific wrapper; recheck it
after any installer update.

Validate both plists with `plutil -lint`. Independently use `plistlib.load` to
assert the dictionaries, exact Postgres arguments, booleans, absolute log
paths, and equality of all retained hub keys to the original. Use `zsh -n`
on all maintenance shell scripts. Safe dry-run mode must perform only inventory,
validation and read-only readiness probes. Do not bootstrap/load the new agent
or stop either service during preparation.

## Scheduled switchover

Prepare owner-only `~/.drover/maintenance/pg-switchover.sh` and
`pg-rollback.sh` with the inspected paths fixed, a single-run lock, explicit
execution mode and durable progress markers. Default invocation should be a
read-only preflight. Do not print credentials or complete DSNs. Fail closed
before any stop if a backup or preflight check fails; after a stop, report the
last completed stage and the rollback command without automatically restoring
logical dumps.

The switchover execution order is:

1. Verify cluster identity, owner, matching tools, the running hub, unloaded
   Postgres agent, both plist dictionaries and readiness. Save the original
   hub definition and exact manual PostgreSQL start invocation privately.
2. Create a unique owner-only backup directory. Run `pg_dumpall --globals-only`
   into a private file and `pg_dump --format=custom` for every non-template
   database, including the control store, catalog and `postgres` database.
   Enumerate names safely, use numeric filenames with a private database-name
   manifest, and include all databases even if the administrator lacks access:
   inability to dump any database must abort. Never place credentials in argv.
3. Require success and nonempty backups; run `pg_restore --list` on every
   custom archive, write SHA-256 checksums, and verify them. Globals can contain
   role password hashes, so do not display them or commit any backup. Logical
   backups taken before the hub stops are individually consistent, but are not
   a coordinated cross-database recovery point. They also do not back up
   external DuckLake data files. This switch reuses the same cluster and does
   not restore or migrate data.
4. Record the downtime start. `launchctl bootout gui/UID/com.drover.server`
   and verify the hub process exits before proceeding. Never rely on `stop`
   with a KeepAlive job because launchd can immediately restart it. Allow the
   existing hub `ExitTimeOut` for its clean checkpoint.
5. Use the discovered matching `pg_ctl -D DATA stop -m fast -w -t 300`.
   Verify the postmaster exits and port 15432 closes. Do not proceed on timeout,
   use immediate shutdown, or kill a PostgreSQL process.
6. `launchctl bootstrap gui/UID ~/Library/LaunchAgents/com.drover.postgres.plist`.
   Wait up to 120 seconds for TCP reachability, `pg_isready`, authenticated
   `SELECT 1`, and SQL identity checks for the same data directory and port.
7. Bootstrap the prepared hub plist. Within 300 seconds require `/healthz`
   and `/readyz` HTTP 200, expected analytical health and `ready=true`.
8. Run a bounded read-only control-store/catalog/analytical soak probe twice,
   separated by 30 seconds. Check agent PID stability and increasing restart
   counts as well as endpoint responses. Store only sanitized probe results.
   Mark success only after both probes pass.

Backups occur before downtime. Budget 1 to 3 minutes for a healthy restart,
plus 30 seconds between soak probes, but this is an estimate, not a measured
Studio result. The hub's checkpoint may take up to its 300-second exit timeout;
PostgreSQL stop, readiness and hub startup bounds can extend the outage. End
user-visible downtime only when the hub passes both health and readiness.
Schedule a 15-minute window with additional rollback time.

## Rollback

`pg-rollback.sh` must use the saved state from this attempt, refuse an unrelated
cluster, and do nothing destructive when invoked in dry-run mode. Stop the hub
if loaded, then boot out the supervised PostgreSQL agent if loaded and wait
for its process to exit. Verify neither a supervised nor manual postmaster
remains before restarting the original manual invocation with the matching
`pg_ctl`, original options and log path. If shutdown is not clean or cannot be
verified, abort and leave the hub down for operator investigation. Restore the
original hub plist dictionary atomically, bootstrap it, and repeat database
identity, health/readiness and both soak checks. Do not use `pg_ctl stop` on a
loaded KeepAlive Postgres agent without booting it out first.

Rollback changes supervision only, preserving the cluster's current data.
Restoring dumps is a separate recovery operation requiring coordinated
DuckLake file/catalog recovery. Leave the new Postgres plist outside
`~/Library/LaunchAgents` after rollback so login cannot accidentally start a
second postmaster. Retain backups, manifests and checksums privately.

## Reboot boundary

A per-user LaunchAgent runs when that user logs in. It does not provide
unattended startup before login after a reboot, including a FileVault reboot.
Verify the Studio's login arrangement in the scheduled window. If service is
required before user login, a LaunchDaemon or an explicitly approved login
arrangement is separate work. The scheduled switch and a reboot test are not
authorized by preparation alone.
