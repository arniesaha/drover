# PostgreSQL control store

Drover normally keeps its control state in local DuckDB and starts with the
legacy combined `drover-server run` command. PostgreSQL is an explicit option
for a central control store. It allows the public API and analytical work to
run as separate processes while each harness host continues to keep its own
local DuckDB spool.

This guide is for an operator preparing a new central store or an offline
cutover. It does not perform a live migration, change a running service, or
turn a local harness daemon into a PostgreSQL client.

## Install and configure

Install the optional PostgreSQL client dependency in the environment that
runs the central server:

```sh
uv sync --extra postgres
```

Put the connection string in a named environment variable. The configuration
contains that variable's name, never the connection string itself.

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

The legacy default remains:

```sh
uv run drover-server --config central.toml run
```

Separate roles require the PostgreSQL configuration above. Start the API and
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

## New empty store and offline cutover

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
