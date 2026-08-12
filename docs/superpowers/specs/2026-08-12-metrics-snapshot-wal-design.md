# Metrics Snapshot WAL Completeness Design

## Problem

`MetricsCollector._quality_snapshot` and `_observatory_snapshot` isolate their
analytical reads by copying the live DuckDB database into a temporary
directory. They currently copy only the main database file. When a writer
connection remains open, committed changes may exist only in `<database>.wal`,
so either snapshot can open successfully while silently omitting committed
data.

The copy-based isolation is load-bearing and must remain. Reading the live
database caused analytical work to contend with request-serving work in a
previous production incident.

## Scope

Fix only the two metrics snapshot paths:

- `MetricsCollector._quality_snapshot`
- `MetricsCollector._observatory_snapshot`

Do not change snapshot caching, analytical query behavior, control-plane
storage, or other file-copy sites.

## Design

Both metrics paths will use the existing WAL-aware DuckDB store-copy helper in
`drover.server.db`. That helper copies the main file and, when it exists, its
adjacent `.wal` file to the matching destination name. A missing WAL remains a
normal case.

The temporary-directory lifecycle and private-copy read path remain unchanged:

1. Create a temporary directory.
2. Copy the source store and optional WAL into it.
3. Run the existing quality or observatory reader against the private copy
   using the `snapshot` DuckDB role.
4. Let the temporary directory remove both files after the read.

Reusing the established helper keeps DuckDB snapshot semantics in one place
and avoids a second implementation that could drift.

## Error Handling

Copy failures retain the current behavior of their calling paths:

- Quality snapshot failures propagate to the existing metrics refresh error
  handling.
- Observatory snapshot failures are caught by `_observatory_snapshot`, logged,
  and returned as an error payload.

No retry or fallback to a live read will be added. A live-read fallback would
violate the isolation guarantee.

## Testing

Regression tests will exercise real DuckDB behavior rather than mock copy
calls. Each affected path will be tested while a writer connection remains
open and committed fixture data is still represented by the WAL. The test will
invoke the metrics snapshot path and assert that the copied reader observes the
committed data.

The tests must fail against the current bare-`copy2` implementation and pass
after the WAL-aware helper is used. Existing isolation and snapshot-role tests
must continue to pass. Focused metrics tests will run first, followed by the
appropriate broader Python verification for the changed server code.

## Success Criteria

- Quality and observatory snapshots include committed data residing in the
  source WAL.
- Both paths still read a temporary private copy with the `snapshot` role.
- Absence of a WAL does not cause an error.
- No live analytical read or unrelated refactor is introduced.
