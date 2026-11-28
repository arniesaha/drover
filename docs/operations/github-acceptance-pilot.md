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
for retained work and verifier sessions that predate their queue links; it
never rewrites either session identity.

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
a named durable input turn, and either a completed lifetime with its correlated
successful terminal exit or a supported structured session that remains
`running` between turns. The resumable form is deliberately narrower: the
reader accepts only a Codex turn's exact `exited: 0`, or Claude Code's complete
driver result (`is_error: false`, `subtype: success`,
`terminal_reason: completed`, and zero queued turns). Its named input must be
the latest durable input, the terminal result must be final in that input
window, and the trusted owning daemon must report the same structured session
as `awaiting: input` with `turn_active: false`. The session IDs must differ.
`running` records without those facts, `created`/queued, busy, unavailable,
failed, missing, wrong-repository, or ambiguous records fail closed;
`awaiting: input` or `turn_complete` alone never qualifies. The issue
key/profile revision bind the pair to the exact issue; `source_head` binds it
to the configured PR source. The binding is stored as one append-only,
link-keyed `bind_existing_session` policy event for each generated link.
Historical links without that event remain unchanged; the existing signed
local-file continuity envelope is not reused or overwritten.

At `verified`, this exception applies only when the stored link event is
byte-for-byte the pair selected by the *current* installed profile/binding and
the strict reader still returns that exact session/turn. This is the narrow
place where a proven `running` structured lifetime is treated as settled. A
normal queue link still requires the ordinary lifetime `completed` state; an
uninstalled profile or older retained pair cannot use this exception or reach
a verdict after the configuration has changed.

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

If a later reviewed durable input supersedes a never-accepted pair, the owner
updates the installed exact binding and repeats this same request. The service
creates a new deterministic pair and retains the old links and their policy
events as history; it never rewrites a session, link, or audit record. This is
allowed only when every old link has its single `bind_existing_session` event.
Any progress, verification intent/outcome, extra event, malformed binding, or
uncertain close is an inspection-required 409: reconcile the prior effect
before considering another pair. The old pair is no longer eligible once the
current binding changes.

`execution_links` retains its normal one-session association invariant. When a
replacement uses a newer successful turn from the same retained session, the
old binding-only link remains that session's sole `session_id` owner. The new
link deliberately leaves that column null and reads the exact session ID only
from its immutable `bind_existing_session` event. This is not the ordinary
sessionless-launch recovery path: it is accepted only when the older owner is
an existing-session link for the same issue, role, profile revision, source
head, and session ID. A bind-only partial newer work link is therefore safe to
retry with the same request; the retry completes its verifier link without
rewriting the old link or event. Any other session owner remains
inspection-required.

The checked issue must contain exactly one marked section whose trimmed contents
equal `criteria`:

## Pushed CI continuation (exact opt-in)

`github_event_delivery_profiles` is a separate exact-issue mapping from issue
number to the already-installed acceptance-profile revision. It defaults to
empty and does not change a profile revision or upgrade historical GitHub or
`local_file` records. It requires the goal's existing native continuity owner.
A work or verifier launch carries that revision in its
hub-signed coordinator binding. The runtime accepts a deferred report only
when both the installed mapping and the durable signed binding agree.

With the existing `github_webhook.secret_env` configured, the webhook worker
persists signed deliveries before processing. An exact-head successful
`check_suite` or `workflow_run` rollup then calls the narrowly scoped adapter.
The adapter re-reads the issue, PR head, criteria/profile revision, and exact
required checks through `GitHubProvider`; webhook fields never become
acceptance facts. A stale head, missing/pending/skipped/failed check, absent
binding, malformed evidence, or unavailable provider read does not continue a
work item. A constituent `check_run` never creates a rollup by itself.

The continuation only schedules a separate verifier after its bound work turn
is terminal. A verifier report remains a durable intent until its exact turn is
terminal and fresh `acceptance_facts` reads agree. If an exact green CI delivery
arrives first, the existing `github_webhook_deliveries` row remains leased with
`continuation_pending` and the webhook worker retries that same signed payload
on its bounded five-second delay. Each retained-delivery retry re-reads
provider source, required checks, and evidence; it is bounded recovery polling
of an already received signed push, not independent provider ingestion or a
new scheduler. Restart reclaims its expired lease. Stale, failed, unsupported,
and already-acknowledged paths are completed without an effect; provider
unavailability leaves the lease recoverable.

The provider close has a narrower recovery fence: after a verified intent, a
restart may record the outcome only when the current installed profile, signed
parent and verifier terminal bindings, current source/head/checks/evidence, and
closed issue all agree. It does not issue a second close. This coordinator close
intent/outcome recovery is not a native-owner effect/ACK receipt. Native-owner
action-intent, effect, and ACK remain the authority for native effects and have
not been extended to GitHub issue closure.

This is preparation and code-contract evidence only. A live owner still needs
the normal protected integration, private installation, signed provider
delivery, and retained provider evidence. If no webhook secret is already
configured, this path is unavailable; it does not create a secret, webhook, or
fallback poller.

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
| Signed exact-head CI survives terminal/report ordering, restart, replay, and constituent/stale controls | [`test_signed_exact_head_ci_continues_opted_github_deferred_pair`](../../tests/test_work_queue_github_acceptance.py), [`tests/test_github_work_item_events.py`](../../tests/test_github_work_item_events.py) |
| A post-close/pre-outcome crash records an observed close without a second PATCH | [`test_observed_close_after_outcome_write_failure_is_acked_without_reclose`](../../tests/test_work_queue_github_acceptance.py) |
| Explicit existing-session reuse is terminal, scoped, separate, and idempotent | [`test_existing_completed_sessions_bind_once_without_relaunch`](../../tests/test_work_queue_github_acceptance.py), [`test_existing_session_reader_requires_terminal_persisted_sessions`](../../tests/test_work_queue_github_acceptance.py), [`test_existing_session_reader_requires_exact_persisted_terminal_turn`](../../tests/test_work_queue_github_acceptance.py), [`test_existing_session_reader_accepts_only_an_idle_structured_turn`](../../tests/test_work_queue_github_acceptance.py), [`test_existing_session_reader_rejects_unsafe_resumable_turns`](../../tests/test_work_queue_github_acceptance.py) |
| A current strict resumable pair may verify, while unsafe, superseded, or uncertain pairs cannot | [`test_verified_accepts_current_strict_resumable_existing_pair`](../../tests/test_work_queue_github_acceptance.py), [`test_verified_rejects_unsafe_resumable_verifier_at_service_boundary`](../../tests/test_work_queue_github_acceptance.py), [`test_running_unbound_verifier_cannot_use_existing_session_exception`](../../tests/test_work_queue_github_acceptance.py), [`test_existing_pair_replacement_keeps_old_links_and_rejects_them_at_verdict`](../../tests/test_work_queue_github_acceptance.py), [`test_existing_pair_with_uncertain_close_cannot_be_replaced`](../../tests/test_work_queue_github_acceptance.py) |
| Later turns may reuse a retained session without relaxing its unique owner or losing a partial bind | [`test_existing_turn_rebind_reuses_session_without_unique_association_conflict`](../../tests/test_work_queue_github_acceptance.py), [`test_existing_turn_rebind_recovers_bind_only_partial_work_link`](../../tests/test_work_queue_github_acceptance.py) |
| Repeats and a lost close response do not make a second effect | [`test_provider_evidence_and_owner_review_close_once_despite_routine_notes`](../../tests/test_work_queue_github_acceptance.py), [`test_uncertain_close_never_causes_second_effect`](../../tests/test_work_queue_github_acceptance.py) |
| Existing work queue/local-file behavior stays covered | [`tests/test_work_queue_execution.py`](../../tests/test_work_queue_execution.py), [`tests/test_work_queue_local_candidates.py`](../../tests/test_work_queue_local_candidates.py) |

Fixtures prove this contract only. Before acceptance of the pilot, the owner
must independently review and install the profile and producer trust boundary,
then use one subsequent authorized real PR through the installed gate. Inspect
the actual retained journey/negative-control observations, provider facts, and
single-close behavior. Live GitHub/installed-pilot acceptance is **not verified
by these fixtures or this documentation**.
