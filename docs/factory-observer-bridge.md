# Factory Observer Bridge

The Factory observer bridge is a narrow adapter over Drover's existing authenticated host-session launch route:

```text
POST /harness/hosts/<target_hostname>/sessions
Authorization: Bearer <existing Drover non-preflight credential>
```

The launch bridge is stateless. TaskFlow remains authoritative for Factory
state. Drover starts one constrained host-local structured harness session and
projects it in the existing Harness UI. The optional durable observer boundary
below records owner continuity in Drover; it adds no scheduler or Factory
lifecycle controls.

## Request contract

Send this exact body shape to the existing route. `target_hostname` must equal the host in the route and the host-local harness daemon checks it again.

```json
{
  "factory_observer": {
    "run_id": "run_FACTORY001",
    "expected_revision": 4,
    "idempotency_key": "factory.launch:run_FACTORY001:4",
    "target_hostname": "studio",
    "repo": {
      "owner": "arniesaha",
      "name": "drover",
      "branch": "factory/run_FACTORY001"
    },
    "worktree": {
      "cwd": "/absolute/path/to/drover",
      "policy": "isolated_required"
    },
    "command": "harness_default"
  },
  "harness": "codex",
  "model": "gpt-5",
  "thinking_effort": "medium"
}
```

`harness` and `model` are required. `thinking_effort` is optional (omission or
`null`); when supplied it must be a non-empty string supported by the selected
model's catalog. For Claude Code models advertising `reasoning: null`, use
`"harness": "claude-code", "model": "opus"` and omit `thinking_effort`.
Supplying an effort for such a model returns HTTP 400 with
"The selected reasoning effort is not supported by this model."

The bridge accepts no prompt, arbitrary command, or Factory mutation field. It forces Drover's existing `structured` launch mode, resolves the command from the selected harness adapter, and requires the existing isolated Git-worktree path to succeed. A non-Git working directory, disabled structured adapter, unavailable model selection, or worktree failure is rejected before a Factory observer session is started.

Isolation is required even when the adapter does not advertise the worktree
capability (including Claude Code). The host creates the same per-session Git
worktree used for Codex; it never falls back to the requested checkout for a
Factory launch. Non-Git directories or repositories without commits return 400;
worktree creation failures return 503. The existing session row records the
isolated cwd and requested repo owner/name/branch, while the projection records
the actual worktree path/branch. A Git worktree isolates checkout changes, not
all host filesystem access or credentials.

### Claude Code unattended permissions

Ordinary Claude structured sessions currently default to `bypassPermissions`.
Factory launches explicitly replace that flag with `--permission-mode dontAsk`,
including when a session is recovered after a daemon restart. The existing
Drover session field `permission_mode: "auto"` is a generic launch-policy label;
it does **not** select Claude's `auto` classifier mode or permission bypass.
The bridge accepts no permission override or bypass option.

[Claude Code documents `dontAsk`](https://code.claude.com/docs/en/permissions#permission-modes)
as denying tool calls that would otherwise prompt, while allowing tools that
need no approval or are already allowed by permission rules. Consequently an
unattended Factory session does not park waiting for tool approval, but may be
unable to complete edits or commands without preconfigured allow rules. Drover
does not add allow rules or disable managed policies. This is deliberately a
restricted unattended launch, not a guarantee that every delegated task can
complete. The driver's existing `control_request` / `control_response` approval
mapping is fixture-tested but has not been verified with a live approval capture
(see `tests/fixtures/structured/FINDINGS.md`); this launch does not rely on it.
Factory creation starts a session without a prompt; the existing turn endpoint
is still needed to submit work.

The Factory idempotency key is deterministically mapped to Drover's existing `client_session_id` uniqueness fence. Repeating the exact request returns the existing bounded session metadata rather than starting another host process or worktree.

## Observer projection and boundaries

The Harness UI derives a `factory_observer` display block from existing session correlation fields. It shows the Factory run ID, launch-bound expected revision, selected host, and the live Drover harness-session status. It labels TaskFlow as authoritative and suppresses the Harness UI's kill control for observer sessions.

That projection is intentionally not a Factory status API: `expected_revision` is the revision that authorized this launch, not a claim about the current Factory run. Drover does not call TaskFlow and cannot approve, cancel, advance, resume, finish, or otherwise mutate Factory state. Factory controls remain in the Factory/TaskFlow surface.

## Configuration and authentication

No new Drover configuration is required. The target host must already be paired/registered, reachable through its existing direct or relay route, and have the chosen harness's structured adapter and model catalog available. The caller needs an existing authenticated Drover bearer credential accepted by the hub; `preflight` credentials are read-only and are refused by the normal authentication gate. Use a dedicated revocable existing Drover credential for the Factory client rather than the legacy shared token where available.

The launch bridge requires no Factory credential or TaskFlow URL. The optional
continuity slice adds two tables through the normal control-store bootstrap
(PostgreSQL migration 10, and additive DuckDB DDL). No credential, configuration,
permission-policy, or production service change is part of this implementation.

## Durable owner continuity (#497)

This opt-in boundary reuses an **existing** observer session's run ID and
launch-bound revision. `factory_observer_runs` stores the objective, checkpoint,
immutable authority scope, single owner lease/epoch, worker observation, and
next owner action. `factory_observer_inbox` stores immutable reports, delivery
attempts, action intent, and acknowledgments. These are Drover observer facts,
not Factory lifecycle state. Reinitialization preserves the latest checkpoint;
a different session, objective, scope, or revision for the same run conflicts.
Changing those bindings is outside this slice.

The authenticated hub boundary is:

```text
GET  /harness/factory-observer/continuity?run_id=run_FACTORY001&limit=20
POST /harness/factory-observer/continuity
```

It uses the existing authentication gate, with no new credential or scope.
Preflight credentials cannot access it. POST bodies are capped at 16 KiB and
accept only these exact operation fields (no commands, approval grants, or
TaskFlow operations):

| operation | Required fields | Optional fields |
| --- | --- | --- |
| `initialize` | `session_id`, `objective`, `checkpoint` | `authority_scope` (default `implementation`) |
| `lease` | `run_id`, `owner_id` | `owner_epoch` for active renewal; `lease_seconds` (default 60) |
| `report` | `run_id`, `source`, `source_event_id`, `subject`, `sequence`, `kind`, `summary` | none |
| `consume` | `run_id`, `owner_id`, `owner_epoch` | none |
| `acknowledge` | `run_id`, `owner_id`, `owner_epoch`, `event_id`, `checkpoint` | none |

Every successful response contains `continuity`, the version-1 status
projection. A report response also contains `event_id`; consume also contains
`delivery` (an action or `null`). IDs are bounded to 191 characters, objective
and checkpoint to 4,000 characters, and report summary to 2,000 characters.
Bad requests return 400, missing runs/events 404, immutable identity or owner
fence conflicts 409, and a busy control-store window 503.

For example, after initialization and leasing, an OpenClaw adapter can report
and consume CI results through the same boundary:

```json
{
  "operation": "report",
  "run_id": "run_FACTORY001",
  "source": "openclaw",
  "source_event_id": "ci:commit-a:attempt-2:result",
  "subject": "ci:commit-a",
  "sequence": 2,
  "kind": "ci_red",
  "summary": "Focused tests failed; owner corrective commit required."
}
```

| Report kind | Next owner action |
| --- | --- |
| `worker_awaiting_input` | `request_input` |
| `worker_brief_completed` | `review_worker_result` |
| `ci_red` | `correct_ci` |
| `ci_green` | `review_ci` |

Awaiting input is distinct from completion of a worker brief. Neither implies
terminal release. The projection always reports `terminal_release: false`,
meaning this observer has inferred no release; it is not a statement about
TaskFlow's current lifecycle. CI green makes `owner_wake` true when a live,
authorized owner has work; it does not merge, deploy, publish, restart, launch
workers, or call TaskFlow. `owner_wake` is a polling hint, not a delivered push.
The adapter must actively poll and consume; no existing cron watcher or worker
producer is automatically rewired by this change.

Authority scope is immutable: `implementation` permits owner continuation
within commit-only authority, `integration` within publish/review authority,
and `deployment` projects `request_explicit_approval` with `authorized: false`.
The bridge rejects acknowledgment of a deployment action. Approval and its
subsequent release handoff require an external, explicitly approved integration;
there is no approval-grant endpoint here. Scope labels describe the adapter's
boundary and do not grant tools or alter worker permissions. An authorized
action is an advisory owner continuation, never an instruction for this server
to execute infrastructure or Factory controls.

## Delivery and recovery contract

The deterministic event ID is SHA-256 of canonical JSON containing `run_id`,
`source`, and `source_event_id`. Retries must retain the same identity **and
the entire report envelope**. Exact repeats return the existing event ID,
including after acknowledgment; divergent content under that identity conflicts.
Producers must use a distinct source identity for each worker transition or CI
attempt/result. `sequence` is a nonnegative, monotonic integer within one
`(run_id, source, subject)` stream. CI subjects must identify the commit/check
stream; a worker subject must identify the worker session. Arrival time is not
source order. A late or equal-sequence report remains reviewable as
`review_late_report` and cannot regress the worker observation. This does not
assert that all incident-ledger duplicate/late deliveries share one root cause.

One live lease exists per run, for 1–300 seconds. Active renewal requires the
same owner and epoch. At expiry (including the exact deadline), reclamation
increments the epoch, even for the same owner name. Consume and acknowledge
reject absent, expired, or stale owners. DuckDB's existing control-plane lock
serializes transactions; PostgreSQL locks the run row with `FOR UPDATE`.
An expired owner never causes automatic release, a worker launch, or a restart.

Consume commits the next action and delivery record together **before** replying.
Only one action may be outstanding; acknowledgment atomically records the new
owner checkpoint and event ack, then clears that action. Duplicate ack is a
no-op and cannot replace later progress. Adapter-side side effects must also
be idempotent on `action_id`: an owner can crash after acting and before ack.
This is at-least-once delivery with idempotent consumption, not exactly once.

There are at most three consume deliveries per event, with delays of 5, 10,
and 20 seconds. Early consumes return `delivery: null`. After the final delay,
the next consume marks the action `exhausted`; it remains durable for manual
reconciliation, and the current owner can acknowledge it after inspecting its
outcome. Lease reclamation preserves the same action ID, attempts, and retry
deadline; it does not reset the budget. An unrecoverable adapter failure must
stop automatic consumption and leave the durable record for the owner.

GET is a bounded read: default 20 unacknowledged reports, maximum 50, plus
counts, `has_more`, objective/checkpoint, owner fencing, scope, worker state,
and outstanding action. It neither delivers nor acknowledges work. Recovery
projects `claim_owner`, `reclaim_expired_owner`, `approval_required`,
`manual_attention`, or `continue_owner`. A bounded page may show only the first
reports; `next_action` always identifies the outstanding action independently
of that page. Consuming and acknowledging drains the inbox without a fragile
max-ID cursor. Acked records remain as durable dedupe fences; retention policy
and operator deletion are outside this slice.

The existing control-store copy/import inventory includes these tables.
Pre-#497 DuckDB snapshots may omit both; any supplied continuity table must
include its full durable schema. This preserves compatibility without silently
discarding a supplied checkpoint or inbox. No live store migration was run.

## OpenClaw adapter and Hermes extension seam

OpenClaw can use the HTTP contract now: initialize from the existing observer
launch, acquire/renew its owner lease, submit report-only worker/CI facts, poll
the version-1 projection, consume with its epoch, reconcile using `action_id`,
and acknowledge with the updated checkpoint. These HTTP and recovery contracts
are covered by focused Python tests; a live OpenClaw producer/owner loop has
not been integrated or exercised in this implementation.

A Hermes adapter should map its stable report IDs and ordered subject streams
into the same envelope, then implement the same lease, idempotent action, and
ack protocol. Adapter transport and owner wake mechanisms stay outside the
ledger; no Hermes-specific scheduler or Factory controls belong here. Hermes
has **not** been tested. Live PostgreSQL concurrency, owner transport wiring,
approval handoff, integration publication/review, and terminal release remain
integration/release verification boundaries.
