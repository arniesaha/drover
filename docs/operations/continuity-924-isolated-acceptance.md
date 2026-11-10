# #924 isolated acceptance

This procedure produces fixture evidence only. It does not establish live
acceptance. The implementation begins at commit
`34bc500e8d0d05e877eb5fe779a8f1d38d07f9ca`; this procedure changes no runtime
behavior or installed configuration.

From the isolated writer worktree, run the focused suite in the foreground:

```sh
uv run pytest -q tests/test_continuity_924_acceptance.py tests/test_native_continuity_owner.py tests/test_factory_observer_continuity.py tests/test_work_coordinator_continuity.py tests/test_idle_undeclared_continuity.py --basetemp=.pytest_cache/924-acceptance -o tmp_path_retention_policy=all
```

The four #924 tests retain a local `receipt.json` beside their isolated control
store and fixture effects. Add `-s` to print the receipt. Do not add generated
receipts or databases to source control. Copy a receipt to an approved private
evidence location before rerunning the same base directory.

| Scenario | Test | Receipt evidence |
| --- | --- | --- |
| Unattended stages | `test_unattended_three_stage_event_to_effect_receipts` | Three submitted turns, complete action receipts, acknowledged inbox rows, completed claims, and two completion projections linked to source events. |
| Ready work under caps | `test_two_ready_tasks_overlap_in_separate_fixture_worktrees_under_caps` | Two admitted tasks, distinct local Git worktree identities, overlapping measured intervals, cap denials, and later admission of preserved ready work. |
| Redelivery and recovery | `test_redelivery_and_interrupted_owner_reconcile_one_effect_and_keep_ready_work` | Two deliveries for one wake, one unchanged effect marker, recovered journal, and ready work retained through recovery. |
| Human gate isolation | `test_human_gate_blocks_only_its_branch` | Zero automatic turns for the deployment branch; unrelated ready work reaches a complete receipt. The explicit negative acknowledgement probe is refused by the approval guard. |

The fixtures reuse `_runner`, `Collector`, and `_ingest_owner_events` from
[test_native_continuity_owner.py](../../tests/test_native_continuity_owner.py).
They invoke the ingest handler directly without opening a listener. The
fixture callback records submitted input through the existing collector, writes
an action intent, creates a wake-keyed local effect, verifies it, completes the
receipt, and acknowledges the delivery. Later fixture events enter the same
ingest handler. No cron, chat, scheduler thread, or direct test dispatch starts
the successor turns.

Continuity timestamps use a declared synthetic UTC clock. They prove ordering,
not latency. The overlap test separately records wall-clock UTC and monotonic
nanoseconds. It joins two foreground threads at a barrier before each writes to
its own worktree. This is fixture execution overlap, not evidence of real
harness agents running concurrently.

The cap test uses the production
`QueueExecutionCoordinator.launch_candidate` and `HarnessBridge.active` over
durable registry sessions. Its launch and store boundaries are fixtures. It
does not exercise PostgreSQL work storage, provider routing, daemon transport,
or production worktree allocation. It also does not establish a separate global
native-owner cap.

The recovery case injects interruption after the local effect and before
receipt completion or acknowledgement. It then uses the existing missing-owner
recovery journal, lease fence, and same-wake redelivery path. The fixture proves
marker reconciliation for its local effect target. It does not prove arbitrary
external API exactly-once behavior, a machine crash, or daemon failure.

The deployment branch is excluded from automatic owner runs before dispatch.
The negative probe is test verification only: it creates no deployment effect,
and it cannot acknowledge the delivery. No human decision is fabricated.

Interpret a local receipt through these sources:

1. [web/app.py](../../src/drover/server/web/app.py): `_ingest_harness_events`
   commits source projections before its non-failing owner kick.
2. [harness/registry.py](../../src/drover/server/harness/registry.py):
   `ingest_structured_events` and `has_completed_turn` retain transactional
   projection and submitted-turn completion evidence.
3. [harness/continuity.py](../../src/drover/server/harness/continuity.py):
   projection, consume, receipt, acknowledgement, and deployment boundaries.
4. [native_owner.py](../../src/drover/server/work_queue/native_owner.py):
   claims, completion settlement, recovery, and redelivery fences.
5. [reconciler.py](../../src/drover/server/work_queue/reconciler.py) and
   [runtime.py](../../src/drover/server/work_queue/runtime.py): existing worker
   admission and active-slot calculation.

Join inbox `event_id` with claim and action-intent `wake_id`. The completed
receipt key is `(wake_id, action_fingerprint)`. Join completion projections by
their continuity event ID. The `_receipts` query in the fixture test is the
reference query and includes event, turn, receipt, effect verification, claim,
and acknowledgement timestamps.

The remaining live-only gate requires separately authorized disposable
infrastructure with PostgreSQL control storage, authenticated hub and daemon
transport, real harness sessions, and allocated independent worktrees. Repeat
the four scenarios using safe target effects and retain the event, wake, turn,
receipt, and effect joins with real timestamps. Interrupt a real owner after
the target effect and before acknowledgement, then prove marker reconciliation,
preserved ready work, and no duplicate target write. Keep the real human gate
unanswered while unrelated ready work advances. Confirm committed events drive
progress without cron or chat dispatch. Fixture success alone cannot satisfy
this gate or authorize a rollout.
