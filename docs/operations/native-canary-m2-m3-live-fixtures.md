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

## M2: one mid-flight hub interruption and same-wake recovery

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

## M3: ordinary multistage, overlap, integration fence, and human gate

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

### Separate-verifier provenance

A `verify` run is deliberately a new isolated Git worktree.  It is created from
the parent worker branch's committed `HEAD`; ignored or uncommitted files in
the worker worktree are not a handoff channel.  This preserves isolation and
prevents a verifier from treating shared mutable scratch as evidence.

Candidate 44 demonstrated the committed-provenance boundary but passed
prose-shaped `evidence_key` metadata to `complete`; that field is not a bound
completion input. It also required pytest, which is not part of the disposable
worker worktree contract. Its failure remains evidence and is not replayed.

[`fixtures/native-canary-m3-candidate-45-stdlib-verification.json`](fixtures/native-canary-m3-candidate-45-stdlib-verification.json)
is the next ordinary ingress record. Its work role creates harmless source and
a standard-library focused test plus the bound local-file evidence, force-adds
the ignored `.drover-native-canary` evidence files, and commits all of that
evidence on its own session branch before requesting deferred completion. Its
verify role only reads the resulting child worktree and reruns the committed
`python3` test. It must not recreate a missing marker, implementation, or test.
A missing, changed, or untracked-only file is a failed verification, not a
reason to copy files between worktrees.

The legacy canary brief remains marker-only and blocks a source commit even if
a manifest body requests one. For the separately reviewed committed-provenance
fixture, use the exact offline policy/goal delta in
[`fixtures/native-canary-m3-committed-provenance-binding.json`](fixtures/native-canary-m3-committed-provenance-binding.json)
and the ordinary ingress record
[`fixtures/native-canary-m3-candidate-48-committed-provenance.json`](fixtures/native-canary-m3-candidate-48-committed-provenance.json).
The policy default is false; the reviewed goal selector names candidate 48 and
the hub signs `committed_provenance` into its work binding. The body cannot
select this permission, verifier bindings never carry it, and the M3 brief only
allows a local session-branch commit of the named source/test/evidence files.
This is an owner-reviewable configuration delta, not an instruction to change
the live disposable configuration.

This uses the existing `create_session_worktree` provenance rule; it does not
add a shared path, cross-worktree read capability, or new authority. The exact
signed `local-file:` marker remains confined to the worker/child session branch
and the native owner continues to attest it through the bound daemon endpoint.

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

## Reviewed offline records

The next M2 input is the complete, schema-validated manifest at
[`fixtures/native-canary-m2-candidates-26-27.json`](fixtures/native-canary-m2-candidates-26-27.json).
It is an offline review record only; the integration owner may copy its two
items into the existing disposable manifest after independent review.

Both records use the current ordinary ingress shape:

- `version: 1`, `workspace_id`, and `goal_id` are `native-canary-924-848`.
- Candidate 26 is `state: open`, labelled `goal:native-canary-924-848`,
  `agent:nix`, `status:queued`, and binds
  `local-file:.drover-native-canary/source-task-26.json`.
- Candidate 27 uses the same normal queued labels and binds
  `local-file:.drover-native-canary/source-task-27.json`.
- Each task says that the harness-assigned disposable worktree is authoritative;
  a missing entry in the old static map is not another worker authorization gate.

`status:queued` is required: `ShadowReconciler` projects it as
`launch_candidate`, then `QueueExecutionWorker.process_once` is the sole
automatic path to `WorkService.launch_candidate`. Neither this document nor an
owner may call that method directly.

### Paired, sanitized observation command

After a reviewed record is staged, the owner can use this read-only observation
snippet. It reuses the approved paired helper in process memory, prints no
credential value, task body, transcript, or request headers, and does not use a
control-store module.

    PYTHONPATH=src DROVER_CANARY_RUNTIME=/path/to/approved-canary-runtime \
      /path/to/canary-python - <<'PY'
    import contextlib, io, json, os, runpy
    from pathlib import Path
    helper_path = (
        Path(os.environ["DROVER_CANARY_RUNTIME"])
        / "env/root-read-candidate-15.py"
    )
    with contextlib.redirect_stdout(io.StringIO()):
        helper = runpy.run_path(helper_path)
    request = helper["request"]
    token = helper["redeemed"]["token"]  # memory only: never print or persist
    for number in (26, 27):
        status, work = request(
            f"/work/get?goal_id=native-canary-924-848&number={number}",
            token=token,
        )
        links = work.get("links") if isinstance(work, dict) else []
        for link in links if isinstance(links, list) else []:
            if not isinstance(link, dict):
                continue
            safe_link = {key: link.get(key) for key in (
                "id", "run_key", "run_role", "session_id", "failure_count",
                "created_at", "updated_at", "last_progress_at",
            )}
            print(json.dumps({"candidate": number, "work_status": status,
                              "link": safe_link}, sort_keys=True))
            run_key = safe_link.get("run_key")
            if isinstance(run_key, str) and run_key:
                c_status, result = request(
                    "/harness/factory-observer/continuity?run_id=work-" + run_key,
                    token=token,
                )
                continuity = result.get("continuity", {}) if isinstance(result, dict) else {}
                print(json.dumps({
                    "candidate": number, "continuity_status": c_status,
                    "run_id": "work-" + run_key,
                    "owner": continuity.get("owner"),
                    "owner_handoff": continuity.get("owner_handoff"),
                    "next_action": continuity.get("next_action"),
                    "events": continuity.get("events"),
                }, sort_keys=True))
    PY

The paired run readback identifies the worker link, source events, wake state,
owner epoch/lease, and final action intent. It intentionally does **not** reveal
an owner session or `client_turn_id` to the device principal. The owner must
locate those identifiers through the existing authenticated, sanitized
owner/coordinator readback, recording only wake ID, session ID, turn ID, epoch,
lease, action fingerprint and action-intent state. A database query or
transcript endpoint is not an alternative.

### Precise M2 interruption boundary

Restart the disposable hub only after the candidate-26 final `worker_done` wake
has all of these machine-readable facts:

1. its source worker is terminal and the bound daemon endpoint reports the
   exact expected artifact `materialized`;
2. continuity names a final wake with a nonempty action intent for that same
   artifact and a durable owner epoch/lease or turn claim;
3. the durable action receipt/intent is complete, but that exact wake is not
   `acknowledged`; and
4. there is exactly one regular owner-marker hash and no second effect hash.

This is after a materialized **owner** effect and before its ACK, not merely
after the worker-side artifact. If an ACK has already settled, retain ordinary
success and stop rather than manufacturing a race. After the permitted hub
restart, exact-once evidence requires the same wake ID, a recovery checkpoint,
a successor epoch that fences the predecessor, an unchanged marker hash, and a
receipt/ACK for the same expected artifact. A new candidate, changed artifact,
or second marker fails the fixture.

Candidate 27 is a real ready witness only while `work/get` has no link and the
queue report projects `launch_candidate`. With the current cap of two it may
automatically launch before the restart; that proves preservation and progress,
but not that it remained ready. The stronger no-lost-ready observation requires
that same queued state while two ordinary worker links consume the cap, without
a manual hold, sleep, or barrier. Otherwise report the actual state honestly.

### Marker scope

The candidate-bound expected effect is verified only against the daemon-assigned
worker session CWD and its signed `local-file:` reference. An owner action may
write its own reconciliation marker in a separately assigned owner worktree,
but that marker is an action receipt aid, not the candidate's materialized
effect. The retained candidate-26 marker under the historical
`runtime/worktrees/work-a/.drover-native-canary/actions/` directory therefore
must not be reused as a worker effect or copied into later M2/M3 fixtures. New
fixtures must bind their marker to the worker's assigned session worktree and
prove that exact signed reference through the daemon readback.

## M3 offline inputs and explicit source limits

[`fixtures/native-canary-m3-candidates-28-32.json`](fixtures/native-canary-m3-candidates-28-32.json)
contains exact normal ingress records for:

- candidate 28, whose worker writes analysis, implementation and focused-test
  artifacts, then uses the existing `drover_work_update` tool with
  `action=complete`. This is the supported transition: `WorkService.update`
  changes the item to `status:review` and launches a separate `verify` run with
  the parent worker session. One prose completion is not several native stages;
- candidates 29 and 30, independent harmless effects whose real intervals must
  overlap under cap two without a barrier or sleep; and
- candidates 31 and 32, each carrying `delivery:integration`, so the existing
  goal's serialization label/key select a normal signed
  `delivery_role=integration_serialized` binding. Actual reservation generation,
  epoch, fence and release-after-ACK evidence remains required; and
- candidate 33, the genuine human-gated branch. Its manifest has no authority
  label or scope field. The reviewed policy and goal-scope delta alone select
  deployment authority for this exact candidate number.

[`fixtures/native-canary-m3-signed-binding-input.json`](fixtures/native-canary-m3-signed-binding-input.json)
contains the offline, review-only policy opt-in
`local_candidate_deployment_scope_enabled: true` and the goal selector
`local_candidate_authority_scopes: {"33": "deployment"}`. It is not a TOML
patch and has no HMAC: only `HarnessBridge` can sign the normal launch binding
with the canary secret.

The selector is deliberately narrow. `GoalScope` accepts it only for an exact
positive local candidate number, its own `drover:owner/<goal>` native owner, and
a policy that explicitly enables deployment selection. The local manifest
schema still rejects `authority_scope`, the worker body cannot choose scope, and
a deployment-selected candidate cannot select integration serialization.
`HarnessBridge` signs the resulting deployment scope. If the original create
response is lost, registry recovery fails closed rather than recomputing that
authority from changed configuration.

For candidate 33, a terminal worker report must yield the signed deployment
continuity run and `request_explicit_approval`, with no executor wake, owner
effect, or ACK. Its paired human decision remains unanswered. In the same
observation window an independent implementation candidate, such as 29 or 30,
must advance normally. Approval is not part of this fixture. The focused tests
`test_policy_opted_local_deployment_selector_is_hub_signed`,
`test_local_candidate_manifest_rejects_caller_selected_deployment_scope`, and
the existing continuity deployment-gate tests cover the selector, hub HMAC,
manifest rejection, and fail-closed human gate.
