# #720: Studio ownership reconciliation

## Root cause

The Studio is replaying orphaned July/August event history whose session ownership rows no longer exist on either host or hub, rather than events from sessions moved by the October 19 03:00 reaper. Read-only inspection on October 19 at approximately 10:24–10:26 PT found eight remaining host-scoped missing-session retry rows first observed on October 18 at 18:10:02 PT, with no matching Postgres `harness_sessions` row (owner host, status, and lifecycle generation all NULL; this ingest protocol has no owner epoch). Some historical event IDs differ between host and hub even at the same sequence: the 41-event session has 31 matching IDs and ten host-only IDs. The hub recognizes duplicates but treats unmatched IDs as an ownership-registration race, returning HTTP 403 with `{"error":"session ownership not registered","pending":[...]}`; a pending session causes the pusher to retain the entire session batch and exponentially retry at 30–300 seconds. The existing hub-observed orphan window was 24 hours, so these particular rows had not yet expired after roughly 16 hours: they were long-lived retries, not evidence that the existing expiry timer had reset on the 0.6.24 flip. Independently, `reconcile_unsent_events()` returned `None` for both pending ownership and delivery failures, and the daemon labeled every incomplete pass “deferred until hub recovery” on each successful 15-second registration heartbeat. There is no hub-unhealthy flag or pending-reject counter in this path: `/healthz` returning `ok` and `analytical=ok` is consistent with this failure.

## Read-only evidence

Source: `~/Library/Logs/drover/{server,harnessd}.err.log`, a disposable filesystem clone of the Studio host ledger, and Postgres queries with `default_transaction_read_only=on`. No production POST, service restart, migration, or data edit was performed. The clone was queried read-only; failed scans are not evidence of on-disk corruption because it was copied while the daemon was active.

Postgres schema: `drover_control`. Credential host for all rows below: `Arnabs-Mac-Studio`. Every row has **no owner/session row** in both Postgres and the host clone. All eight persisted `first_seen_at` values are within 18:10:02.874460–18:10:02.950318 PT on October 18.

| Session ID | Host ledger events | Matching hub event IDs | Host-only event IDs |
| --- | ---: | ---: | ---: |
| `harness-04ee6ce0-6830-448f-a18e-38563185a0bd` | 41 | 31 | 10 |
| `harness-cf6fe172-fce9-4459-86cf-ccd132d44922` | 11 | 5 | 6 |
| `harness-78a32828-1015-4e5b-b1f7-f29f68e15201` | 7 | 5 | 2 |
| `harness-2a6a75af-b5a4-41ed-b439-a9ded121329d` | 12 | 6 | 6 |
| `harness-892c3348-4361-4b4f-b1a7-1031674b26e9` | 10 | 6 | 4 |
| `harness-a8372a13-443e-4d65-9943-3351a08c5b31` | 20 | 10 | 10 |
| `harness-810ac776-5dff-4932-af00-f653efd46028` | 18 | 8 | 10 |
| `harness-e4a32c8a-e65b-407a-81c8-27601e66ccda` | 18 | 14 | 4 |

The reported five event-count values are not five uniquely identified sessions: replay pages fragment sessions, and several sessions share counts. The first four whole-session counts match 41/11/7/12; the remaining sessions can contribute page fragments. Existing pusher logs contain counts rather than session IDs, so an exact historical `events=8` fragment-to-session assignment cannot be recovered from those logs alone.

At `server.err.log:1718061`, 10:22:01 PT, the hub logs:

```text
structured event ownership pending: route=/harness/events session_id=harness-04ee6ce0-6830-448f-a18e-38563185a0bd credential_host_id=Arnabs-Mac-Studio
```

This is emitted only by the HTTP 403 branch above. **The historical hub logger did not record status/body or the complete pending list.** The status and JSON schema are established from the installed 0.6.24 handler, and reproduced in isolated regression tests; no raw production response body was captured. The patch adds status and complete ownership-response metadata to future logs without event bodies or credentials.

Example identity mismatch at sequence 4 for the 41-event session:

- Host ID: `harness-event-77de7ca0-eeaf-41f0-b3b9-5fe1aeb9f18b`
- Hub ID: `harness-event-b0a498e8-32db-4b4f-8fe9-e6634f370b49`
- Both have event type `status`. No payload comparison was needed for the ownership diagnosis.

Reproduce the ownership query read-only with the configured DSN (do not print the DSN):

```sql
SET default_transaction_read_only = on;
SELECT r.host_id AS credential_host, r.session_id,
       to_timestamp(r.first_seen_at) AS first_seen,
       s.host_id AS owner_host, s.status, s.lifecycle_generation
FROM drover_control.harness_unknown_session_retries r
LEFT JOIN drover_control.harness_sessions s USING (session_id)
ORDER BY r.first_seen_at;
```

A separate live failure appeared during investigation: the host's `prune_reconciled_events()` raised DuckDB `FatalException: Failed to delete all rows from index`, invalidating that process's database instance. This patch reports local ledger failures separately and preserves pending work. It does not repair a damaged production index. The release orchestrator should inspect this failure if it persists after the normal upgrade restart; any repair or one-time cleanup requires a separately documented proposal and approval.

## Corrected behavior and rollout

- Missing sessions receive ten minutes of **hub-observed** registration grace, persisted across restarts. Event timestamps never establish orphan expiry. Valid registration during that window still permits delivery.
- Expired missing sessions and sessions owned by another host have explicit terminal rejection reasons. A capable client can deliver owned events in a mixed batch while quarantining only rejected sessions. Older clients keep their atomic wrong-owner HTTP 403 behavior; the new client advertises `X-Drover-Event-Delivery: quarantine-v1`.
- A host commits `harness_event_quarantine(event_id, session_id, reason, quarantined_at)` before removing rejected events from its retry queue. Original event payloads remain in the host ledger, with no successful acknowledgement timestamp. Reconciliation, restart, stale live queues, and acknowledged-event retention all respect those durable receipts.
- The hub records a metadata-only `events.ownership_rejected` insight for the reporting host. No transcript text or token is included.
- Successful registration remains the reachability signal that schedules replay. Pending ownership is debug-level per-session work; transport/delivery and local ledger failures have separate diagnostics. No synthetic ownership backlog flag gates hub health.
- PostgreSQL control migration 22 and additive host bootstrap create the quarantine receipt table. Upgrade hub and harnessd together through the normal release process. Existing observed retry windows already older than ten minutes become terminal on the next eligible replay.

No one-time production cleanup is required for ownership reconciliation. Do not delete orphan events or recreate ownership rows to bypass authorization. Quarantined history is retained for explicit, reviewed recovery if needed.
