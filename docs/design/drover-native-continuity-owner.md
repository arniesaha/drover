# Drover-native continuity owner

Issue #848 makes the Drover hub the sole continuity executor. A worker completion,
block, idle transition, or CI fact is sufficient for the hub to continue an
authorized goal even when OpenClaw, Hermes, or any client is stopped. Clients
may subscribe to status; they are never on the execution path.

## Identity and configuration

`GoalScope.continuity_owner_id` accepts existing `agent:<harness-key>` owners
during migration and the canonical native form `drover:owner/<goal_id>`. The
suffix must exactly equal `GoalScope.goal_id`, preventing cross-goal claims;
all other `drover:` identities are rejected.

A native owner has an explicit goal execution profile: configured harness and
optional model, an approved `execution_cwds` entry, checked-in `brief_ref`, and
an optional concurrency limit bounded by the global owner cap. The profile is
invalid without `execution_enabled` and the normal worker-launch validation.
No implicit model/default is introduced. The standing brief requires: poll,
lease, consume one delivery, perform only its authorized action, verify, ack.

## Hub owner runner

Drover owns execution, context, and continuity through its harness adapter
contract, not a named harness. When the decision/outbox transaction creates or
redelivers an owner wake, the runtime resolves the goal and queues a separate
owner-run candidate. It keeps a durable owner-session link keyed by
`(goal_id, owner_id)`. A usable linked session receives one turn through the
existing session-turn API. Otherwise the hub launches exactly one harness
session with the configured profile and brief, records the link atomically, then
sends the delivery turn.

The turn contains the wake id, goal/repository context, verified authority, and
lease/consume/ack contract, not untrusted issue text as authority. The owner
fetches the delivery itself, so an old resumed session can reject it. Launch
failure leaves the wake pending for bounded retry and alerting; it never falls
back to an external orchestrator.

## Fencing, authority, exactly once

Existing lease epochs fence every poll, consume, and acknowledge. A replacement
owner makes stale sessions unable to consume or ack. The wake id is the
idempotency key: consume returns one oldest visible delivery and gives it a
visibility timeout; redelivery preserves its id and payload.

Before its single authorized next action, the owner records/checks an action
receipt keyed by wake id and action fingerprint. On re-wake it verifies that
receipt and outcome rather than repeating an external write, then acknowledges
the original wake. Killed turns, duplicate candidates, and at-least-once
outbox delivery therefore cannot make a second authorized action.

Authority is hub-verified data. Deployment, release publication, configuration
outside the goal, deletion, money, and public-facing actions require explicit
human approval. Native owners cannot enlarge their configured scope.

## Recovery and caps

Owner session links, claims, wake state, leases, and receipts are durable
control-plane records. After restart the hub reclaims only expired leases,
requeues expired incomplete claims, preserves unacked wakes through visibility
timeout, and checks receipts before sending or accepting redelivered turns.
A restart may resume work but cannot duplicate action.

The execution runtime retains global/per-goal worker limits plus a distinct
bounded owner cap. Owner turns neither consume worker slots nor bypass budget
or kill-switch checks. Exhaustion becomes one actionable stall/alert, never a
busy loop.

## Dependency check

| Component | Required dependency | Optional client/harness behavior |
| --- | --- | --- |
| wake/outbox, lease, receipt, recovery | Drover control store and hub | no OpenClaw/Hermes dependency |
| owner scheduling/context | Drover runtime and goal profile | any adapter implementing the session contract; no Codex-specific branch |
| owner action/ack | hub HTTP/session APIs and verified authority | clients may display status but cannot make progress required |
| status/alerts (#838) | Drover read model and metrics | subscribers consume them opportunistically |

A missing harness makes only that goal's configured profile unavailable and
produces a bounded launch alert; it cannot corrupt the queue or make Drover
depend on another harness. A missing optional client has no effect on owner
launch, fencing, acknowledgement, or recovery.

## Observability and migration

Native owner runs appear in the #838 status read model with goal, owner id,
session id, wake id, epoch, retry/visibility deadlines, and last receipt.
Metrics cover queued/running/failed owner turns, lease reclaims, redeliveries,
receipt deduplications, launch latency, and oldest pending wake. Correlated
logs carry `goal_id`, `wake_id`, `owner_id`, epoch, and session id without
credentials or full issue bodies.

Until the canary passes, the interim owner remains `agent:coder:main`; the
OpenClaw plugin may only notify/poll and no NAS configuration changes occur.
After a native-owner soak migrates #819, Stage 7 removes the Drover plugin
package and owner-schema script, deprecates `agent:*` configuration, disables
and uninstalls the NAS plugin, removes its config and AGENTS.md wake section,
and prepares (but does not restart) an OpenClaw fork-revert PR. The gateway
restart remains an explicit human hard stop.

## Canary, soak, and rollback

Focused tests cover native ID validation, an owner run from `owner_wake`,
duplicate re-wake receipt suppression, and lease recovery after hub restart.
A dedicated goal runs with the OpenClaw plugin disabled: a tiny queued issue
auto-launches, invokes the owner, performs one action, and acks. A second run
restarts the hub mid-flight and proves no duplicate action. Three unattended
native jobs are the soak gate before removal. Rollback is goal-local: disable
execution or restore its `agent:*` owner; never alter other goals.

The first implementation slice is deliberately hub-run owner plus fencing and
receipts. A subsequent slice may permit an owner to file a follow-up issue
within its goal only when the label namespace, issue cap, budget, and verified
authority all allow it. That action gets its own receipt/fingerprint and uses
the hub's configured GitHub identity; it is not required for the native-owner
canary.
