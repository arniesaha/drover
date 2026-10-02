# PostgreSQL control store

Fresh central Drover installations use PostgreSQL for their control store.
They can run the public API and analytical work as separate processes while
each harness host continues to keep its own local DuckDB spool. Existing
configurations that omit `[control_store]` keep their established DuckDB
control store until an operator performs an explicit migration.

This guide is for an operator preparing a new central store or an offline
cutover. It does not perform a live migration, change a running service, or
turn a local harness daemon into a PostgreSQL client.

## Operator prerequisites and ownership

PostgreSQL has two first-class central-server modes: **existing DSN**, which remains entirely operator-owned, and **managed container** for a fresh trusted server. Drover never installs a container runtime. In managed mode it owns one labeled PostgreSQL 17 container and named volume, but the local container-runtime operator can inspect container environment metadata and therefore remains inside the trust boundary. Drover still does not manage host runtime upgrades, capacity, or recovery policy. Before installing a fresh
central server, the operator must provide an empty dedicated database and
dedicated login role, keep the database listener loopback-only or on a private
network, and decide how PostgreSQL is started and monitored after reboot.

For a disposable readiness test, use a separate user-owned cluster, role, and
database from production. Bind that test cluster to loopback only and discard
it after the test. A production store needs its own lifecycle, capacity,
upgrade, backup, restore, and access-control procedure; it is not a
long-lived version of the readiness test.

On macOS, an operator can use a user-managed PostgreSQL installation and keep
its data directory outside Drover state. On Linux, an operator can use the
distribution-managed PostgreSQL service or another explicitly managed service.
In either case, restrict `listen_addresses`, host firewall rules, and
`pg_hba.conf` to loopback or the intended private network before exposing
Drover to a phone or another host. Do not bind PostgreSQL publicly for Drover.

Create the role and database using the PostgreSQL administration procedure for
the chosen installation. Keep the role scoped to its dedicated database and do
not use a superuser DSN for Drover. Put the resulting DSN in the process
environment or a secret manager, never in `config.toml`, a shell history,
service arguments, screenshots, or repository files. The installer reads
`DROVER_CONTROL_DSN` once and writes an owner-only service environment file.
It does not print the DSN.

Before committing installation mutations, run:

```sh
install.sh --verify-release
```

This downloads the selected public release, verifies the wheel and lockfile
against `SHA256SUMS.txt`, and starts a disposable local runtime to prove the
PostgreSQL-default installer contract. It does not contact the database or
write `~/.drover`; `install.sh --dry-run` remains the offline action preview.


### Managed-container lifecycle

`install.sh --control-store managed` uses the immutable multi-architecture `postgres:17.6` manifest digest `sha256:00bc86618629af00d2937fdc5a5d63db3ff8450acf52f0636ec813c7f4902929` (amd64 and arm64). Image updates are a reviewed source change: update the version and digest together after `docker buildx imagetools inspect`, run managed integration tests on both available architectures, and document the new digest in the release notes. It uses `--restart unless-stopped`, a loopback-only published port, and labels both container and `drover-postgres-data` volume. A collision with an unlabeled or foreign object fails rather than adopting it.

Credentials are generated locally and saved only as the DSN in `~/.drover/server.env` (0600). Initialization needs an ephemeral private env file; credentials are never printed or passed in argv. Container operators can inspect a running container, so do not grant Docker/OrbStack access to untrusted users.

Use the installed owner-only helper for diagnostics and safe lifecycle actions:

```sh
~/.drover/bin/drover-managed-postgres status
~/.drover/bin/drover-managed-postgres stop
~/.drover/bin/drover-managed-postgres start
~/.drover/bin/drover-managed-postgres backup /absolute/path/outside-drover-data/drover-$(date +%F).dump
```

The backup command makes a PostgreSQL custom-format logical dump and verifies/list it with `pg_restore -l`; the destination must be outside the live volume. A volume is **not** a backup. Restore is intentionally a manual, stopped-server `pg_restore` procedure, so it cannot accidentally overwrite a live database. Normal Drover uninstall/upgrade preserves the labeled container and volume. Destruction is separate: `drover-managed-postgres purge --i-understand-this-deletes-drover-postgres-data`. Restore only after stopping native Drover: `drover-managed-postgres restore /absolute/path/backup.dump --i-understand-this-overwrites-drover`.

## Install and configure

The PostgreSQL client is part of the default central-server dependency set.
Install the project normally in the environment that runs the central server:

```sh
uv sync
```

The release installer includes the same dependency in its hash-pinned
requirements export. The `postgres` extra remains available for compatibility
with existing source-install commands.

Put the connection string in a named environment variable. The configuration
contains that variable's name, never the connection string itself.

For a fresh source installation, generate the PostgreSQL configuration first,
then explicitly initialize its empty target:

```sh
export DROVER_CONTROL_DSN='postgresql://USER:PASSWORD@HOST/DATABASE'
uv run drover-server init
uv run drover-server control-store init
uv run drover-server control-store status
```

`init` writes the configuration only. `control-store init` connects to the
configured PostgreSQL target, bootstraps its schema, and writes its empty-store
readiness marker. Do not start a serving role until `status` reports
`"ready": true`.

For a fresh legacy deployment, make the exception explicit instead:

```sh
uv run drover-server init --control-store duckdb
```

The command never rewrites an existing config. A missing central config is an
error rather than permission to create a DuckDB store. Keep existing DuckDB
configs unchanged until their offline migration is ready.

```toml
[paths]
incoming_dir = "incoming"
parquet_dir = "parquet"
duckdb_path = "central-selector.duckdb"

[control_store]
backend = "postgres"
dsn_env = "DROVER_CONTROL_DSN"
pool_min_size = 1
pool_max_size = 2
acquire_timeout_seconds = 2.0
statement_timeout_seconds = 5.0
schema = "drover_control"

[runtime]
# all retains the established combined process. Use the command-line role
# override below when launching separate API and analytics processes.
role = "all"

[analytics_boundary]
worker_url = "http://127.0.0.1:7082"
api_url = "http://127.0.0.1:7080"
api_to_worker_token_env = "DROVER_API_TO_ANALYTICS_TOKEN"
worker_to_api_token_env = "DROVER_ANALYTICS_TO_API_TOKEN"
connect_timeout_seconds = 0.25
request_timeout_seconds = 10.0
max_request_bytes = 262144
max_response_bytes = 4194304
max_concurrent_requests = 8
```

`duckdb_path` remains a path-scoped selector for the central control store.
When `backend = "postgres"`, Drover uses its configuration and does not
create a DuckDB control database at that path. The analytics role still uses
its configured DuckDB path for analytical views and derived context.

Use distinct tokens for the two loopback-only directions. The API token and
the two boundary tokens belong in the service environment or another secret
manager, not in the TOML file. The boundary rejects non-loopback URLs, browser
credentials, unknown routes, oversized bodies, and calls that exceed its total
deadline.

Pool sizes apply per process. Size the API, analytics worker, administration,
and test pools together, leaving capacity for maintenance. The example keeps
each role at two connections. The boundary's concurrent-request limit should
also be no larger than the work its worker pool and response-size limit can
serve. The control store applies an acquisition timeout and a PostgreSQL
statement timeout to each pooled connection.

The analytics-owned exporter currently uses fixed bounded settings: 100 events
per batch, a 5 second idle flush age, a 1 second polling interval, a 60 second
lease, and 100 retention candidates per pass. It reports pending, claimed,
published-unacknowledged, oldest outstanding work, retention results, and its
last error through analytics health. Those are implementation settings, not a
separate export CLI or a promise of unlimited throughput.

## Run combined or split roles

The generated PostgreSQL config supports the combined process:

```sh
uv run drover-server --config central.toml run
```

Separate roles also require the PostgreSQL configuration above. Start the API and
analytics roles with the same central configuration and the generated service
environment tokens:

```sh
uv run drover-server --config central.toml run --role api
uv run drover-server --config central.toml run --role analytics
```

The API role owns authenticated fleet and session serving, pairing, relay,
push, control readiness, and central content-consent reads. It does not open
the analytics lake. The analytics role owns ingestion, maintenance, MCP and
OTLP listeners, derived jobs, archive resolution, usage and recap work, and
the durable outbox exporter. A worker outage leaves a ready control API able to
serve `/harness`; analytical routes return an explicit unavailable response
until the worker returns. `drover-harnessd` is unchanged and retains its
host-local DuckDB spool even when its environment contains a central DSN.

## Installer behavior, empty stores, and offline cutover

For a new central installation, export `DROVER_CONTROL_DSN` before running the
installer. It writes a mode `0600` service environment file and references that
file from the server's launchd or systemd definition. The DSN value is absent
from the TOML configuration and service arguments. The installer validates and
initializes a fresh empty PostgreSQL target before it starts the server. A
missing DSN fails before it writes the fresh config. An initialization failure
leaves the generated config for remediation but starts no service.

When the installer is run again with an existing PostgreSQL configuration, it
loads the private environment file and checks `control-store status`; it does
not initialize the target again. A migrated ready target can therefore retain
its fenced DuckDB source files. An interrupted import remains unready until its
explicit import and verification sequence finishes.

For a deliberate empty central store, initialize it offline:

```sh
uv run drover-server --config central.toml control-store init
uv run drover-server --config central.toml control-store status
```

For an existing local control store, stop writers and take a read-only fenced
source snapshot before creating the PostgreSQL target. Supply the timezone for
legacy naive timestamp columns and, when needed, an explicit credential and
server-identity document:

```sh
uv run drover-server --config central.toml control-store import \
  --source-snapshot FENCED_CONTROL_SNAPSHOT.duckdb \
  --source-timezone America/Los_Angeles \
  --credentials FENCED_CREDENTIALS.json

uv run drover-server --config central.toml control-store verify \
  --source-snapshot FENCED_CONTROL_SNAPSHOT.duckdb \
  --source-timezone America/Los_Angeles \
  --credentials FENCED_CREDENTIALS.json
```

`import` and `verify` never discover a live source. Bootstrap alone is not
readiness. Do not start a serving role until `status` reports `ready: true` and
the offline verification succeeds. Migration streams event history but
materializes non-event administrative relations. Preserve the fenced source
snapshot as the audit record for their source history.

### Content-consent cutover

The first PostgreSQL role startup writes the singleton central consent row.
Before that startup, preserve the original config and its matching companion
`.<config filename>.content-consent.json` artifact. Prefer one shared config
and `--role` overrides for the first split launch. The initializer imports an
enabled legacy gate only when its configured scope is also enabled. Missing or
invalid legacy state becomes disabled, local, epoch zero consent.

The first insert wins. Starting a different scratch configuration first writes
the fail-closed central row, and starting later with the original companion
artifact does not import it again. Inspect the central state through the
authenticated content-analysis surface after startup. Re-enable or revoke
content analysis through the supported authenticated consent endpoints; do not
edit the central row or copy a companion artifact into a running store.

## Retention and backup

The central store has narrow event metadata and payload projections. The
analytics exporter publishes immutable Parquet batches from its durable
PostgreSQL manifest, then retention removes a hot payload only after the batch
is acknowledged, usage is exact, terminal recap dependencies are complete, and
archive bytes verify by hash. A missing archive remains explicit unavailable
history. It is never represented as an empty payload or a missing session.

After retention, a cold page can require one authenticated archive resolver
call per event. The API gives the whole page one aggregate boundary deadline,
rather than a separate full timeout for each event. Operators should keep page
sizes and boundary limits within the configured request and response caps.

After any payload is pruned, a PostgreSQL dump alone cannot restore complete
history. A consistent backup procedure fences API and worker writes, records
the acknowledged manifest boundary, captures the PostgreSQL state with
[`pg_dump`](https://www.postgresql.org/docs/17/backup-dump.html), and copies
the immutable archive files referenced by that manifest. Take the database dump
and archive copy while the fence remains in place. A filesystem copy followed
by a later database dump is not a consistent cross-store backup while export is
active.

Use a custom dump format when an operator needs `pg_restore`. PostgreSQL global
objects are outside a per-database dump and need their own operator procedure.
PostgreSQL base backup and WAL recovery are separate operational work described
by the [continuous archiving documentation](https://www.postgresql.org/docs/17/continuous-archiving.html).
They are not configured or proven by Drover's bounded recovery validation.
Remote production connections need an explicit TLS policy; see PostgreSQL's
[SSL support documentation](https://www.postgresql.org/docs/17/libpq-ssl.html).

Restore archive files to the original location recorded in the published
manifest and configure the worker with that matching Parquet root. Analytical
relation registration reads the manifest `archive_path`, while the local
verified resolver reconstructs a batch path from its configured Parquet root.
Arbitrary worker filesystem relocation is not a supported restore operation.
After restoring both stores, start an analytics worker to verify and register
the manifest, then perform an authenticated fleet smoke request. Do not use
`control-store verify` against the original legacy snapshot after PostgreSQL
has accepted writes: a correct forward-moving target will differ from it.

## Interrupted import and forward recovery

`control-store recovery` is inspection-only:

```sh
uv run drover-server --config central.toml control-store recovery
```

If a process dies after import admission, the original target stays
`initializing` and unavailable. Preserve it for diagnosis. Before PostgreSQL
accepts serving writes, create a fresh explicitly configured target schema and
repeat `import` and `verify` with the same fenced source snapshot, credential
document, and source timezone. Do not clear the marker and do not point the
failed target back at a live old DuckDB file.

After PostgreSQL accepts writes, recovery is forward-only. Restore PostgreSQL
and the immutable archive files required by the chosen recovery point, then
replay forward as the operator's backup procedure supports. WAL alone does not
recover pruned archive payloads. The validated procedure covers an interrupted
pre-cutover import, a PostgreSQL-only restore that explicitly fails cold
payload lookup, and a paired restore at the original archive path. It does not
prove PITR, TLS, power-loss durability, capacity, or a production cutover.

## Workload validation before cutover

The unconditional `pgvector-memory-integration` CI job tests actual vector writes,
cosine similarity reads, stale-generation exclusion, the job ledger and memory
integrity. It uses `pgvector/pgvector:0.8.6-pg17-bookworm` pinned to the multi-platform
digest `sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f`.
`--require-pgvector` requires an explicit test DSN, asserts that
`CREATE EXTENSION IF NOT EXISTS vector` succeeds and fails on any skipped test.
The separate `postgres-integration` job keeps plain PostgreSQL coverage, including
the missing-extension contracts.

Reproduce the vector gate from a source checkout with Docker and uv installed:

```sh
bash scripts/test_pgvector_memory.sh
```

This creates a uniquely named container with an ephemeral loopback port, overrides
any inherited test DSN, runs the locked dependencies and foreground tests, and
removes only that container and its anonymous volume on exit. No live stores or
production credentials are needed. Repository branch-protection settings must
require the `pgvector-memory-integration` check to prevent merging a failed gate.

Point `DROVER_TEST_POSTGRES_DSN` at a **disposable PostgreSQL instance**, then
run the synthetic workload from a source checkout:

```sh
uv run --extra postgres python scripts/benchmark_postgres_control_plane.py \
  --hosts 4 --sessions 200 --events 75000 --phase-requests 5000 \
  --drain-timeout-seconds 3600 \
  --output /tmp/drover-postgres-benchmark.json \
  --work-root /tmp/drover-postgres-benchmark
```

This starts isolated API and analytics processes and exercises worker outage,
export and verified retention. Allow tens of minutes for the default worker
to drain the fixture. Ingestion uses direct registry writes, so this does not
measure host-relay or HTTP ingestion throughput. Startup is measured before
event ingestion; recap completion uses synthetic receipts without LLM calls.

Read the API latency/error distributions, background sampling-error counts,
and export/retention duration together. A transient background sampling
failure can be retried within the overall deadline; API response errors fail
the run. The result is synthetic evidence, not a production capacity estimate.
Before cutover, compare the expected event arrival rate with the worker's drain
rate and monitor outstanding export age. The current batch, polling and
retention limits are fixed implementation values, not configuration options.
