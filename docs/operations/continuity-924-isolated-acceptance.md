# #924 isolated continuity acceptance receipts

These are **fixture receipts, not live acceptance**. The implementation under
test starts at `34bc500e8d0d05e877eb5fe779a8f1d38d07f9ca`. This increment adds
tests, this runbook, and a captured receipt report; it changes no runtime code.
The recorded execution is confined to the isolated writer worktree.

Run the focused suite in the foreground from that worktree:

```sh
/Users/arnab/.local/bin/uv run pytest -q tests/test_continuity_924_acceptance.py tests/test_native_continuity_owner.py tests/test_factory_observer_continuity.py tests/test_work_coordinator_continuity.py tests/test_idle_undeclared_continuity.py --basetemp=.pytest_cache/924-acceptance -o tmp_path_retention_policy=all
```

The four new tests each write `receipt.json` next to their isolated
`control.duckdb` and effects. Add `-s` to print receipts. The retention override
keeps passing databases and effects available for inspection; rerunning this
same basetemp replaces that local evidence. Copy receipts before rerunning if
they must be retained. The checked-in [capture](../receipts/924-isolated-acceptance.json)
contains the exact values from the reported run, its command, implementation
head, and SHA-256 hashes of the test and production source files it exercised.
It is a captured result, not a golden snapshot that makes tests pass.

| Requirement | Fixture test in [test_continuity_924_acceptance.py](../../tests/test_continuity_924_acceptance.py) | Required receipt evidence |
| --- | --- | --- |
| Unattended stages | `test_unattended_three_stage_event_to_effect_receipts` | Three distinct submitted turns, three complete action receipts and local effects, all inbox rows acknowledged and claims completed; two subsequent completion projections link source event IDs to wake IDs. |
| Independent ready tasks | `test_two_ready_tasks_overlap_in_separate_fixture_worktrees_under_caps` | Two ready tasks admitted at global cap 2 / per-goal cap 1; distinct real Git worktree admin directories; measured UTC/monotonic intervals overlap; global and per-goal denials occur; a denied ready task is subsequently admitted after capacity frees. |
| Interruption/redelivery | `test_redelivery_and_interrupted_owner_reconcile_one_effect_and_keep_ready_work` | The original wake has two delivery attempts and the same deterministic turn ID; its effect marker is unchanged after recovery; recovery journal is `recovered`; ready work remains pending through recovery and then completes with its own single effect. |
| Human gate isolation | `test_human_gate_blocks_only_its_branch` | Deployment-scoped branch receives zero automatic turns; unrelated branch reaches complete receipt, acknowledged inbox and completed claim. A separately labelled negative lease/consume/ack probe is refused by the actual deployment approval guard. |

The native fixtures reuse `_runner`, `Collector`, and `_ingest_owner_events`
from [test_native_continuity_owner.py](../../tests/test_native_continuity_owner.py).
The handler is invoked directly, with fixture authentication disabled, without
opening a listener. External harness submission is replaced with a fixture
callback that first records the submitted input through the existing collector,
persists `action_intent`, writes a local wake-keyed effect marker, verifies it,
completes the receipt and acknowledges the inbox delivery. Subsequent fixture
producer events enter the real ingest handler; no cron, chat, scheduler thread,
or direct test call to `process_once` dispatches the multistage successor turns.
This proves event-driven fixture effects, not autonomous agent reasoning.

Continuity events, leases, claims, receipts and recovery use a declared synthetic
UTC clock starting `2026-10-09T00:00:00+00:00`; those times are not wall-clock
latency measurements. Host registration retains wall time solely for the normal
liveness check. Only the overlap test uses measured wall UTC and monotonic
nanoseconds. Two foreground joined threads synchronize at a barrier, then write
in separate worktrees of a tiny local fixture repository. This is actual fixture
execution overlap, not evidence of two real harness agents running concurrently.

The cap test runs the production `QueueExecutionCoordinator.launch_candidate`
and `HarnessBridge.active` against durable fixture registry sessions. Its
`service.launch` and `store.links` boundaries are fixture adapters; it does not
exercise PostgreSQL `WorkStore`, the complete provider/quota launch path,
daemon transport, or production worktree allocation. The configured caps here
are the existing worker caps. This does not establish a separate global native
owner cap. Each admitted fixture session has its own worktree identity recorded
in the report. The fixture does not change any installed configuration.

The interruption is an injected exception after the local effect and before
receipt completion/ack, followed by fixture removal of the lost owner's session
and input rows. A fresh `NativeOwnerRunner` uses the existing missing-owner
recovery journal, lease fencing and same-wake redelivery path. It reconciles the
existing marker instead of writing again. This covers that recovery window;
it is not a real daemon kill, machine crash, or arbitrary external API
exactly-once guarantee. Safe replay depends on an effect target that supports
the same deterministic marker/reconciliation contract.

The gated branch is deliberately deployment-scoped. `owner_runs` excludes it
before automatic dispatch. The explicit negative probe is test verification,
not an unattended deployment attempt: it records an unauthorized delivery but
cannot acknowledge it. Its null turn/receipt/artifact values are expected.
No human decision is fabricated, and no deployment operation is performed.

The source path for interpreting the receipts is:

1. [web/app.py](../../src/drover/server/web/app.py),
   `_MetricsHandler._ingest_harness_events`: source transaction commits before
   the non-failing owner kick; duplicate ingest can retry a failed kick.
2. [harness/registry.py](../../src/drover/server/harness/registry.py),
   `ingest_structured_events` and `has_completed_turn`: transactional projection
   and durable completion within the submitted turn's input window.
3. [harness/continuity.py](../../src/drover/server/harness/continuity.py),
   `record_observer_worker_completion`, `consume`, `action_intent`, `acknowledge`
   and `owner_runs`: projection identity, bounded redelivery, fenced receipt/ack
   and the actual deployment approval boundary.
4. [native_owner.py](../../src/drover/server/work_queue/native_owner.py),
   `_claim`, `_complete_finished_claim`, `_recover_abandoned_claim`,
   `_settle_expired_claims` and `process_once`: submitted-turn fences and durable
   recovery without replacing the inbox contract.
5. [reconciler.py](../../src/drover/server/work_queue/reconciler.py),
   `QueueExecutionCoordinator.launch_candidate`, and
   [runtime.py](../../src/drover/server/work_queue/runtime.py),
   `HarnessBridge.active`: existing global/per-goal admission and active slots.

For read-only inspection of a retained fixture database, join
`factory_observer_inbox.event_id` to
`continuity_owner_turn_claims.wake_id` and
`continuity_owner_action_intents.wake_id`. The complete receipt key is
`(wake_id, action_fingerprint)`. Join source projections using
`factory_observer_completion_receipts.continuity_event_id`; inspect
`continuity_owner_recoveries.wake_id` for recovery. `_receipts` in the test is
the exact executed SELECT. The JSON includes event, turn, intent, verification,
ack and claim timestamps; pending ready snapshots; attempts; and effect markers.
No database dump or synthetic bearer token is committed.

The **remaining live-only gate** requires a separately authorized disposable
deployment with actual PostgreSQL control storage and real hub/daemon/harness
transport, authenticated event delivery, and allocated independent worktrees.
Repeat the four scenarios with safe target effects and retain the same event →
wake → submitted turn → receipt → effect joins, real UTC timestamps, overlapping
execution intervals and cap decisions. Interrupt the actual owner process after
the target effect but before ack, then prove marker reconciliation, preserved
ready work, and no duplicate target write. Keep the real human gate unanswered
while an unrelated branch advances. Confirm progression comes from committed
events with cron/chat dispatch absent. Fixture success alone cannot satisfy
that gate or authorize production rollout. No live gate, push, PR, merge,
deployment, configuration change, or service operation was performed here.
