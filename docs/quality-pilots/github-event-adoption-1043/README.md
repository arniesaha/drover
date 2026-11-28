# #1043 GitHub event-path adoption packet

This packet prepares a distinct, source-pinned adoption task for PR #1042 at
`bde283aa6bd4f97073867930153c8d12e9b8b85e`, the actual GitHub PR head.
The installed merged runtime is the distinct merge commit
`f68653d9be5adbe8eed1ba53a832d5ebb55d7dbc`; equal tree identity does not make
it an acceptance head. This packet is not an installation record, a GitHub
delivery, a native-owner acknowledgement, or an accepted outcome. P1 remains
accepted historical evidence for #1031 only; its profile,
producer check, bindings, and closed issue are not reusable proof for #1043.
The original P2/P3 ledger remains separately not accepted.

## Reviewed contract

The proposed profile is [profile-candidate.json](profile-candidate.json):
`independent_check`, PR #1042, App `15368`, behavioral check
`github-event-adoption-1043-outcome`, and revision
`df895b4aafeb94a8d356cbcd9dfc277642e67989dc5ce9202a07f96544c95cfe`.
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

The App `15368` check identity does **not** establish workflow identity. A
repository writer with `checks: write` through that shared App could create a
matching check; protected-main producer placement is a reviewed convention,
not an enforcement mechanism in GitHub or this profile. Publisher reruns are
also deliberately fail-closed: they compare the complete retained output,
including interpreter and log hashes, and inspect only the first 100 matching
head check runs. A changed rerun, an older matching check outside that page, or
an ambiguous match requires owner inspection rather than a new publication.
The hosted proof's `postgres:17` service tag is floating rather than
digest-pinned; this packet makes no measurement or reproducibility claim about
future image contents.

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
  docs/quality-pilots/github-event-adoption-1043/validate-private-config.py \
  docs/quality-pilots/github-event-adoption-1043/config_contract.py
git diff --check
```

The hosted workflow separately verifies the candidate at the pinned PR head.
The distinct merged source is runtime provenance only and cannot satisfy an
acceptance-source, behavioral-check, or required-check head comparison.
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
execution_host=${DROVER_OWNER_EXECUTION_HOST:?set the existing native harness host id}
execution_dir=${DROVER_OWNER_EXECUTION_DIR:?set an existing isolated execution directory}
python_bin=${DROVER_TEST_PYTHON:-$(command -v python3)}
uv_bin=${DROVER_TEST_UV:-$(command -v uv)}
export UV_PROJECT_ENVIRONMENT="$scratch_root/venv" UV_CACHE_DIR="$scratch_root/uv-cache"
"$uv_bin" sync --locked --extra dev --extra postgres --python "$python_bin"
"$UV_PROJECT_ENVIRONMENT/bin/python" -I \
  "$repo_root/docs/quality-pilots/github-event-adoption-1043/prepare-private-config.py" \
  --baseline-config "$config_before" \
  --native-template-goal native-owner-canary-848 \
  --execution-host "$execution_host" --execution-cwd "$execution_dir" \
  --profile "$repo_root/docs/quality-pilots/github-event-adoption-1043/profile-candidate.json" \
  --output "$scratch_root/work-queue-projection.json"
"$UV_PROJECT_ENVIRONMENT/bin/python" -I \
  "$repo_root/docs/quality-pilots/github-event-adoption-1043/validate-private-config.py" \
  --baseline-config "$config_before" \
  --native-template-goal native-owner-canary-848 \
  --execution-host "$execution_host" --execution-cwd "$execution_dir" \
  --projection "$scratch_root/work-queue-projection.json"
```

`$execution_dir` must already exist, must be supplied explicitly, and must not
equal, contain, or be contained by any CWD from the native canary template.
The script creates neither that directory nor any live configuration. The
projection derives the supported
native harness/model and brief from the named native-template goal; it copies
the existing
`DROVER_WORK_QUEUE_GITHUB_TOKEN` *reference* and never prints a credential
value. It preserves and revalidates the template's policy path, execution
enablement, budget, estimated cost, worker limit, brief, harness and model. It
preserves `execution.global_max_workers == 2`, uses
`drover:owner/github-event-adoption-1043`, and a goal-local integration fence.
Its only #1043 mappings are `github_acceptance_profiles`,
`github_event_delivery_profiles`, and `github_provider_ingestion_profiles`, all
at the revision above. It turns polling on with batch two and cadence/retry 60
seconds. It validates that the entire P1 goal's canonical digest did not change.
It also removes only the newly prepared #1043 goal and the ingestion block from
the candidate before comparing the entire remaining work queue to the baseline;
principal roles, every unrelated goal, capability, hook, and authority change
therefore fail validation. The candidate ingestion block must be exactly
enabled with batch two, 60-second interval/retry, 300-second maximum backoff,
and 300-second settled delay.

There is no WorkQueue API that writes server configuration. The owner must
apply the validated projection with the normal private configuration installer,
then run the same validator with `--candidate-config` against the staged final
TOML before activation, supplying the same template, host, and directory
arguments. The final installer must change only `work_queue`; the validator
compares every other normalized configuration value to the baseline. It also
compares the complete #1043 `GoalScope` model, including defaults and the
credential capability list. With `--require-binding`, the sole permitted
target difference is one validated #1043 session-binding map; all other goal
fields remain exact. The script rejects a copied or modified P1 goal and the
same whole-baseline and exact-ingestion checks apply to both projection and
TOML.

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

1. Read, before launch, the merged PR #1042 record (actual PR head
   `bde283…`, distinct merged runtime `f68653…`), the installed profile
   revision and issue acceptance marker, and the protected-main required-check
   rules and exact current check IDs/conclusions. Read the issue labels and
   stop unless the exact `goal:github-event-adoption-1043` and `status:review`
   labels are present. Then read the published behavioral check ID/App/external
   ID/output digest and poller health. Any unavailable, stale, wrong-source,
   failed, skipped or missing fact stops the task. A poller observation is not
   a verdict.
2. With #1043 at `status:review`, create the distinct work using the supported
   coordinator request: `POST /work/launch` with
   `{"goal_id":"github-event-adoption-1043","number":1043,"request_key":"owner-recorded-unique-key","estimated_cost":1}`.
   Preserve the returned work run key and its signed turn/session provenance.
   While that bound work agent's structured turn is still running, the agent
   itself must use its stdio MCP tool
   `drover_work_update(action="complete")`. It carries the signed run key and
   returns a deferred intent; it does not launch the verifier itself. The owner
   observes the result and stops if a coordinator sends the completion update
   or if the work turn has already ended. `advance_deferred_completion` later
   requires the terminal work turn and current exact-head required CI before
   scheduling the distinct verifier.
3. Let installed API polling observe the exact configured item; do not POST a
   copied webhook payload and do not use a cron tick as a stage driver. Record
   the poller health counters, source/profile/head, required-check rollup, and
   resulting verifier run key. Confirm the verifier is distinct and terminal.
4. While the distinct bound verifier's structured turn is still running, that
   verifier itself must use its stdio MCP tool
   `drover_work_update(action="verified", evidence_key="check-run:<published-id>")`.
   The bound MCP proxy also carries its fixed legacy compatibility booleans;
   this GitHub pilot does not use them as acceptance proof. The request returns
   a deferred intent. The owner observes only and stops if a coordinator
   submits verification or the verifier turn has already ended.
   `advance_deferred_verification` subsequently requires both terminal turns,
   current source and evidence before it can retain the verified outcome, one
   close, native wake, owner epoch, action receipt/fingerprint and ACK
   correlations.
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
