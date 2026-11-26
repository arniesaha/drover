# GitHub acceptance pilot: Slice A first increment

This is an opt-in coordinator gate, not an enabled deployment or live pilot
certification. No profiles ship enabled. Existing GitHub and `local_file`
records retain their historical proof level. The native work → verify → ack
chain remains local-file-only. Execution finished, checks passed, merged,
deployed, and accepted remain distinct; this gate only authorizes issue close.

## Reviewed quality contract

The independent owner installs `github_acceptance_profiles` in the existing
goal's server configuration, keyed by the exact issue number. Each entry has:

```json
{
  "pull_number": 123,
  "criteria": "The selected journey succeeds and its negative control fails.",
  "instructions": "Run the owner-selected journey and negative control in isolation; retain their actual results in the check output.",
  "behavioral_check": "owner-journey",
  "app_id": 42,
  "owner_id": 81,
  "amendment_record": "Initial contract; independent review reference"
}
```

Numbers above are placeholders. The affected surface, concrete journey,
negative control, expected observations, and retained result must be specified
in the real profile's criteria/instructions during owner review. The goal needs
`issues_read`, `pulls_read`, `checks_read`, and its existing issue write access.
The issue includes exactly one section:

```html
<!-- drover:acceptance -->
The selected journey succeeds and its negative control fails.
<!-- /drover:acceptance -->
```

Its trimmed contents must equal the approved criteria. Notes/markers outside
this section do not invalidate verification. Any profile field change changes
the revision and requires a new verification. Amendments require independent
review and an explicit amendment record referencing the retained prior contract
and rationale; this increment does not create an amendment approval service.

The profile's revision is SHA-256 of `json.dumps(profile.model_dump(),
sort_keys=True).encode()`, exposed by `GitHubAcceptanceProfile.revision`.
Compute it using the installed model from the independently reviewed profile.

## Enforced boundary and trust roots

Verification identity includes the provider-resolved PR head, base branch,
repository, PR number, and profile revision. Verdict evidence must be
`check-run:<id>:review:<id>`. The provider retrieves those IDs only from fixed
GitHub API routes in the configured repository. It never fetches report URLs
or executes issue/profile instructions as shell commands.

The behavioral check must have the configured App ID/name, exact head,
`external_id` equal to the profile revision, completed status, and success
conclusion. An APPROVED review from the configured human owner's numeric ID
must reference the exact commit and contain the whitespace-delimited token
`drover-acceptance:<revision>`. Caller success booleans grant no pilot authority.
Both work and separate verification sessions must be completed.
The cited behavioral run must still be the latest run from that App for its
check name. The owner must dismiss obsolete approval reviews when rescinding
their verdict; this increment does not reconcile later review threads.

The existing required-CI resolver also returns required check-run IDs,
conclusions, and App IDs. Missing, skipped, pending, failed, ambiguous, or wrong
head/App runs fail closed. The pilot supports branch-protection check runs,
up to 100 required contexts; status-only checks and ruleset-only protection
are not supported. Existing bounded pagination, response caps, conditional
revalidation, and backoff apply. Source identity is reread after evidence
resolution; two complete fact resolutions must agree before durable intent.
GitHub provides no atomic compare-head-and-close operation: movement after
the final read is a remaining external race, requiring owner reconciliation.

For pilot events, `provider_operation_id` contains bounded JSON provider facts
(source/profile, required run IDs/conclusions/App IDs, and evidence reference),
instead of the legacy opaque evidence label. No schema migration is needed.
Repeated successful verdicts return the existing outcome; uncertain close
intents return deferred and require inspection, never an automatic second close.

Server configuration and the trusted App/behavioral producer are trust roots.
Candidates must have no ability to apply their own profile changes, obtain
the producer's credentials, or alter its executed journey/instructions without
independent review. The owner must verify those protections and inspect the
retained behavioral observations. A candidate-controlled workflow using a
shared App identity is insufficient. This code cannot establish that trust or
infer product quality from a success conclusion; it checks provenance and
binding, and requires an independent owner behavioral verdict.

## Independent bootstrap and installed verification (NOT VERIFIED here)

1. Parent independently reviews this commit, runs focused CI, and installs it
   through the normal reviewed process. This implementation cannot certify
   itself through its own gate.
2. For one subsequent authorized real issue/PR, independently review/install
   the concrete profile and criteria section. Retain profile review/amendment
   history. Verify producer isolation/credentials and required branch checks.
3. Finish the worker and launch separate verification through the existing
   coordinator. Confirm the configured PR's current head through GitHub.
4. The trusted producer actually executes the reviewed journey and negative
   control on that head. It publishes the retained observations in check-run
   output, with the pinned name, App, head, and revision as `external_id`.
   Owner independently inspects those results and submits the revision token
   in an approving review on that commit. A fabricated check is not a pilot.
5. After verification completes and required CI succeeds, the coordinator calls
   `update(action="verified", evidence_key="check-run:<id>:review:<id>")`
   for the verification run. Inspect the stored provider facts and one close.
   Repeat the same call and confirm no second effect.
6. On separately authorized test outcomes, exercise missing evidence, failed/
   skipped checks, a moved head, and changed criteria/profile before accepting;
   each must leave the issue open. Inspect uncertain operations before retrying.

Remaining Slice A rollout gates: independently reviewed/isolated real behavioral
producer and concrete surface profile, CI/install, then one subsequent authorized
real outcome through this installed gate. HTTP fixtures and disposable Postgres
tests establish contracts only; live GitHub/installed acceptance is NOT VERIFIED.

## Foreground test evidence

Initial gap reproduction (before implementation): **1 failed**, because the
forged-flags/evidence-label verdict closed the fixture issue without raising:

```sh
DROVER_TEST_POSTGRES_DSN='host=/Users/arnab/scratch/issue1024-a port=55428 dbname=postgres' TMPDIR=/Users/arnab/scratch/issue1024-a uv run --extra dev --extra postgres pytest -q tests/test_work_queue_github_acceptance.py --basetemp=/Users/arnab/scratch/issue1024-a/red
```

Final changed-module and compatibility run: **141 passed in 12.28s**. The DSN
was a disposable local socket-only cluster created for these tests, not a
production service. Its data and the test environment were preserved in scratch.

```sh
PYTHONDONTWRITEBYTECODE=1 DROVER_TEST_POSTGRES_DSN='host=/Users/arnab/scratch/issue1024-a port=55428 dbname=postgres' TMPDIR=/Users/arnab/scratch/issue1024-a /Users/arnab/scratch/issue1024-a/venv/bin/python -m pytest -p no:cacheprovider -q tests/test_work_queue_github_acceptance.py tests/test_work_queue_execution.py tests/test_work_queue_github.py tests/test_work_queue_policy.py tests/test_work_queue_local_candidates.py tests/test_github_work_item_events.py --basetemp=/Users/arnab/scratch/issue1024-a/final
```

`black --check` on the three changed Python modules and new test, and
`git diff --check`, passed. Intermediate development: 27 passed; then 137 passed/
2 failed on the existing decision-options constraint (fixed without migration);
then 140 passed and 141 passed before the final run above.
