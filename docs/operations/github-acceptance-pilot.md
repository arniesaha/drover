# GitHub acceptance pilot

This is an opt-in, exact-issue coordinator gate. It is not enabled by default,
does not certify itself, and does not certify a live GitHub outcome. Read the
[work-queue guidance](../../AGENTS.md) first for the canonical source map and
scratch-only focused-test recipe.

## Scope and state boundaries

The pilot authorizes only a GitHub issue close. It binds that decision to the
configured PR's current head, a reviewed quality-profile revision, required
checks, behavioral evidence, and that profile's configured verdict mode. It does not mean
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
# Defaults to "human_approval" when omitted.
review_mode = "human_approval"
owner_id = 81
amendment_record = "Initial independently reviewed contract"
```

## Existing-session reuse

The normal `launch` and `link` path remains unchanged: it only resolves a
queue-created `client_session_id`. A narrow recovery path is available only
when an exact installed GitHub profile has a separate reviewed binding. It is
for a retained completed work session and a separate completed verifier session
that predate their queue links; it never rewrites either session identity.

```toml
[work_queue.goals.github_existing_session_bindings."1024"]
# SHA-256 revision of the complete profile above; do not change that profile
# after its check output was published.
profile_revision = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
# Current configured PR head read during the bind and again at verdict.
source_head = "0123456789abcdef0123456789abcdef01234567"
work_session_id = "retained-work-session"
work_turn_id = "retained-work-turn"
verifier_session_id = "retained-verifier-session"
verifier_turn_id = "retained-verifier-turn"
```

The map key must name an installed profile and its `profile_revision` must
match. Each retained session must have the goal repository's stored owner/name,
`status=completed`, and a named durable input turn with a successful terminal
result. The session IDs must differ. `running`, `created`/queued, failed,
missing, wrong-repository, or ambiguous records fail closed; `awaiting: input`
alone never qualifies. The issue key/profile revision bind the pair to the exact
issue; `source_head` binds it to the configured PR source. The binding is stored
immutably on each generated link; historical links without it remain unchanged.

With the issue in its ordinary `status:review` state, a coordinator calls:

```json
POST /work/bind_existing
{"goal_id":"reviewed-goal","number":1024}
```

The request contains no session, turn, run key, source, or evidence fields.
Success returns `200` with deterministic `work` and `verify` links. A repeat
returns those same link identities and never launches a session. Missing or
uninstalled bindings return 403; stale-source, unready-label, or nonterminal
retained records return 400; an incompatible or malformed stored link returns
409 for inspection. The regular `verified` request remains required and
rechecks retained turns, profile/criteria source, provider evidence, required
checks, and close idempotency.

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

`human_approval` is the default and requires `owner_id`. An exact installed
profile may instead set `review_mode = "independent_check"` and omit `owner_id`.
That mode treats the configured App check as the independently selected verdict
source; it does not turn an agent result into a GitHub human approval. Arnab may
review and install either complete profile as the sole human decision-maker.

An amendment must be reviewed before installation and retain an explicit
`amendment_record` that identifies the prior contract and rationale. This
increment has no amendment-approval service, no automatic profile editor, and
no permission for a candidate to weaken its own acceptance condition.

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
  "evidence_key": "check-run:7"
}
```

For `human_approval`, `evidence_key` has the fixed
`check-run:<positive-id>:review:<positive-id>` shape. For the explicit
`independent_check` profile mode, it is exactly `check-run:<positive-id>`.
Callers cannot choose the mode: the installed complete profile does. For
opted-in issues, caller-supplied `behavioral_passed` and `checks_rerun` values
grant no authority; the provider facts below decide the verdict. A successful
outcome is `200 {"outcome":"verified"}`. Repeating a completed request returns
the recorded outcome and does not close a second time. If a close intent was
recorded but its provider result is uncertain, a repeat returns
`200 {"outcome":"deferred"}`; inspect provider state before a recovery rather
than treating it as a second close.

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
base branch protection, and named check IDs (plus a review ID only for the
human mode). It does not fetch arbitrary
URLs or execute shell commands from issue text, profile instructions, or check
output.

The behavioral check must have the configured name and App ID, exact current
head, completed/success status, and `external_id` equal to the profile revision.
It must still be the latest run for that name/App at that head. Its GitHub-hosted
`output.summary` must be nonempty; `output.text`, when present, must be text.
The coordinator hashes the canonical summary/text pair, rejects it above 64 KiB,
and retains only its byte count and SHA-256 in the decision audit. The required
branch-protection checks must be present exactly once with their configured App
IDs, completed/success conclusions, and the same head. Missing, duplicate,
pending, skipped, failed, wrong-App, wrong-head, missing output, and oversized
output fail closed. The pilot supports up to 100 branch-protection check-run
contexts; status-only and ruleset-only requirements are outside this increment.

For `human_approval`, the configured human owner must submit an `APPROVED`
review for that same head with the exact whitespace-delimited token
`drover-acceptance:<profile-revision>`. For `independent_check`, no review is
fetched or synthesized: the configured App ID, exact head, profile revision,
bounded output, and required checks are the provider verdict. This does not
waive repository branch-protection rules or turn the App's result into a human
review.
Source identity is reread during evidence resolution and all provider facts are
resolved again immediately before the durable close intent. The retained
`provider_operation_id` is bounded JSON of the resolved source, required-check
facts, evidence reference, and behavioral-result byte count/digest. GitHub has
no atomic compare-head-and-close API; movement after the final read remains an
owner reconciliation gate.

## Inspecting the retained behavioral result

The evidence key identifies the sole result location: authenticated GitHub
`GET /repos/<configured-repo>/check-runs/<check-id>`. Before submitting a
verdict, the owner retrieves that fixed route, inspects
`output.summary` and `output.text`, and confirms that they record the reviewed
journey and its negative control. The owner then matches the returned run ID,
head SHA, App ID, and `external_id` to the configured profile revision. In
`human_approval` mode, the owner also matches the review ID to the same head and
revision token. After a close intent, the recorded result digest and byte count
bind the inspected output to that decision without copying potentially private
output into the control-store audit.

An output summary can prove that GitHub retained a bounded result, but it cannot
interpret whether the observations establish the claim. A producer can still
write a vague or dishonest summary. The independent owner must judge the
contents and the producer's isolation; this increment deliberately does not
turn check text into a general evidence DSL or automate producer trust.

## What is enforced, what remains manual, and retry handling

| Boundary | Enforced here | Manual owner/producer responsibility |
| --- | --- | --- |
| Profile and criteria | exact issue profile, marked criteria match, revision pin, selected review mode, fresh verification | Arnab reviews/installs profiles and amendments; keep candidate access from weakening it |
| Behavioral result | provider-resolved check identity/status/head/revision, latest-run check, nonempty bounded GitHub output, and output digest | isolate the App/producer, execute the reviewed journey and negative control, retrieve and judge the retained observations |
| Verdict mode | human mode: configured human ID, approval state, exact head, revision token; independent mode: configured App check only | decide whether observations establish the substantive claim; never represent an agent result as a human review |
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
| Behavioral evidence and required CI fail closed | [`test_mismatched_behavioral_evidence_cannot_close`](../../tests/test_work_queue_github_acceptance.py), [`test_missing_or_unbounded_behavioral_result_cannot_close`](../../tests/test_work_queue_github_acceptance.py), [`test_required_ci_negative_controls`](../../tests/test_work_queue_github_acceptance.py), [`test_older_success_cannot_replace_latest_behavioral_failure`](../../tests/test_work_queue_github_acceptance.py) |
| A changed retained result cannot replace the reviewed observation | [`test_changed_behavioral_result_at_final_boundary_cannot_close`](../../tests/test_work_queue_github_acceptance.py) |
| A separate owner review is revision/head bound | [`test_owner_review_is_independent_and_revision_bound`](../../tests/test_work_queue_github_acceptance.py) |
| Explicit independent-check mode removes no other provider binding | [`test_explicit_independent_check_profile_does_not_require_human_review`](../../tests/test_work_queue_github_acceptance.py), [`test_independent_check_mode_still_requires_configured_producer`](../../tests/test_work_queue_github_acceptance.py), [`test_human_review_mode_rejects_check_only_evidence`](../../tests/test_work_queue_github_acceptance.py) |
| Explicit existing-session reuse is terminal, scoped, separate, and idempotent | [`test_existing_completed_sessions_bind_once_without_relaunch`](../../tests/test_work_queue_github_acceptance.py), [`test_existing_session_binding_requires_terminal_persisted_sessions`](../../tests/test_work_queue_github_acceptance.py), [`test_existing_session_reader_requires_persisted_terminal_turn`](../../tests/test_work_queue_github_acceptance.py) |
| Repeats and a lost close response do not make a second effect | [`test_provider_evidence_and_owner_review_close_once_despite_routine_notes`](../../tests/test_work_queue_github_acceptance.py), [`test_uncertain_close_never_causes_second_effect`](../../tests/test_work_queue_github_acceptance.py) |
| Existing work queue/local-file behavior stays covered | [`tests/test_work_queue_execution.py`](../../tests/test_work_queue_execution.py), [`tests/test_work_queue_local_candidates.py`](../../tests/test_work_queue_local_candidates.py) |

Fixtures prove this contract only. Before acceptance of the pilot, the owner
must independently review and install the profile and producer trust boundary,
then use one subsequent authorized real PR through the installed gate. Inspect
the actual retained journey/negative-control observations, provider facts, and
single-close behavior. Live GitHub/installed-pilot acceptance is **not verified
by these fixtures or this documentation**.
