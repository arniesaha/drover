# GitHub acceptance pilot

This is an opt-in, exact-issue coordinator gate. It is not enabled by default,
does not certify itself, and does not certify a live GitHub outcome. Read the
[work-queue guidance](../../AGENTS.md) first for the canonical source map and
scratch-only focused-test recipe.

## Scope and state boundaries

The pilot authorizes only a GitHub issue close. It binds that decision to the
configured PR's current head, a reviewed quality-profile revision, required
checks, behavioral evidence, and an independent owner review. It does not mean
that a worker has finished, a PR has merged, a release has deployed, or a
continuity owner has acknowledged work. Those are separate states.

The `local_file` canary's signed deferred work -> verify -> acknowledge chain
remains local-file-only. This pilot neither invokes that chain for GitHub work
nor upgrades historical GitHub or local-file records with stronger proof. See
the [stage boundary](verified-event-path-boundaries.md#2-real-github-coordinator-ingress-vs-local-file-proof).

## Reviewed quality profile

An independent owner installs the profile in the existing goal's reviewed
server configuration. It is keyed by the exact issue number; candidate text,
worker input, and a report URL cannot select a profile, source SHA, or command.

```toml
[work_queue]
enabled = true
shadow_mode = false
workspace_id = "reviewed-workspace"

[[work_queue.goals]]
goal_id = "reviewed-goal"
repo = "owner/repository"
goal_label = "goal:reviewed"

[work_queue.goals.credential]
mode = "app" # A token credential is also supported.
token_env = "DROVER_GITHUB_TOKEN"
installation_id = 1
repository_id = 1
capabilities = ["issues_read", "issues_write", "pulls_read", "checks_read"]

[work_queue.goals.github_acceptance_profiles."1024"]
pull_number = 123
criteria = "The selected journey succeeds and its negative control fails."
instructions = "Run the reviewed journey and negative control; retain actual observations in the check output."
behavioral_check = "owner-journey"
app_id = 42
owner_id = 81
amendment_record = "Initial independently reviewed contract"
```

The checked issue must contain exactly one marked section whose trimmed contents
equal `criteria`:

```html
<!-- drover:acceptance -->
The selected journey succeeds and its negative control fails.
<!-- /drover:acceptance -->
```

Notes and routine markers outside this section do not invalidate verification.
The profile revision is SHA-256 over sorted JSON of the complete profile model;
changing any profile field or the marked criteria changes the binding and
requires a new separate verification. The profile's `instructions` tell the
independent producer what to run; the provider never executes them.

An amendment must be independently reviewed before installation and retain an
explicit `amendment_record` that identifies the prior contract and rationale.
This increment has no amendment-approval service, no automatic profile editor,
and no permission for a candidate to weaken its own acceptance condition.

## Request, response, and failure contract

The existing authenticated control API accepts the terminal request only after
the separate verification session is complete:

```json
POST /work/update
{
  "goal_id": "reviewed-goal",
  "number": 1024,
  "run_key": "verification-run-key",
  "action": "verified",
  "evidence_key": "check-run:7:review:8"
}
```

`evidence_key` has the fixed `check-run:<positive-id>:review:<positive-id>`
shape. For opted-in issues, caller-supplied `behavioral_passed` and
`checks_rerun` values grant no authority; the provider facts below decide the
verdict. A successful outcome is `200 {"outcome":"verified"}`. Repeating a
completed request returns the recorded outcome and does not close a second
time. If a close intent was recorded but its provider result is uncertain, a
repeat returns `200 {"outcome":"deferred"}`; inspect provider state before a
human-authorized recovery rather than treating it as a second close.

The HTTP boundary returns sanitized failures: malformed or stale requests are
`400 {"error":"invalid work request"}`; caller/role/shadow-mode failures are
403; unavailable GitHub credentials, reads, rate limits, or bounded provider
responses are `409 {"error":"work operation denied","reason":"item provider unavailable"}`.
Other unexpected post-intent failures are a 409 pause-for-inspection response.
Provider bodies and private evidence remain in server logs or the trusted
provider, not in the API response or decision audit.

## Enforced provider facts

At acceptance, the trusted GitHub adapter reads only fixed API routes in the
configured repository. It resolves the issue, configured PR, current head,
base branch protection, and named check/review IDs. It does not fetch arbitrary
URLs or execute shell commands from issue text, profile instructions, or check
output.

The behavioral check must have the configured name and App ID, exact current
head, completed/success status, and `external_id` equal to the profile revision.
It must still be the latest run for that name/App at that head. The required
branch-protection checks must be present exactly once with their configured App
IDs, completed/success conclusions, and the same head. Missing, duplicate,
pending, skipped, failed, wrong-App, and wrong-head runs fail closed. The pilot
supports up to 100 branch-protection check-run contexts; status-only and
ruleset-only requirements are outside this increment.

The configured human owner must submit an `APPROVED` review for that same head
with the exact whitespace-delimited token `drover-acceptance:<profile-revision>`.
Source identity is reread during evidence resolution and all provider facts are
resolved again immediately before the durable close intent. The retained
`provider_operation_id` is bounded JSON of the resolved source, required-check
facts, and evidence reference. GitHub has no atomic compare-head-and-close API;
movement after the final read remains an owner reconciliation gate.

## What is enforced, what remains manual, and retry handling

| Boundary | Enforced here | Manual owner/producer responsibility |
| --- | --- | --- |
| Profile and criteria | exact issue profile, marked criteria match, revision pin, fresh verification | independently review/install profile and amendments; keep candidate access from weakening it |
| Behavioral result | provider-resolved check identity/status/head/revision and latest-run check | isolate the App/producer, execute the reviewed journey and negative control, retain/inspect actual observations |
| Owner verdict | configured human ID, approval state, exact head, revision token | decide whether observations establish the substantive claim; dismiss/revise obsolete reviews |
| Required CI and close | required check IDs/App IDs/current conclusions, final reread, one intent/outcome path | reconcile a GitHub head change after the final read and inspect uncertain provider operations |

`failed` returns the item to revision until the existing policy attempt budget is
exhausted, then blocks it for the existing systemic-review path. A new profile
revision or source movement requires a fresh verification request; the pilot
does not retry, relaunch, merge, deploy, or re-run the producer automatically.

## Protecting proof map

| Claim | Meaningful protection |
| --- | --- |
| Caller flags or an evidence label alone cannot close | [`test_forged_flags_and_evidence_label_cannot_close_pilot`](../../tests/test_work_queue_github_acceptance.py) |
| Current head, criteria/profile revision, terminal sessions, and final reread are required | [`test_stale_verdict_and_unfinished_verifier`](../../tests/test_work_queue_github_acceptance.py), [`test_final_boundary_rechecks_head_movement`](../../tests/test_work_queue_github_acceptance.py) |
| Behavioral evidence and required CI fail closed | [`test_mismatched_behavioral_evidence_cannot_close`](../../tests/test_work_queue_github_acceptance.py), [`test_required_ci_negative_controls`](../../tests/test_work_queue_github_acceptance.py), [`test_older_success_cannot_replace_latest_behavioral_failure`](../../tests/test_work_queue_github_acceptance.py) |
| A separate owner review is revision/head bound | [`test_owner_review_is_independent_and_revision_bound`](../../tests/test_work_queue_github_acceptance.py) |
| Repeats and a lost close response do not make a second effect | [`test_provider_evidence_and_owner_review_close_once_despite_routine_notes`](../../tests/test_work_queue_github_acceptance.py), [`test_uncertain_close_never_causes_second_effect`](../../tests/test_work_queue_github_acceptance.py) |
| Existing work queue/local-file behavior stays covered | [`tests/test_work_queue_execution.py`](../../tests/test_work_queue_execution.py), [`tests/test_work_queue_local_candidates.py`](../../tests/test_work_queue_local_candidates.py) |

Fixtures prove this contract only. Before acceptance of the pilot, the owner
must independently review and install the profile and producer trust boundary,
then use one subsequent authorized real PR through the installed gate. Inspect
the actual retained journey/negative-control observations, provider facts, and
single-close behavior. Live GitHub/installed-pilot acceptance is **not verified
by these fixtures or this documentation**.
