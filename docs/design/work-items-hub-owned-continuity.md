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

## Step 3: GitHub CI and pull-request facts

`POST /integrations/github/webhook` is a public, signed ingress route. Set
`work_queue.github_webhook.secret_env` to the name of an environment variable
containing the webhook secret. The receiver verifies
`X-Hub-Signature-256: sha256=<hmac>` in constant time, caps the body (128 KiB
by default), deduplicates `X-GitHub-Delivery`, and writes the delivery before
returning `202`. It never logs the secret or body. A bounded local worker then
handles the durable delivery; bad/missing signatures return `401` and an
oversized body returns `413` before it is read.

The worker links a delivery only to an existing item in the same workspace by
repository plus PR URL/number, falling back to the recorded branch. It appends
`ci_completed`, `ci_green_exact_head`, `ci_failed`, `pr_synchronized`,
`pr_merged`, `pr_closed`, `pr_behind`, and `review_submitted` (plus small PR
state facts) through the normal timeline/outbox transaction. `detail` is an
additive, small JSON field on `work_item.event`; version remains `v: 1`.
Unknown PRs never create items.

For a completed suite or workflow, a green/failed rollup is emitted only when
its `head_sha` equals the item head learned from a PR event. When the configured
GitHub credential has `checks_read` and can read branch protection, the worker
checks that branch's required check contexts at the immutable SHA; an
in-progress check delays the fact. Without that read path it degrades cleanly
to the conservative accumulated-webhook rollup. Individual `check_run` facts
never turn an item green.

The smallest secure tailnet deployment is an Actions `workflow_run` job that
can reach the hub over Tailscale (or a Cloudflare Access-protected tunnel) and
POSTs the signed JSON summary with a secret injected by that environment.
Alternatively route GitHub's webhook through a Cloudflare tunnel to this
receiver. Do not expose an unauthenticated route, and do not configure a live
webhook or secret from Drover. Receiver health is advisory in `/readyz?detail=1`;
Prometheus exports receive/reject/dedup/link/error counters.

The supported default is the Actions route; add this to the repository that
owns the work items, after supplying the three named Actions secrets. This is
an example only—Drover never creates the workflow, secret, tunnel, or webhook.

```yaml
name: report completed CI to Drover
on:
  workflow_run:
    workflows: [CI]
    types: [completed]
jobs:
  report:
    runs-on: ubuntu-latest
    steps:
      - uses: tailscale/github-action@v2
        with: {oauth-client-id: ${{ secrets.TS_CLIENT_ID }}, oauth-secret: ${{ secrets.TS_CLIENT_SECRET }}}
      - env:
          HUB_URL: ${{ secrets.DROVER_HUB_URL }} # https://hub.tailnet.ts.net
          WEBHOOK_SECRET: ${{ secrets.DROVER_GITHUB_WEBHOOK_SECRET }}
        run: |
          body=$(cat "$GITHUB_EVENT_PATH")
          sig=$(printf %s "$body" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex | sed 's/^.* //')
          curl --fail-with-body --max-time 5 -X POST "$HUB_URL/integrations/github/webhook" \
            -H 'Content-Type: application/json' -H 'X-GitHub-Event: workflow_run' \
            -H "X-GitHub-Delivery: actions-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" \
            -H "X-Hub-Signature-256: sha256=$sig" --data-binary @"$GITHUB_EVENT_PATH"
```

Migration 25 is additive: it adds `work_items.head_sha`, the additive event
`detail`, and durable GitHub delivery/rollup tables. It does not backfill or
alter existing values.
