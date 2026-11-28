# #1043 GitHub event-path adoption packet

This packet prepares a distinct, source-pinned adoption task for PR #1042 at
`f68653d9be5adbe8eed1ba53a832d5ebb55d7dbc`. It is not an installation
record, a GitHub delivery, a native-owner acknowledgement, or an accepted
outcome. P1 remains accepted historical evidence for #1031 only; its profile,
producer check, bindings, and closed issue are not reusable proof for #1043.
The original P2/P3 ledger remains separately not accepted.

## Reviewed contract

The proposed profile is [profile-candidate.json](profile-candidate.json):
`independent_check`, PR #1042, App `15368`, behavioral check
`github-event-adoption-1043-outcome`, and revision
`e126e03963bcb5cf65c1db3d1f017ab9c70c293ce54309874ea030c26d84205b`.
The exact issue marker is in [pilot-issue-body.md](pilot-issue-body.md).

The protected-main workflow [github-event-adoption-1043.yml](../../../.github/workflows/github-event-adoption-1043.yml)
has two separate jobs:

- `proof` fetches the immutable candidate without a GitHub credential, uses an
  isolated PostgreSQL service, and runs the current PR1042 regressions:
  `test_opted_provider_poll_rescans_pending_ci_and_restarts_without_duplicate_pair`,
  both parameterizations of
  `test_provider_poller_isolates_target_failures_and_keeps_its_thread_alive`,
  and `test_host_deferred_close_recovery_redelivery_rebuilds_plain_evidence_outcome`.
  It also requires the owner-held wrong-source proof rejection.
- `publish` fetches protected-main producer code only. It receives the
  short-lived Actions token with `checks: write`, does not fetch or execute the
  candidate checkout, and either publishes the exact output or reconciles an
  identical prior check.

`run-proof.py` writes source, lock hash, interpreter, JUnit counts, command
arguments, and log hashes. It fails for a skipped, failed, or errored selected
test. `publish-check.py` rejects a changed source, count, command shape, or
invalid-result observation before it can use its token. These are source-level
proof boundaries. They do not establish a live GitHub API-polling observation,
a configured profile, a close, a native effect, or an ACK.

## Fresh-session local verification

Use the root `AGENTS.md` disposable socket-only PostgreSQL recipe. This is the
literal focused command portion to substitute after its interpreter, `uv`,
fixture, `DROVER_TEST_POSTGRES_DSN`, and trap setup; it preserves fixture logs
under the newly-created `$scratch_root` and the trap stops only that cluster.

```sh
sh scripts/quality/preflight.sh --out "$scratch_root" \
  --python "$UV_PROJECT_ENVIRONMENT/bin/python" \
  --expect-worktree "$repo_root" --expect-base "$(git rev-parse HEAD)" \
  --require-clean --profile focused-tests-pg \
  --pg-dsn-env DROVER_TEST_POSTGRES_DSN --pg-expect-role "$(id -un)" \
  --pg-expect-data-prefix "$scratch_root/pg" --pg-forbid-endpoint 5432 \
  --then '{python}' -m pytest -p no:cacheprovider -q \
  tests/test_work_queue_github_acceptance.py::test_opted_provider_poll_rescans_pending_ci_and_restarts_without_duplicate_pair \
  tests/test_work_queue_github_acceptance.py::test_provider_poller_isolates_target_failures_and_keeps_its_thread_alive \
  tests/test_work_queue_github_acceptance.py::test_host_deferred_close_recovery_redelivery_rebuilds_plain_evidence_outcome \
  tests/test_github_event_adoption_1043_packet.py \
  --basetemp="$scratch_root/pytest"
"$uv_bin" run --locked --extra dev --extra postgres python -m black --check \
  tests/test_github_event_adoption_1043_packet.py \
  docs/quality-pilots/github-event-adoption-1043/run-proof.py \
  docs/quality-pilots/github-event-adoption-1043/publish-check.py \
  docs/quality-pilots/github-event-adoption-1043/prepare-private-config.py \
  docs/quality-pilots/github-event-adoption-1043/validate-private-config.py
git diff --check
```

The hosted workflow separately verifies the candidate at the pinned source.
The local receipt validates this packet and its source regressions; neither is
live acceptance.

## Owner-held private configuration preparation

The installed provider transport is authenticated **GitHub API POLLING**, never
a pushed webhook. Its cadence and retry are at least 60 seconds. The existing
signed-receiver delivery-table loop (0.25 seconds with five-second retained
continuation retries) remains a different transport. The bootstrap model cron
is repair only and cannot drive this stage.

After protected review/CI and before any private installation, use only an
owner-held copy of the current configuration. These scripts read configuration,
write a JSON projection beneath owner scratch, and never contact GitHub, a
service, a store, or a harness:

```sh
repo_root=$(git rev-parse --show-toplevel)
scratch_root=$(mktemp -d "$HOME/scratch/github-event-adoption-1043.XXXXXX")
config_before=${DROVER_OWNER_CONFIG:?set the owner-held configuration path}
python_bin=${DROVER_TEST_PYTHON:-$(command -v python3)}
uv_bin=${DROVER_TEST_UV:-$(command -v uv)}
export UV_PROJECT_ENVIRONMENT="$scratch_root/venv" UV_CACHE_DIR="$scratch_root/uv-cache"
"$uv_bin" sync --locked --extra dev --extra postgres --python "$python_bin"
"$UV_PROJECT_ENVIRONMENT/bin/python" -I \
  "$repo_root/docs/quality-pilots/github-event-adoption-1043/prepare-private-config.py" \
  --baseline-config "$config_before" \
  --native-template-goal native-owner-canary-848 \
  --profile "$repo_root/docs/quality-pilots/github-event-adoption-1043/profile-candidate.json" \
  --output "$scratch_root/work-queue-projection.json"
"$UV_PROJECT_ENVIRONMENT/bin/python" -I \
  "$repo_root/docs/quality-pilots/github-event-adoption-1043/validate-private-config.py" \
  --baseline-config "$config_before" \
  --projection "$scratch_root/work-queue-projection.json"
```

The projection derives the live supported native harness/model, execution CWD
and brief from the named native-template goal; it copies the existing
`DROVER_WORK_QUEUE_GITHUB_TOKEN` *reference* and never prints a credential
value. It preserves `execution.global_max_workers == 2`, uses
`drover:owner/github-event-adoption-1043`, and a goal-local integration fence.
Its only #1043 mappings are `github_acceptance_profiles`,
`github_event_delivery_profiles`, and `github_provider_ingestion_profiles`, all
at the revision above. It turns polling on with batch two and cadence/retry 60
seconds. It validates that the entire P1 goal's canonical digest did not change.

There is no WorkQueue API that writes server configuration. The owner must
apply the validated projection with the normal private configuration installer,
then run the same validator with `--candidate-config` against the staged final
TOML before activation. The script rejects a copied or modified P1 goal.

The existing P1 A/B sessions cannot become #1043 bindings: the durable
session-owner rule permits reuse only for the same goal, issue, role, profile
revision, and source head. P1 is accepted on different #1031/PR1029 facts, so
this packet deliberately has no `github_existing_session_bindings`. Fresh,
distinct #1043 work and verifier turns are pending until after this profile's
publisher output exists. Do not invent IDs in configuration.

## Owner live driver after installation

All commands below are owner actions after review, protected merge,
merged-source checks, compatible private install with rollback/health proof,
and the separate proof/publisher outcome. They are not commands executed by
this packet.

1. Read the exact PR #1042 head, required check IDs/conclusions, published
   behavioral check ID/App/external ID/output digest, current profile revision,
   and poller health. Any unavailable, stale, wrong-source, failed, skipped or
   missing fact stops the task. A poller observation is not a verdict.
2. With #1043 at `status:review`, create the distinct work using the supported
   coordinator request: `POST /work/launch` with
   `{"goal_id":"github-event-adoption-1043","number":1043,"request_key":"owner-recorded-unique-key","estimated_cost":1}`.
   Preserve the returned work run key and its signed turn/session provenance.
   Complete it only after its actual terminal success using `POST /work/update`
   with its returned `run_key`, `action:"complete"`, and the normal authorized
   worker identity. The service re-reads current provider facts; it will defer
   while CI is pending.
3. Let installed API polling observe the exact configured item; do not POST a
   copied webhook payload and do not use a cron tick as a stage driver. Record
   the poller health counters, source/profile/head, required-check rollup, and
   resulting verifier run key. Confirm the verifier is distinct and terminal.
4. Submit `POST /work/update` for that verifier with
   `action:"verified"` and `evidence_key:"check-run:<published-id>"`.
   The request supplies no success flags. Confirm current source and evidence
   are reread, then retain the verified intent/outcome, one close, native wake,
   owner epoch, action receipt/fingerprint and ACK correlations.
5. Replay the real provider observation and repeat the exact verified request.
   Expect no second work/verifier pair, close, wake, receipt or ACK. For the
   owner-approved isolated crash exercise, stop only after the native receipt
   and before ACK, restart the owned component, and confirm reconciliation of
   the exact receipt rather than a repeated effect. Preserve stale head,
   revoked mapping, wrong App/check, failed check, failed verifier and
   unauthorized-owner negatives with no close or ACK.
6. A cap-two overlap needs one separately authorized second isolated task and
   its own exact goal/mapping; it cannot borrow P1 or add an unreviewed mapping
   to this packet. Under the sole integration owner, record both fences and
   show the blocked task does not prevent the ready task from progressing.

The service supports `POST /work/bind_existing` only after a separately
installed exact binding and takes *only* `{"goal_id", "number"}`. It is not a
way to attach P1 A/B sessions or supply session/turn IDs from request text.
If future #1043 retained sessions are used, first install their exact fresh
turn IDs and profile/source revision through the owner configuration path, run
`validate-private-config.py --require-binding`, then call that API. No binding,
launch, verification, close, or configuration mutation has occurred here.
