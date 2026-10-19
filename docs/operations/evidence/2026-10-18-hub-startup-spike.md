# Studio 0.6.23 startup footprint investigation

Root cause: provider-refresh advisory enqueue closes the last handle to the
legacy `/Volumes/M2 1/drover-data/drover.duckdb`, repeatedly running DuckDB's
whole-file shutdown checkpoint and reopen because #702 forces the analytical
pin only when `retire_legacy_writers=true`, whereas Studio selects DuckLake
with that setting omitted (default false).

## Existing production evidence (read before changing code)

Source: Studio launchd `com.drover.server` stderr,
`~/Library/Logs/drover/server.err.log`, 2026-10-18, PID 60660, runtime 0.6.23.
The startup phases show `pin_analytical_connection`, not
`pin_retired_analytical_connection`; bootstrap completes at 126,976,912 bytes.
The material growth happens after provider refresh starts.

Peak window, verbatim excerpts (bytes, `phys_footprint`, not RSS):

```text
18:23:14 INFO    drover.runtime_observability: hub_startup_footprint footprint=2042547056 footprint_delta=1495680248 active=[{'kind': 'background', 'name': 'advisory_sweep'}, {'kind': 'background', 'name': 'cockpit_warmup'}]
18:23:16 INFO    drover.runtime_observability: hub_startup_footprint footprint=3517665192 footprint_delta=1475118136 active=[{'kind': 'background', 'name': 'advisory_sweep'}]
18:23:23 INFO    drover.runtime_observability: hub_startup_footprint footprint=3631206384 footprint_delta=113524808 active=[{'kind': 'background', 'name': 'advisory_sweep'}]
18:23:24 WARNING drover.runtime_observability: hub_startup_native_growth footprint=3631206384 footprint_delta=113524808 outside_activities=False active=[{'kind': 'background', 'name': 'advisory_sweep'}] native_threads=79 native_frames=duckdb_task_scheduler,duckdb_checkpoint_reader,arrow_or_parquet
18:23:24 INFO    drover.runtime_observability: hub_activity end kind=background name=ducklake_export_batch footprint=4487828064 footprint_delta=148684920 duration_s=0.012
18:23:25 INFO    drover.runtime_observability: hub_startup_footprint footprint=748573760 footprint_delta=-2882632624 active=[{'kind': 'background', 'name': 'advisory_sweep'}]
```

The 4,487,828,064-byte reading is **4.180 GiB**, the reported failed gate.
An activity's process-wide delta is not allocation ownership: the exporter
thread is waiting for a disposable query child in the Python dump.

The sampler also retained full native reports in `/tmp`. At
`python_2026-10-18_182311_JQMH.sample.txt`, thread `drover-provider-refresh`,
483 sampled stacks include `DuckDBPyConnection::Close`; 475 continue through:

```text
DuckDBPyConnection::Close
DuckDB::~DuckDB
DatabaseInstance::~DatabaseInstance
DatabaseManager::ResetDatabases
AttachedDatabase::Close
SingleFileStorageManager::CreateCheckpoint
SingleFileCheckpointWriter::CreateCheckpoint
SingleFileCheckpointWriter::WriteTable
DataTable::Checkpoint
RowGroupCollection::Checkpoint
RowGroup::WriteToDisk
RowGroup::CheckpointColumn
StandardColumnData::Checkpoint
ColumnDataCheckpointer::WriteToDisk
ColumnDataCheckpointer::ScanSegments
FSSTStorage::Compress
```

The Python watchdog dump at 18:23:12 places that same thread at
`ProviderRefreshLoop.run_once -> on_operational_change ->
enqueue_operational_checks -> enqueue_advisory_check -> con.close()`.
`enqueue_advisory_check` opens `cfg.duckdb_path`, not the PostgreSQL control
store or a lake query child. It writes `pipeline_receipts` / `pipeline_jobs`;
the native checkpoint stack does not identify which table it is checkpointing,
so no claim is made that those small ledger rows themselves occupy the peak.
At 18:23:23, the native stack additionally shows `Connect -> AttachDatabase ->
LoadDatabase -> CheckpointReader::LoadCheckpoint`, corroborating instance churn.

The broad `arrow_or_parquet` signature includes idle Arrow worker threads;
it does not establish an active Parquet allocation. These reports identify
shutdown checkpoint/reopen, not WAL replay, a lake attach, or view binding,
as the demonstrated allocation path. The 18:23:27 native report's own peak
header reads `4.8G`; that is a separate tool reading from the exact-byte
4.180 GiB log/gate observation above.

## Fix and regression coverage

Force the existing bounded analytical pin for DuckLake selection before
bootstrap and workers, independently of legacy-writer retirement. Keep legacy
selection's explicit opt-in policy. Reuse the existing atomic snapshot and
shutdown/recovery machinery.

The startup regression test exercises the actual CLI branch for legacy,
DuckLake before retirement (the missed Studio configuration), and DuckLake
after retirement. It asserts the pin precedes bootstrap/worker startup and
that both DuckLake configurations force it without the environment opt-in.

## Isolated reproduction

Both runs used the installed 0.6.23 Python 3.14.7 / DuckDB 1.5.5 runtime,
with only the fixed run's `PYTHONPATH` pointed at this branch. Scratch state:
`/Volumes/M2 1/drover-spike-scratch-20261018/`. A stable atomic DB/WAL clone,
Parquet/lake copies, and read-only `pg_dump` snapshots restored into separate
`drover_spike_20261018_control` / `drover_spike_20261018_lake` databases kept
all writes off production. The copied lake attestation and catalog data root
were relocated to the copy; catalog identity and data were retained.

The copied production config kept DuckLake selected and writer retirement
false, with local paths redirected and HTTP on loopback port 17080. Both runs
disabled outbound push/model workers and MCP/OTLP listeners. A shared safety
wrapper blocked external/fleet socket connections and replayed the real
provider-to-advisory enqueue for copied hosts locally (their saved liveness
had expired), rather than fetching provider data from the fleet. Each run
started from the same legacy DB snapshot.

An external observer sampled physical footprint at one-second intervals from
process creation through 120 seconds (121 readings) and read the kernel's
lifetime high-water mark, so sub-second spikes also count. The built-in
one-second startup logger/native-growth sampler ran as well. Measurements:

| Build | First 120 s kernel peak | Peak through all requests |
| --- | ---: | ---: |
| Unmodified 0.6.23 | 3,670,347,928 B (3.418 GiB) | 3,670,347,928 B (3.418 GiB) |
| Fixed | 599,049,680 B (0.558 GiB) | 720,504,392 B (0.671 GiB) |

The unmodified copy reproduced `duckdb_checkpoint` and
`duckdb_checkpoint_reader` growth signatures. The fixed run logged
`analytical store pinned for process lifetime` before bootstrap; its material
growth signatures showed scheduler/Arrow frames without the shutdown
checkpoint/reopen signatures seen in the baseline.

All four requests returned 200 in each run:
`/analytics?days=365&metric=tokens`,
`/analytics?days=365&metric=sessions`,
`/cockpit/overview?days=365`, and `/insights`. The analytics activity sections
reported `status=ok`. The fixed kernel peak remained below the 1.5 GiB cap
through the entire startup/request sequence. This is isolated proof; the
orchestrator still needs to run the live release gate.

The [measurement summary](2026-10-18-hub-startup-spike-measurements.json)
contains exact peaks and request results. Raw per-second measurements, logs,
full native samples, DB dumps/copies, and private safety/measurement scripts
were retained in the scratch directory for operator inspection. Private
response payloads and service credentials are not committed.

Validation: 75 startup/DB/memory/observability tests passed; 135
recovery/advisory/routing tests passed, 12 platform-dependent skips. Black,
isort and `git diff --check` passed.

Production PID 60660 stayed on 0.6.23 without a restart or rollback; the NAS
was not contacted and no release was created.
