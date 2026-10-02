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
- Rebuild/verify CLI, frozen-schema dedupe accounting, backup rehearsal and
  cutover/rollback runbook.
- Least-privilege catalog provisioning, installer pin verification, Linux proof,
  fixed 25-session audit, 24-hour soak, and second-machine restore.
