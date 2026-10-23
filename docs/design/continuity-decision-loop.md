# One continuity decision loop (gaps 1, 2, 4 and 6)

Issues: #784 (gaps 1 and 6), #785 (gaps 2 and 4). Source: the Oct 23
architecture review, sections 5 to 7.

Today continuity is decided twice. `drover.py reconcile` (operator glue,
GitHub-backed, live every 10 min) and the hub rules engine
(`work_queue/rules.py`, shadow) have different rule sets. Workers end turns in
an ambiguous `awaiting: input`, approvals do not reach them, and wakes are
best-effort chat messages. This note fixes the shape of the replacement and the
PR order. Nothing here ships before the #777 canary finishes.

## 1. Worker states (gap 4)

A worker ends every turn with exactly one machine-readable line:

```
Drover-State: done sha=<40-hex> [pr=<n>]
Drover-State: blocked reason=<short text>
Drover-State: needs-approval gate=<pr|merge|rollout|canary|human> reason=<short text>
Drover-State: working
```

The hub projects the last such line of the turn's final assistant text onto the
session as `worker_state`, `worker_state_detail` and `worker_state_at`. It is a
projection inside the existing harness-event ingest transaction, so it is
idempotent by event id and works for every structured adapter (Codex, Claude
Code, OpenClaw ACP) without per-adapter code. Harnesses that cannot emit text
fall back to the inferred state.

| Derived state | Source | Meaning |
| --- | --- | --- |
| `working` | turn in progress, or an explicit `working` line | progress expected; stage deadline applies |
| `done` | explicit line with a SHA | claims completion; evidence is checked by the decision function, not trusted |
| `blocked` | explicit line, or a harness error | needs a decision; reason is required |
| `needs_approval` | explicit line naming a gate | auto-answered when the issue's label already covers the gate |
| `idle_undeclared` | `awaiting=input` with no line in the last turn | a stall candidate, never a wait |
| `ended` | session terminated or completed | terminal |

`done` with a SHA that does not match the remote branch is reported as
`head_mismatch` (gap 9 territory, wake only).

## 2. Authority as data (gap 2)

The issue's autonomy label and its `Hard stop:` lines are rendered into one
`Authority` block. Launch (`WorkService.launch` and `drover.py launch`) and
every nudge/turn sent by the decision loop prepend it, together with the
`Drover-State` contract above. A `needs_approval` state whose gate is at or
below the approved stage, and not a hard stop or always-human gate, is answered
automatically with one turn ("approved through `<stage>`, proceed"). Any other
`needs_approval` becomes `blocked:human` on the issue with the worker's reason.

## 3. Deadlines and leases (gap 4)

Each state has an expected duration (configurable, defaults below). Liveness is
`state_at + budget`, not "newest activity", so a CI rerun or a chatty comment
no longer resets the clock.

| Stage / state | Budget |
| --- | --- |
| `working` (no new turn event) | 45 min |
| `idle_undeclared` | 10 min |
| PR open, CI pending | 40 min |
| release workflow running | 60 min |
| `needs_approval` not covered by the label | 0 (escalate now) |

Past deadline: first a nudge, then `stalled` plus one alert (the same two-step
contract as today). Every action is recorded once per (issue, action,
fingerprint) so a missed tick or a restart repeats nothing.

An **owner lease** binds an issue to the orchestrator that acts on it:
`continuity_owner_leases(issue_key, owner_id, owner_epoch, lease_until)` with
compare-and-swap renewal (same fence rules as `factory_observer_runs.lease`).
Wakes for an issue go to the lease holder; an expired lease makes the issue
claimable again, so a dead orchestrator is detected within one lease period.

**Reaping** replaces the report-only lifecycle scan for sessions that are
`done` and accepted (linked PR merged), `ended`, or `idle_undeclared` past
24 h with no open PR, no live lease and no enabled watcher. Reap means
terminate through harnessd (worktree and history kept) plus `archived_at` and an
audit event. Data is never deleted. A dry run lists candidates first; the
apply path records each session in the issue/ledger audit trail.

## 4. The single decision function (gap 1)

`drover.server.continuity_loop.decide(facts, now, policy) -> Decision` is pure.
`facts` is the normalised issue shape `drover.py` already produces (labels,
markers, linked PRs with CI, release, bot markers, worker session and its
state). It returns at most one action per issue:

`none | nudge | stall | handoff | auto_approve | wake(reason)`

The first PR ports `drover.py._decide` unchanged in behaviour, so parity is
measurable, then extends it with worker states and deadlines. It lives in the
hub next to the rules engine and is called from two triggers:

- the rules worker's event path (`work_item.event`, GitHub webhook facts) for
  latency, and
- a level-triggered sweep (10 min) that loads facts through the hub's
  `GitHubProvider` for correctness.

Mechanical PR writes (open draft, update branch, exact-head merge) stay in the
existing `WorkItemRuleWorker` rules; `decide` only owns continuity actions.
The two share the durable action identity table (`work_item_rule_actions`, key
`(item, rule, sha)`), so there is one idempotency mechanism.

## 5. Outbox wakes with ack (gap 6)

A decision that needs an agent becomes a row in `continuity_wakes`, written in
the same transaction as the decision record:

```
wake_id, issue_key, action, fingerprint UNIQUE (issue_key, action, fingerprint),
payload_json, state pending|delivered|acknowledged|exhausted,
owner_id, owner_epoch, attempts, next_delivery_at, acked_at
```

An owner consumes under its lease (`consume` returns the oldest deliverable
wake and marks it `delivered` with a visibility timeout), acts, then
`acknowledge`s with the wake id. A wake that is delivered but not acked by
`next_delivery_at` is delivered again, up to a bounded attempt budget, then
marked `exhausted` and alerted. This is the owner-plugin protocol from #774
(poll / lease / consume / acknowledge, epoch fence, 3 deliveries with backoff)
applied to issue-level wakes instead of observer runs; the request/response
vocabulary is reused so the OpenClaw plugin needs a new operation target, not a
new protocol. Delivery is at-least-once; consumers dedupe on `wake_id`.

Delivering into a chat is the consumer's job (OpenClaw cron turn or the owner
plugin). A killed orchestrator turn leaves the wake unacked, and it is
re-delivered on the next consume after the visibility timeout. Watchers
become optional nudges.

## 6. Migration from `drover.py reconcile`

| Step | PR | `drover.py` | Hub |
| --- | --- | --- | --- |
| a | worker states + authority injection | prepends Authority + state contract to launch/nudge | projects `worker_state`; exposes it on `/harness/sessions` |
| b | deadlines, leases, reap | `reap` uses `worker_state` and leases | lease table, deadline policy, reap with audit (dry run first) |
| c | `decide` in shadow | `reconcile --shadow` posts its facts and decisions | `decide` runs on the same facts; mismatches recorded and printed |
| d | outbox wakes + ack | none | `continuity_wakes`, consume/ack endpoints |
| e | flip | `reconcile` becomes: GET hub decisions, apply GitHub writes, consume/ack wakes | hub is the decider; `drover.py` keeps only the GitHub write path until #786 gives the hub its own identity |

Parity gate for (e): zero mismatches on the live approved issues
(#777 to #781 and today's #784 to #786) over at least three consecutive ticks,
and the deliberate-kill test (consume a wake, kill the turn, observe
re-delivery) passes.

Rollback at every step is the previous `drover.py` path: the cron command is
unchanged until (e), and (e) keeps `--local` to decide in-process.

## Out of scope here

Hub read timeouts (#778) and the agent GitHub identity (#786) are owned
elsewhere. Until #786 lands, GitHub writes keep going through the operator
credential in `drover.py`, and the hub only decides.
