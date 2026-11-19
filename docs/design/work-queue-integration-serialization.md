# Work-queue integration serialization role

This is a design and acceptance runbook for #924 on top of the native-owner
architecture in [drover-native-continuity-owner.md](drover-native-continuity-owner.md).
It describes the bounded source implementation and its remaining acceptance
work. It does not create an integration owner, change authority, or establish
acceptance.

## Boundary

An **integration serialization role** is a logical delivery class for a
conflicting write. It is not a provider account, human identity, credential,
or a second harness session. Its authenticated principal remains the goal's
configured `continuity_owner_id`; its provider route and worktree allocation
remain those selected by the existing `WorkService` and harness adapter.

The role must retain the existing `implementation` / `commit_only` authority
scope. It must not reuse the existing `integration` authority scope: that scope
maps to `publish_review` in `harness/continuity.py`, so treating it as a
serialization label would broaden authority.

Independent ready work may continue through the existing worker cap path. A
conflicting write must first acquire the one durable serialization reservation
for its declared repository/key, then be delivered to the configured native
owner. The reservation is a fence, not a new executor identity. A release,
deployment, public write, configuration change, deletion, or payment remains
outside this role and requires the existing human-authority boundary.

## Inputs and durable contract

The normal launch path accepts a checked-in, explicitly authorized
serialization key only when all of these inputs are present:

| Input | Existing source of truth | Required behavior |
| --- | --- | --- |
| Goal, repository, policy and labels | `GoalScope` / `WorkService` | Resolve the goal and reject an unknown or ambiguous key. |
| Base owner | `GoalScope.continuity_owner_id` | Reuse the configured native owner; do not mint an owner or provider identity. |
| Worker profile and cap | `execution_enabled`, `execution_cwds`, worker coordinator | Admit independent source work through the existing cap calculation. |
| Serialization class and key | `GoalScope.integration_serialization_key` | Bind the conflicting action to one repository/key reservation before dispatch. |
| Optional delivery selector | `GoalScope.integration_serialization_label` | When configured, only a candidate carrying this operator-authority label enters the conflicting-write class; unlabelled work retains the standard delivery role. |
| Authority scope | Existing signed work-coordinator binding | Keep `implementation`; reject `integration` for this role. |

The hub-signed work-coordinator binding adds only immutable,
allowlisted fields such as `delivery_role: integration_serialized` and
`serialization_key`. It must continue to include the existing `run_id`, base
owner, authority scope, objective, and signature. The daemon must reject a
missing, duplicate, unknown, or mismatched role/key before it creates a
session. A host receives the signed binding but never the signing secret.

The durable reservation is keyed by repository and serialization key, and
contains the base owner, run id, generation, owner epoch, state, and acquired
or released timestamps. It is acquired with the signed coordinator binding,
fenced by the existing owner epoch on lease/consume/receipt/ack, and released
only by the matching fenced acknowledgement. The existing action intent key
`(wake_id, action_fingerprint)` remains the exactly-once receipt key; its
run is durably bound to the immutable serialization reservation. Existing
lease/consume/ack calls match the owner epoch and reservation generation.

## Fail-closed behavior

| Condition | Required result |
| --- | --- |
| Missing, unknown, or ambiguous serialization binding | Refuse launch and durable dispatch; leave unrelated ready work unchanged. |
| Reservation owned by a live conflicting action | Do not overlap the write; retain the candidate as ready/pending with bounded retry or human escalation. |
| Stale owner epoch or reservation generation | Refuse consume, receipt completion, acknowledgement, and release from the stale turn. |
| Duplicate source completion or redelivery | Reconcile the existing reservation and action intent; do not create another write or receipt. |
| Hub restart | Recover only an expired reservation/lease, fence the predecessor, and reconcile the existing receipt before redelivery. |
| Provider route or allowance unavailable | Deny launch before a session is created; do not substitute an account, credential, provider, or authority. |

The role does not make a human gate automatic. A deployment or other explicit
human gate remains blocked; an unrelated ready branch may proceed if it does
not require the held serialization key.

## Implemented source boundary and remaining delivery

The bounded source implementation changes and tests these normal paths:

| Boundary | Source boundary | Focused coverage |
| --- | --- | --- |
| Explicit metadata validation | `src/drover/server/work_queue/config.py` | Reject ambiguous keys and any authority escalation. |
| Signed launch binding | `src/drover/server/work_queue/runtime.py`, `src/drover/server/harness/work_coordinator.py` | Bind the logical role/key; reject tampering; retain `implementation`. |
| Host/session admission | `src/drover/server/harness/daemon.py`, `src/drover/server/harness/work_coordinator.py` | Reject malformed role/key before session creation. |
| Durable reservation and recovery | `src/drover/server/continuity_schema.py`, `src/drover/server/harness/continuity.py` | PostgreSQL transaction, generation and owner-epoch fences, acknowledgement release, and duplicate-redelivery reconciliation. |
| Native delivery | `src/drover/server/metrics.py` | Signed binding initializes the existing native owner run; the existing owner epoch fences stale acknowledgement. |
| Worker admission | `src/drover/server/work_queue/config.py`, `src/drover/server/work_queue/runtime.py` | Tagged work binds the logical role; independent task overlap still needs live evidence. |

The focused test set extends
`tests/test_work_queue_launch_postgres.py`,
`tests/test_native_continuity_owner.py`, and
`tests/test_factory_observer_continuity_postgres.py`. It must include a real
disposable PostgreSQL path for the reservation and must not fabricate an inbox
row, receipt, or integration binding.

## Evidence map

| Requirement | Direct evidence required after implementation | Current status |
| --- | --- | --- |
| #924.1 multistage continuation | Durable event, dispatch receipt, source/owner session and turn identities, implementation/test/review effects; no chat or cron trigger. | Open. |
| #924.2 parallel plus serialized conflict | Two independent worktree intervals overlap; one repository/key reservation has one epoch owner; conflicting candidate is fenced; receipts/acks and effects join to both runs. | Source and PostgreSQL reservation coverage exists; live combined evidence is open. |
| #924.3 interruption and redelivery | Interrupted owner, recovery checkpoint, successor fence, one receipt/effect, preserved ready work. | Open. |
| #924.4 human-gate isolation | Blocked branch remains unacknowledged; unrelated branch advances. | Open. |
| #924.5 regressions and runbook | Foreground focused tests and this document distinguish unit, fixture, and live proof. | Documentation exists; live proof is open. |
| #848 native canary | Plugin disabled; hub launches, acts exactly once, and acknowledges without an orchestrator. | Open. |
| #848 restart and three-job gate | Mid-flight hub recovery without duplicate action, then three unattended native jobs with zero OpenClaw execution. | Open. |
| #848 architecture documentation | Native ownership remains the execution/continuity layer; clients are optional. | Existing architecture is in `docs/design/drover-native-continuity-owner.md`; this document adds the planned serialization boundary only. |

## Acceptance run order

1. Run the focused source and PostgreSQL tests after the implementation exists.
2. In a disposable authenticated PostgreSQL hub/daemon environment, verify the
   provider route through the normal usage reporter before launch.
3. Launch two independently ready source tasks through `WorkService`; record
   separate worktrees and overlapping monotonic intervals.
4. Submit one actual conflicting candidate through the normal signed binding;
   retain its reservation, owner epoch, fence decision, receipt, effect, and
   acknowledgement. Do not infer serialization from the absence of a
   conflicting candidate.
5. Run the #924 recovery and human-gate scenarios separately. Run the #848
   plugin-disabled, restart, and three-job canary gates separately.

Until those direct records exist, this design and any fixture result are not
#924 or #848 acceptance.
