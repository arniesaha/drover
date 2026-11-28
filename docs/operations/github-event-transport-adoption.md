# GitHub event transport adoption: exact pilot handoff

This is an owner-operated adoption and live-acceptance preparation guide for
the opt-in GitHub event path. It is not an installation record, a substitute
for a GitHub delivery, or a claim that the path has accepted another outcome.
It uses either the existing signed receiver or the separately installed,
bounded authenticated GitHub API poller, followed by provider rereads and the
native owner. It does not bypass Access, create a tunnel or secret, or use a
model scheduler as an acceptance driver.

## Fixed historical anchors

| Field | Value |
| --- | --- |
| Repository | `arniesaha/drover` |
| Parent / accepted historical pilot | #1024 / #1031 |
| Historical task source | PR #1029 at `5202c2359483a58b2916c58acac5f11ee2df20b8` |
| Historical profile | `228c9ccced9eac01eea57b8971d83195ab3f25f18e1870b30283f6a0128aed7c`, `independent_check`, App `15368` |
| Historical behavioral evidence | check `140725060148`, `cohort1024-independent-quota-outcome` |
| Criterion | Expired and unknown quota deny admission; the direct fresh-process `route()` observation must deny with `quota or capability unavailable`; see the exact [profile](../quality-pilots/p1-runner/profile-candidate.json). |
| Goal / owner fence | `quality-cohort-1024` / `drover:owner/quality-cohort-1024` |

The #1031 P1 outcome is accepted historical evidence. Do not reopen it,
rebind its sessions, or use its closed issue as a transport demonstration. P2
and P3 retain their separate, unaccepted original-ledger status. A future
transport exercise needs a new explicitly authorized issue, profile, source
head, behavioral check, and evidence; its owner epoch is read from the native
continuity status immediately before the owner action. It is never supplied by
a caller or copied from P1.

## What the installed path can and cannot ingest

`GitHubWebhookReceiver` accepts a GitHub-signed `pull_request`,
`check_suite`, or `workflow_run` delivery at
`POST /integrations/github/webhook`, persists it before responding, and
rejects absent or invalid HMAC signatures. `GitHubWebhookWorker` polls that
**local persisted delivery table** every 0.25 seconds. A retained delivery
whose work or verifier is not yet terminal is retried after five seconds and
re-reads GitHub provider facts. This is recovery polling of a received signed
push, not GitHub source discovery.

`GitHubProviderIngestionWorker` is a disabled-by-default, **GitHub API
POLLING** source. When `github_provider_ingestion.enabled` and the exact issue
appears in `github_provider_ingestion_profiles`, it periodically reads only
that goal's configured issue/PR/profile through the existing credential and
calls `advance_github_ci`. The service then rereads the current source and
required-check rollup before every continuation and native-owner boundary.
It does not create webhook rows, accept HMAC, or treat a token as a webhook
secret. It has no durable cursor: restart rescans the small exact allowlist so
it cannot skip retained pending work. Health reports the source as
`github_api_polling`, a `none; ... rescanned` cursor, observations, pending,
rejected, unavailable and last-error counters. Provider conditional reads and
rate-limit backoff remain in `GitHubProvider`; the poller uses visible
`interval_seconds`, `batch_size`, `retry_seconds`, and `max_backoff_seconds`.

A signed receiver remains an independently useful pushed-ingress path. Its
0.25-second local delivery-table loop and five-second continuation retry are
polling of a received delivery. The provider poller's periodic rereads are a
separate API polling transport and must be described that way in operations.
A repository-hooks API result does not establish whether an installed GitHub
App has its own subscription, and a token credential cannot be used as a
webhook HMAC secret.

## Read-only preflight

Run these before asking for any deployment change; they reveal neither token
nor secret material.

```sh
gh api user --jq '.login'
gh api repos/arniesaha/drover/hooks --jq 'map({id,name,active})'
gh api repos/arniesaha/drover/pulls/1029 --jq '.head.sha'
gh api repos/arniesaha/drover/check-runs/140725060148 \
  --jq '{id,name,status,conclusion,head_sha,app:.app.id,external_id}'
```

The expected historical reads are the source, check name, App, and profile
revision in the table above. A `401` from `GET /repos/.../installation` is not
evidence that an App subscription is absent: inspect the existing App's hook
configuration with its authorized owner interface instead. Do not write a hook
while performing this preflight.

The private runtime/config reader must then confirm, without exposing values:

1. Choose one installed transport. Signed ingress needs
   `github_webhook.secret_env` and a reachable approved callback. API polling
   needs `github_provider_ingestion.enabled: true` and no callback or HMAC.
   It still uses only the existing read credential and must not expose it.
2. The new authorized issue alone appears in
   `github_acceptance_profiles`, `github_event_delivery_profiles`, and, for
   API polling, `github_provider_ingestion_profiles`, all at the same reviewed
   revision. The schema rejects a polling entry without the existing exact
   delivery mapping and native owner.
3. Its goal has the exact native owner above, the signed parent and verifier
   bindings, and the repository-scoped existing read credential. P1 entries
   remain unchanged.
4. For signed ingress only, the configured callback is reachable by the
   GitHub subscription without an interactive Cloudflare Access login. Do not
   weaken Access, add a public bypass, or create a tunnel to satisfy this
   condition. API polling has no public callback requirement.

If the selected transport's condition fails, stop before launching a work
session. An unavailable signed route can be replaced only by the reviewed
opt-in API-polling configuration above; it cannot be replaced by fabricated
local deliveries. An unavailable credential or a provider rate-limit backoff
is a fail-closed polling health condition, not evidence of readiness.

For an isolated future task, the owner reviews an exact configuration shape
like this before the ordinary protected configuration installation. Replace
every placeholder with the new task's independently reviewed values; do not
add P1's `1031` entry or copy its closed source.

```yaml
github_provider_ingestion:
  enabled: true                 # default is false
  interval_seconds: 5           # GitHub API POLLING cadence
  batch_size: 1                 # bounded exact-source scan
  retry_seconds: 5
  max_backoff_seconds: 300
goals:
  - goal_id: <new-goal>
    repo: arniesaha/drover
    continuity_owner_id: drover:owner/<new-goal>
    github_acceptance_profiles:
      "<new-issue>": <reviewed-profile>
    github_event_delivery_profiles:
      "<new-issue>": <same-reviewed-profile>
    github_provider_ingestion_profiles:
      "<new-issue>": <same-reviewed-profile>
```

Before installation, the owner records the new issue, PR number and exact
head, immutable profile/criterion revision, behavioral check ID/App/output
digest, required-check IDs, distinct signed work/verifier run keys and turns,
and native-owner epoch. After installation, read the worker health and current
provider facts; a health observation is not an acceptance verdict. Keep this
record in the owner-held evidence location, never in issue text supplied by a
candidate.

## Owner-run live driver after preflight passes

Use a new low-risk authorized task. Record the actual issue, PR, source head,
profile revision, check ID/App/output digest, required-check IDs, signed
work/verifier run keys and turns, continuity run, wake ID, and owner epoch in
the owner-held evidence record.

1. Bind or launch only the configured work and independent verifier. Confirm
   their durable signed bindings, distinct identities, terminal successful
   turns, and an idle owner before submitting a verifier result.
2. For signed ingress, let GitHub deliver an actual exact-head completed CI
   event and read its stored delivery. For API polling, record the configured
   poller health observation and its provider reads for the exact item. Never
   POST a copied payload or persist a fixture as a live substitute.
3. Submit the verifier's bounded evidence. The coordinator must reread the
   current head, required checks, profile/criterion revision, behavioral output
   and producer App before its single close. Confirm the durable `verified`
   intent and outcome are correlated to the same facts.
4. Confirm the distinct native wake, action-intent receipt, deterministic
   effect marker, and ACK. A close is not an ACK.
5. Replay the same real delivery and repeat the exact verified request. Confirm
   one close, one effect receipt and one ACK. For the owner-approved isolated
   interruption exercise, stop only after the effect receipt and before ACK,
   restart the owned component, and confirm a fenced receipt reconciliation
   rather than a second effect.
6. For the cap-two exercise, use two isolated authorized tasks under the sole
   integration owner. Show one blocked task does not prevent unrelated ready
   progress, record both owner fences, and do not launch or relabel P1
   sessions.

Stale head, revoked mapping/profile, wrong App/check, failed or skipped
required check, missing behavioral output, nonterminal/resumed session, wrong
owner/epoch, and duplicate/out-of-order delivery must leave no close or new
receipt. Preserve those negative observations with the same correlation fields.

Live transport adoption and the driver remain unverified until these owner-run
steps complete. The source contract and fixture tests protect code boundaries;
they do not certify a GitHub subscription, an Access route, or a live outcome.
