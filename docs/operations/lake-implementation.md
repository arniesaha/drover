# Phase 4 implementation checkpoint

The backend is still legacy. These primitives are not a cutover-ready backend.
No live configuration, catalog, data root, or services were changed.

## Runtime pins

DuckDB is exactly 1.5.5 (`uv.lock` records wheel SHA-256 digests). DuckLake and
postgres_scanner decompressed artifact SHA-256 digests are in
`server/lake/runtime.py`, for osx_arm64 and linux_amd64. They were fetched from
`https://extensions.duckdb.org/v1.5.5/<platform>/<name>.duckdb_extension.gz`.
The macOS artifacts report DuckLake `d8a1881e` and postgres_scanner `41223e5`,
matching the spike. Runtime downloads and automatic catalog migrations are off.

`LakeSpec.engine_sha256` must come from the installer’s independently verified
wheel, not an on-startup digest enrollment. The tested Python 3.14 macOS arm64
`_duckdb.cpython-314-darwin.so` digest is
`85fad85339c7e345eabb66a33a25d8503c3ee8881f9d298fa8499a97617dbf68`.
Other ABIs require separately recorded/verified engine digests. Runtime refuses
engine/version/extension mismatch before catalog attachment.

## Implemented primitives

- Stable table creation; table and connection inlining off; global/table Zstd;
  optional VARCHAR UTC-day partitioning. Explicit catalog creation only.
- Disposable SELECT processes, one admitted process per data root/operator,
  2GB engine memory, two threads, monitored 2 GiB RSS, five-second total deadline
  including admission, row/serialized-byte limits, hard kill and reap on breach.
- Dedicated PostgreSQL mutation fence and reader/drained-maintenance fences.
  Connection loss fails the fence check; no reconnect or pooled lock ownership.

The reader/maintenance lock protocol is mandatory for every future serving read
and lifecycle operation. An OS admission lock alone does not drain catalog
transactions. A dedicated fence check alone does not fence a long-running export
if its connection disappears during a lake commit: exporter supervision must
cover that interval before release.

The configuration options follow [DuckLake configuration](https://ducklake.select/docs/stable/duckdb/usage/configuration).

## Remaining release work

- Actual fenced exporter integration with PG outbox and canonical projection,
  immutable transactional lake receipts and supervised lock-loss recovery.
- Complete backend configuration/routing through MCP, cockpit, summarizer and
  PG task/fleet projections; legacy parity tests.
- Daily fenced lifecycle operations and reader-safety tests.
- Immutable catalog/files backup generations, verified fresh restore, and
  replacement of the planned section in `docs/backup.md`.
- Bound the full-backup rebuild RSS, complete catalog publication/verification
  on the real backup, and rehearse the drafted cutover/rollback runbook.
- Least-privilege catalog provisioning, installer pin verification, Linux proof,
  fixed 25-session audit, 24-hour soak, and second-machine restore.

## Offline rebuild checkpoint

`drover-server lake rebuild` and `lake verify` are implemented without loading
hub config. The explicit three-table schemas exclude spans. Dedupe preserves the
repository ordering, with normalized payload SHA-256/file/ordinal tie-breaks,
and retains every null-key row with lineage. Verification recomputes hashes
from contents and verifies baseline/evidence digests. The drafted runbook is
`docs/operations/lake-cutover.md`; it explicitly blocks production execution.

The first full-tar dry run (2GB engine budget) completed in 62.06 seconds wall
(59.10 seconds inside rebuild), with 9,866,592,256 bytes peak RSS. Counts:

| Relation | Raw rows | Canonical rows |
| --- | ---: | ---: |
| agent_events | 5,288,617 | 5,211,239 |
| provider_usage_snapshots | 147,362 | unchanged |
| control_outbox_batches | 100,089 | unchanged |

Event losers: 77,378. Null-key rows retained: 357,236. The supplied frozen tar
has 5,350 more events than the spike baseline; no rows were deleted to force the
baseline. Normalized multiset SHA-256 (version 1 fixed-column JSON, sorted row
hashes with newline separators, multiplicity retained):

- Raw events: `69ab500054948ac56361b2e14931d95e3fcea9492916975da3ff28dd36b326e5`
- Canonical events: `69bd71edd43910c8f0902e175e641c9cdb5102d3802fa557ecd58c9a4925a0eb`
- Provider: `f7866d6ea00a14d14e95f415c18d9e9b2b8fc3cc35ce4e678434e717a86938ff`
- Outbox: `75c20d41144afc238b4ea488b566b59260739976d0b3ef2dc809ea57e65c5139`

**This is accounting evidence, not a successful bounded publication rehearsal.**
A 512MB single-scan publication attempt failed during staging after 11.62 seconds
(1,782,530,048 bytes peak RSS). Slicing execution through the same globally bound
schema also failed at 512MB after 30.15 seconds (2,476,802,048 bytes peak RSS).
The reduced budget and sliced staging are the current implementation; both
failures occurred before catalog publication. Rebuild memory use is a release
blocker. Do not raise serving ceilings to accommodate this tooling.

Small fixture catalog publication and independent verification pass against
initdb-created Postgres. Large local rehearsal is explicitly opt-in via
`DROVER_PHASE4_REHEARSAL_TAR`/`DROVER_PHASE4_REHEARSAL_ROOT`, and refuses an
external test DSN. All source data/evidence stays in scratch `/tmp` roots.
