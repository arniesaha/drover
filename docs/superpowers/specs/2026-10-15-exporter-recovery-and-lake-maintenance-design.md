# Exporter Recovery and Lake Maintenance Design

## Purpose and constraints

The DuckLake exporter must not leave the Studio hub running with export stopped
after in-process recovery is exhausted.  The live catalog has accumulated
8,560 snapshots and 42,792 catalog data-file rows in nine days, so the repair
must also provide an explicit, fenced operator maintenance path.  The exporter
must retain its least-privilege role: DuckLake 1.5.5 mutable tables remain
non-inlined and the maintenance path uses the catalog administrator only.

Live production state is inspection-only.  Tests and measurements use a
disposable PostgreSQL database and role plus a temporary lake root, on DuckDB
and DuckLake 1.5.5.

## Recovery design

Add a small durable control-store ledger for exporter-requested process exits.
On exhausted in-process recovery (or a stopped exporter past its deadline),
the lifecycle atomically records the request and counts requests in the
preceding hour.  Up to three requests per rolling hour set the shared server
stop event.  The foreground command completes ordinary cleanup and exits with
status 75, which launchd's `KeepAlive` restarts.

When the fourth request would occur in that hour, the lifecycle stays up,
marks the exporter stopped and crash-loop guarded, and reports the rolling
count and next allowed restart time through readiness/doctor.  It does not
request another exit.  This makes the failure visible without converting a
catalog problem into an unbounded launchd loop.  The counter is durable across
server restarts, so it cannot be bypassed by the process replacement itself.

## Maintenance design

Add `drover-server lake maintain`, an explicit admin CLI.  It acquires the
existing `drained_mutation` fence (exclusive mutation plus drained readers),
then uses an isolated DuckLake 1.5.5 admin connection to merge adjacent files
for the five exporter tables.  Only after all merge calls succeed does it
expire snapshots using the already-provisioned 30-day option and clean files
that DuckLake has scheduled for deletion.  The command is never scheduled or
run by the hub; a launchd timer is an operator-owned follow-up invoking this
CLI while the hub is stopped.

The command reports per-table merge results, before/after catalog file counts,
snapshot count, and elapsed time.  It does not grant privileges, provision
tables, change export storage options, or infer a data root/DSN from hub
configuration.  A `--dry-run` reports the intended operations without opening
the catalog.

## Tests and documentation

Lifecycle tests cover the rolling exit allowance, durable crash-loop guard,
and non-zero foreground exit selection.  Integration tests create thousands
of small Parquet files and snapshots in a temporary lake, run maintenance, and
capture merge latency and peak child RSS before and after compaction.  Operator
documentation gives the first manual run and a weekly timer example, with the
hub stopped for the fenced operation.
