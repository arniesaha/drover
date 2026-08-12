# Analytics and Advisory Query Pressure Design

## Goal

Restore reliable observed-cost analytics and insight details by removing redundant local DuckDB work, honoring the configured advisory review interval, and making partial analytics failures visible in the iOS app.

## Scope

This change covers four related defects:

1. An analytics request rebuilds the same normalized session-facts relation for the snapshot fingerprint, totals, projects, harnesses, hosts, and models.
2. The advisory scheduler computes material source versions for all six analyzers on every five-second worker poll, even though automatic full reviews are configured for a 24-hour interval.
3. The iOS analytics screen silently omits observed usage and API cost when the server returns an error section with no data.
4. A client timeout while the server is still producing an insight response can raise `BrokenPipeError` while response headers are sent.

It does not change provider-capacity collection, contact an external AI or provider service, increase DuckDB's memory limit, or restart/deploy the shared live server as part of implementation.

## Current Data Flow

All implicated analytical work is local:

- `spans_enriched` is a DuckDB view over local Parquet span data.
- `sessions` is local analytical state.
- `harness_sessions` is read through an attached private snapshot of the local control-plane DuckDB.
- advisory provider, telemetry, routing, and hook facts are read from those local stores.

The analytics API does not call OpenAI, Anthropic, or provider APIs. Provider-capacity refresh is a separate subsystem and is not on the observed-cost or insight-detail request path.

## Design

### One analytics materialization per request

`activity_analytics` will materialize the filtered normalized session facts into a connection-scoped temporary table inside its existing transaction. The snapshot fingerprint, totals, project page, and harness/host/model pages will read that table rather than embedding the complete `spans_enriched`/session/control-plane CTE in every statement.

The temporary table will:

- remain connection-scoped and never persist in `drover.duckdb`;
- be created after the request's fixed `snapshot_at` is selected;
- contain only the filtered facts required by the response;
- be dropped in cleanup on success, query failure, cancellation, or transaction rollback;
- preserve the existing cursor fingerprint and pagination semantics.

This reduces repeated Parquet/view work within one analytics response without introducing a stale cross-request cache or a new schema object.

### Honor the automatic advisory interval

`AdvisoryScheduler.enqueue_due_full_review` will check the configured time bucket before calling `source_version_factory`. If the current 24-hour bucket has already been evaluated, it will return immediately without loading any operational snapshots.

On the first poll in a new bucket it will compute material versions, enqueue only analyzers whose versions changed, and record the bucket after the complete pass succeeds. If version calculation or enqueueing fails, the bucket will remain unrecorded so a later poll can retry.

Manual scoped rechecks continue through the existing explicit `check_again` path and are unaffected. The five-second worker poll remains responsible for claiming already-enqueued jobs; only automatic source-version recomputation is reduced to the configured daily cadence.

### Explicit iOS degraded state

The analytics view will always render an Observed usage section when the server supplies an analytics snapshot. If `activity.data` is absent or the section status is not usable, it will show a compact warning card stating that observed usage, including API cost, is temporarily unavailable and can be retried by pull-to-refresh.

Provider-reported subscription capacity remains visible. The view will not invent zero sessions, zero tokens, or zero cost when data is unavailable.

The presentation text and accessibility label will live in a small testable presentation type in DroverKit; the SwiftUI view will consume that type.

### Disconnect-safe HTTP response

The response writer will catch `BrokenPipeError` and `ConnectionResetError` across both header transmission and payload transmission. A disconnected client will produce the existing concise informational log rather than a socketserver traceback. Other response errors will continue to surface.

## Error Handling

- Analytics temporary-table cleanup is best effort while preserving the original query exception.
- An interrupted analytics query continues to return an isolated `activity.status = error` section and triggers the existing cooldown.
- Advisory bucket state advances only after all source-version calculations and enqueues complete successfully.
- The iOS client distinguishes missing/error data from a truthful numeric zero.
- Client disconnect handling is limited to the two expected socket exceptions.

## Testing

Server tests will prove:

- one analytics response materializes normalized facts once and every breakdown remains identical;
- temporary materialization is removed on success and failure;
- pagination and snapshot-change behavior remain intact;
- repeated scheduler polls in one bucket do not call the source-version factory;
- a new bucket recomputes versions and unchanged facts do not enqueue duplicate work;
- a failed version pass retries within the same bucket;
- manual rechecks remain independent;
- disconnecting during headers does not escape the request handler.

Swift tests will prove:

- an error or unknown activity section produces the unavailable presentation;
- an OK section with data does not show the warning;
- the warning explicitly mentions observed usage and API cost and has a stable accessibility identifier.

Focused Python and Swift suites will run before the complete repository-appropriate test gates. Live services will not be restarted until code verification is complete and deployment is explicitly authorized.

## Success Criteria

- Automatic advisory source-version queries run at most once per configured 24-hour bucket, barring a failed pass that must retry.
- Each analytics request builds its filtered session-facts input once.
- Cost unavailability is explicit in the iOS UI.
- Insight-detail client disconnects no longer emit `BrokenPipeError` tracebacks.
- Existing analytics values, cursor behavior, advisory manual rechecks, and provider-capacity behavior remain unchanged.
