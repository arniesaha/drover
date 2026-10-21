# Work items: hub-owned continuity

Every session accepted through the hub creates a durable `work_items` record,
or links an explicit `work_item_id`, in the same control-plane transaction that
records the hub session. This is an extension of the existing work-queue
control schema, not a second queue. It records local execution continuity while
GitHub and the configured provider queue continue to own provider policy.

Launch metadata is intentionally small: title/intent, repository, branch,
session id, device or credential owner, stage, gate, optional acceptance and
next action. The initial stage is `launched`, with the `worker_done` gate.
`intent` is preferred; otherwise the first prompt line and then the branch are
used. The timeline is append-only.

Lifecycle projection happens inside the existing harness-event ingest
transaction, after a new outbox event wins. The source event id is unique in
the timeline, so replaying an event is a no-op. A completed turn sets
`worker_done` and `ci_green`, retaining a reported commit SHA; awaiting input
sets `needs_input`; an error sets `blocked`; and termination sets `closed`
unless the item is already `done`. Shadow mode never disables these writes.

The API exposes item list/detail/edit and the reconciler export. The export is
the only bridge required by the current external reconciler:

```json
{"items":[{"id":"...","stage":"worker_done","pr":null,"branch":"feature/x","sid":"harness-...","next":null,"after":"..."}]}
```

`drover work-items export` emits the same shape from the configured hub.

## Migration and rollback

Migration 23 is additive: it creates `work_items`, `work_item_timeline`, and
their indexes. It does not alter or backfill existing tables. A down migration,
if a rollback is required, is `DROP TABLE work_item_timeline; DROP TABLE
work_items;` after taking an export backup; application rollback alone leaves
the new tables inert and is the preferred safe path.

Later continuity steps need the event emission hooks below, a webhook receiver
for CI and PR facts, and a rules engine to act on gates.

## Step 2: subscriber events

Every timeline append now atomically creates a versioned `work_item.event`.
The timeline row id is its immutable `event_id`; the payload includes the item,
kind, prior and resulting stage, gate, next action, session, PR, and timestamp.
The hub allocates a workspace cursor while holding the transaction lock, so a
pull consumer can advance `GET /work-items/events?since=<cursor>` without
skipping a concurrent commit.

Webhook subscriptions are owned by the creating credential. The server makes a
per-subscription HMAC secret, returns it exactly once at creation, and stores
the secret only to sign future delivery bodies. URLs require HTTPS except for
loopback or an explicitly allow-listed tailnet host. Matching subscriptions are
expanded into a durable delivery row in the same transaction as the event.

The delivery worker leases a small database batch and uses a fixed in-flight
cap. It signs `X-Drover-Signature: sha256=<hmac>` and includes
`X-Drover-Event-Id`; success is any 2xx response. Failures retry with bounded
exponential backoff (capped at one hour) and move to `dead_letter` after the
configured attempt limit. Health is advisory in `/readyz`, while Prometheus
exposes sent, failed, dead-lettered, pending, and lag measures.

Step 3 can add a GitHub webhook receiver that appends CI/PR facts through this
same timeline/event transaction. Step 4 can consume these event facts in an
in-hub rules engine, using durable cursor delivery rather than a poll loop.
