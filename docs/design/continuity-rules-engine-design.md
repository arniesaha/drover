# Continuity Rules Engine Design

## Goal

Move mechanical, safe work-item progression into the hub.  A worker still
publishes its own branch; the hub verifies the remote state and uses the
configured GitHub credential only for pull-request lifecycle writes.  Anything
ambiguous or requiring judgment wakes the external orchestrator.  A periodic
sweep is retained as a safety net.

## Boundaries

The existing work-item timeline and `work_item.event` outbox remain the source
of facts.  A new rule worker consumes those events after GitHub fact
projection, and periodically re-evaluates open items.  It never runs shell
commands or `git push` on a worker host.  GitHub writes use the existing
per-goal `GitHubProvider` credential path; unavailable credentials turn a
would-be write into an `orchestrator_wake` and advisory readiness detail, not a
readiness failure.

The rule worker records an append-only timeline/event for its evaluation,
action, skip, or wake.  It ignores its own bookkeeping events so that these
records cannot recurse.  A durable action row provides idempotency: the key is
`(work_item_id, rule, head_sha)`, with a stable synthetic head value for rules
that do not naturally have a commit SHA.  An action is claimed before an
external write and finalized afterward; an interrupted claim remains visible
for an operator instead of being replayed blindly.

## Additive data and API

Migration 26 only adds tables/columns:

- `work_items.auto_merge BOOLEAN NOT NULL DEFAULT FALSE`, `automation_gate TEXT
  NOT NULL DEFAULT 'none'`, and `publish BOOLEAN NOT NULL DEFAULT FALSE`.
  (`gate` already names the released lifecycle gate.)
- `work_item_rule_actions` for rule idempotency, outcome, dry-run state, and
  short reason;
- `work_item_dependencies` for directed `after` edges; and
- a workspace cursor for durable event consumption.

Existing rows therefore take no automatic action.  The item patch endpoint
accepts the additive policy fields and dependency list; valid gates are
`none`, `ui_review`, and `arnab`.  `ui_review` and `arnab` always prevent
automatic merge.  The API also exposes the queue execution kill switch as an
authenticated device-only toggle; configuration remains the default source of
truth at process start.

## Rules

| Trigger | Preconditions | Hub action | Otherwise |
| --- | --- | --- | --- |
| `worker_done` / sweep | `publish`, reported SHA, remote branch exists at that SHA, no PR | Open a draft PR | Wake `branch_not_pushed` or `head_mismatch`; other uncertainty wakes |
| `pr_behind` / sweep | current PR, no failing CI, writable credential | Request GitHub branch update | Wake for conflict, failed CI, missing credential, or ambiguity |
| `ci_green_exact_head` / sweep | SHA equals current item head, PR current with main, `auto_merge`, `gate=none` | Mark ready then squash-merge with expected head SHA | Skip stale heads; wake if the write path/PR state is uncertain |
| `pr_merged` | item currently open | Mark item done and unblock direct dependents | Record no-op if already done |
| `ci_failed`, conflicts, worker errors/awaiting, review, unexpected state | none | Emit one `orchestrator_wake` with a short stable reason | Never auto-act |

Dry-run uses the same evaluations and durable action identity, but records
`would_act` without calling GitHub, changing stages, unblocking dependencies,
or launching work.  Duplicate events and sweeps find the same action identity
and append a skip rather than repeat an external side effect.  A green result
for an old SHA is always a stale-head skip and can never merge.

## Queue execution

`shadow_mode` remains true by default.  Per-goal `execution_enabled` is false
by default; it is the sole opt-in for turning existing reconciler candidates
into calls through `WorkService.launch`.  There is no second launch path.
The executor checks the current global and per-goal concurrency limits,
estimated-cost/budget limits, and an in-memory/configurable kill switch before
each launch.  Denials are reported as metrics and durable queue diagnostics.
The existing manual launch authorization remains unchanged.

## Configuration and observability

`work_queue.rules` supplies `enabled`, `dry_run`, and a sweep interval bounded
to 30-60 minutes.  Goal scopes add `execution_enabled`,
`execution_budget`, and (where needed) an execution concurrency override;
global execution settings provide a cap and initial kill-switch value.  Metrics
cover rule evaluations, actions, skips by stable reason, wakes, launches,
cap/budget denials, and kill-switch state.  `/readyz?detail=1` reports whether
write-capable GitHub credentials are usable, but stays healthy when they are
not.

## Verification

Focused mocked tests prove every rule runs once, stale heads and gated items
never merge, duplicate events are idempotent, dependencies unblock on merge,
dry-run has no side effect, branch-not-pushed degrades to a wake, and missing
GitHub credentials degrade to a wake.  Queue tests prove shadow defaults,
per-goal/global caps, budgets, and the kill switch.  Migration and lake rollout
tests run because migration 26 is added.
