# Fenced transactional DuckLake exporter

This slice implements the durable PostgreSQL **control-event outbox** adapter.
It is explicitly constructed, synchronous, and not started by the legacy server.
Serving/backend selection remains a later Phase 4 slice. No production cutover,
configuration change, payload pruning, or derived worker activation is implied.

## Provisioning and ownership

An admin explicitly calls `provision_exporter(spec, exporter_role=...)` against an
initialized separate catalog with the rebuilt `agent_events` and
`control_outbox_batches` tables. It creates stable `export_batch_receipts` and
`export_event_versions` tables, then installs the private catalog ownership
schema and snapshot commit trigger. Setup is deliberately separate from runtime
startup; it must complete before an exporter starts. The group name comes from
`provision_catalog_roles`. No login/password is created. Provisioning is not a
retryable migration command; partially failed admin setup requires inspection.

The added lake tables use Zstd, disabled inlining, and the pinned runtime.
Event versions retain UTC-day partitioning; raw outbox rows and receipts remain
unpartitioned. Neither tables nor receipts are replaced. No time-travel reads
or filesystem globs participate in publication or recovery.

```python
# Explicit scratch/rehearsal construction; never resolves hub configuration.
with LakeOutboxExporter(control_path=control_path, spec=spec) as exporter:
    result = exporter.run_once()
```

Runtime pins are verified before ownership acquisition. The context holds a
dedicated PG advisory lock for its entire lifetime, including
between batches. A second owner fails with `lake_mutation_fenced`. Each check
verifies the actual advisory lock, not just whether the connection remains open.
One owner processes one batch at a time. Export and lifecycle operations share
the existing catalog mutation lock.

A fresh ownership token is bound to the child catalog connection's
`application_name`. At the pinned DuckLake snapshot publication boundary, a PG
trigger verifies the token and the dedicated lock holder. It holds a shared
ownership-row lock until catalog commit. Ownership replacement takes an
exclusive row lock, draining an already-validated old commit before issuing its
new token. A stale engine cannot publish afterwards. Limited exporter roles must
supply the token and cannot edit ownership state or disable the trigger. Admin
operations remain separate, explicitly fenced operations.

This uses DuckLake's [atomic catalog transaction and snapshot contract](https://ducklake.select/docs/stable/duckdb/advanced_features/transactions)
and [snapshot insertion query](https://ducklake.select/docs/stable/specification/queries).
The trigger is part of the application catalog provisioning contract; engine or
catalog upgrades must retest it before changing the recorded runtime pins.

## Batch and recovery contract

1. Claim at most 100 events by default (maximum 1,000) using existing PG durable
   membership and stable batch IDs. Freeze the full raw envelopes and canonical
   `project_control_event` projection in `lake_export_batches`, with a SHA-256
   input digest and catalog identity, under a repeatable-read PG snapshot.
   Validate membership, ordinals and payload
   hashes. Frozen input is capped at 32 MiB. Projection is frozen once: identity
   enrichment after a crash cannot change replay semantics.
2. Start one disposable export process: 1GB engine memory, one thread, spill
   under the data root, monitored 2 GiB child RSS ceiling and 30-second deadline.
   The supervisor monitors the dedicated fence, kills/reaps on failure, and
   checks ownership again before accepting the result. DSNs stay in environment
   variables, outside the owner-only temporary input document and logs.
3. Before any MERGE, cast into explicit schemas and rank each non-null key once
   with repository attribution/timestamp/ID ordering, followed by normalized
   payload SHA-256 and stable file/ordinal tie-breaks. Compare only affected
   target keys with the same ordering. Full payloads, not UI previews, feed the
   canonical projection. Control identities retain `control:<event_id>` keys.
4. In **one lake transaction**, MERGE canonical events and append raw outbox
   rows, immutable deduplicated event versions, and one immutable batch receipt.
   The receipt binds catalog identity, contract version, batch/input hash,
   raw/canonical counts and normalized multiset hashes. Version rows preserve
   per-batch proof even when a later canonical winner replaces an active row.
5. Only after lake commit, atomically record the receipt hash in PG and
   acknowledge the batch and every member. No file archive path or archive
   coverage is invented, and payloads are retained. Acknowledgement is not
   permission to prune or a backup generation.

A replacement first recovers frozen unacknowledged batches, before claiming new
work, even if the former outbox lease has not expired. It validates current PG
source membership against frozen input, recomputes the expected receipt, and
inspects the catalog receipt. If present, it recomputes raw and event-version
content hashes, then acknowledges without another lake append or snapshot. A
mismatch fails explicitly and leaves PG unacknowledged. If absent, it publishes
the exact frozen batch. Orphan files never become serving input. A crash before
input freezing leaves an ordinary claimed PG batch, recovered after its existing
lease expires. All failures retain durable ingress for retry.

## Focused proof

`tests/test_lake_exporter.py` uses the existing disposable initdb harness, isolated
control schemas, fresh catalog databases and small fixture lakes. It covers:

- Commit followed by crash before PG acknowledgement; immediate replacement
  replay, stable projection after identity enrichment, unchanged snapshot count,
  full payload preservation and ignored orphan files.
- Two exporters fenced throughout ownership, including between batches;
  stale-token rejection and actual snapshot-transaction ownership draining.
- Atomic transaction rollback; payload-hash rejection before lake publication;
  corrupt raw/event-version content rejected before acknowledgement.
- Duplicate-key batches ranked before MERGE, with attribution winner ordering.
- Limited-role export and rejection of unfenced writes/ownership edits;
  explicit detection of advisory unlock on a still-open connection.

Run in the foreground with verified extension artifacts and **without** an
external `DROVER_TEST_POSTGRES_DSN` to use disposable infrastructure. The follow-up [canonical serving slice](lake-serving.md) adds explicit server
routing and separately opt-in lifecycle activation. Daily maintenance and paired
backup/restore remain separate gates.

Checkpoint validation: **12 exporter integration tests passed** as part of the
foreground combined run: **81 passed, 3 skipped in 44.75 seconds**. The skips
were the opt-in full-backup rehearsal and two legacy exporter cases requiring
an external-DSN environment variable; that variable was explicitly removed so
this run used the harness-owned disposable cluster. Formatting/import checks
and `git diff --check` passed. Runtime-pin rejection leaves ingress pending.
