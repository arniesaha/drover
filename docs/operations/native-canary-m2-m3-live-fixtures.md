# Disposable native-canary M2/M3 fixtures

This is a source-bound owner runbook for the existing isolated #924/#848
canary. It is deliberately not an executable dispatcher, a cron job, or an
acceptance record. It never authorizes a direct store write, an owner turn,
an acknowledgement, or a provider/account change.

## Source contract

The only supported worker ingress is the configured `local_file` provider:

1. `LocalCandidateProvider` validates the versioned, goal-bound manifest in
   `src/drover/server/work_queue/local_candidates.py`.
2. `WorkService` and `QueueExecutionWorker` admit it normally; no caller may
   call a coordinator method to manufacture a launch.
3. `HarnessBridge._work_coordinator_binding` in
   `src/drover/server/work_queue/runtime.py` signs the run ID, native owner,
   immutable expected-effect artifact, and (when selected) serialization role.
4. `web/app.py:_ingest_harness_events` commits worker facts before its best-effort
   native-owner kick. `NativeOwnerRunner` then leases, consumes, claims, writes
   an action intent, verifies the exact artifact, and acknowledges the same wake.

For every run, retain a private, sanitized manifest joining these keys: candidate
number; `run_key` and `work-<run_key>`; source session and worker turn; source
event IDs; `wake_id`; owner session and `client_turn_id`; owner epoch and lease;
`action_fingerprint`; expected artifact; action-intent state; acknowledgement
timestamp; and the hub/daemon source and config hashes. A missing value remains
explicitly missing; it is never inferred from an aggregate count.

The existing fixture-only coverage remains in
`tests/test_continuity_924_acceptance.py`:
`test_redelivery_and_interrupted_owner_reconcile_one_effect_and_keep_ready_work`,
`test_two_ready_tasks_overlap_in_separate_fixture_worktrees_under_caps`, and
`test_human_gate_blocks_only_its_branch`. Those tests prove source behavior, not
these live fixtures.

## M2 — one mid-flight hub interruption and same-wake recovery

The integration owner performs these steps only in the approved disposable
allocation, after recording a positive supported allowance readback. This worker
does not edit the manifest or restart a service.

1. Add exactly one fresh ordinary local candidate using the already accepted
   manifest shape and policy labels. Its body must make one harmless marker in
   its assigned worktree. Its immutable
   `expected_effect_artifact` must be the matching
   `local-file:.drover-native-canary/<path>` reference. Add one already-ready,
   independent local candidate before the interruption; it is the preserved-ready
   witness. Use no direct queue, owner, or database API.
2. Let `WorkService -> QueueExecutionWorker -> HarnessBridge` launch the worker.
   Confirm the source worker and the `worker_started` and `worker_done` reports
   through ordinary paired readbacks. Do not create a replacement candidate when
   either report is pending.
3. Stop only at the observable mid-flight boundary: the final `worker_done`
   wake has one durable native-owner claim/turn, is not acknowledged, and no
   duplicate expected marker exists. If the ACK settles before this boundary,
   retain that outcome and stop; do not retry the fixture merely to force a race.
4. The integration owner restarts only the disposable hub using its approved
   launcher. The daemon, PostgreSQL allocation, manifest, and credentials remain
   untouched. After readiness, observe automatic recovery of the **same** wake
   ID. No manual claim, consume, action-intent, ACK, requeue, or source turn is
   permitted.
5. Pass only when the old epoch cannot ACK, the recovery checkpoint/journal and
   successor epoch are durable, the expected worker marker has one stable hash,
   the final action receipt and ACK name the same wake and artifact, and the
   independent ready candidate was retained and later advances normally. A hub
   restart that occurs after an ACK is not M2 evidence.

`NativeOwnerRunner._settle_expired_claims`, `_recover_abandoned_claim`, and
`FactoryObserverContinuity.lease/consume/action_intent/acknowledge` are the
relevant implementation boundaries. The retry design remains delivery-only:
it must not replay a provider turn or materialized effect, must respect active
claims and epochs, and must stop for human, authority, cancellation, budget, or
permanent-auth gates. The separate durable-transient-retry recommendation is
unchanged; this fixture does not raise a retry limit.

## M3 — ordinary multistage, overlap, integration fence, and human gate

Run each item through the same ordinary local-file ingress. An operator may
prepare the disposable manifest/config only after review; this document does
not make that mutation.

### Multistage candidate

Use one candidate whose harmless, assigned-worktree task records:

- analysis and its bounded justification;
- a local implementation artifact;
- a focused test command and result; and
- a review/next-action artifact.

The final reviewed artifact is the signed `expected_effect_artifact`. Evidence
must join the worker session/turn and all source events to the final owner
receipt and ACK. A chat message, a direct coordinator call, or a cron trigger
is not an unattended stage.

### Independent overlap and fenced integration

Submit two independent ready candidates with distinct assigned worktrees while
the existing cap is two. Record their real UTC and monotonic start/finish
intervals from source sessions and prove a nonempty overlap; do not add a sleep,
barrier, or external launcher merely to manufacture one. If they do not overlap,
record the result as a failed fixture.

The conflicting candidate must use the existing configured
`integration_serialization_label` on its normal manifest item. The matching
`GoalScope.integration_serialization_key` causes `HarnessBridge` to sign
`delivery_role=integration_serialized`, repository, and key. Retain the durable
reservation generation and owner epoch. The second conflicting item must remain
fenced while the first reservation is live, then proceed only after the matching
fenced ACK releases it. If the disposable config lacks both reviewed fields,
this is a configuration prerequisite: stop and ask the integration owner to
stage it; never simulate the reservation.

### Human-gated branch

Use a real scope whose signed binding has deployment authority, leave its human
approval unanswered, and retain its pending wake without an owner action or ACK.
At the same time, a separate `implementation` candidate must advance through
normal ingress. The gate must not be represented by a marker, a label change,
or an injected acknowledgement.

## Evidence decision table

| Fixture | Required direct evidence | Stop condition |
| --- | --- | --- |
| M2 restart/redelivery | Same `wake_id`; predecessor and successor epochs; recovery journal; one expected-marker hash; exact receipt and ACK; preserved ready link | Final wake already ACKed before interruption, uncertain effect, missing lease/claim, or any manual lifecycle operation |
| M3 multistage | Source event/receipt/session/turn timestamps; implementation/test/review artifacts; exact final artifact receipt and ACK | Any stage lacks a durable source-to-effect join |
| M3 overlap/fence | Two distinct worktrees and overlapping real intervals; reservation/key/generation; fenced conflicting item | No actual interval overlap or no configured serialization binding |
| M3 human gate | Unanswered deployment wake and zero owner action/ACK; unrelated implementation completion | Any fabricated approval or gate action |

The final run report must distinguish these live artifacts from fixture tests.
Nothing here proves #924 or #848 until the corresponding direct records exist.
