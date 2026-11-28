# GitHub event transport adoption: exact pilot handoff

This is an owner-operated adoption and live-acceptance preparation guide for
the opt-in GitHub event path. It is not an installation record, a substitute
for a GitHub delivery, or a claim that the path has accepted another outcome.
It uses the existing signed receiver, provider rereads, and native owner; it
does not introduce provider polling, an Access bypass, a tunnel, a secret, or
a scheduler.

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

`GitHubProvider` provides bounded authenticated reads but has no configured
provider-ingestion poller that calls `advance_github_ci`. The only production
caller of that continuation is the signed delivery worker. A repository-hooks
API result does not establish whether an installed GitHub App has its own
subscription, and a token credential cannot be used as a webhook HMAC secret.

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

1. `github_webhook.secret_env` is a nonempty reference to an already approved
   secret facility, and the receiver and its worker are running.
2. The new authorized issue alone appears in both
   `github_acceptance_profiles` and `github_event_delivery_profiles` at the
   same reviewed revision.
3. Its goal has the exact native owner above, the signed parent and verifier
   bindings, and the repository-scoped existing read credential. P1 entries
   remain unchanged.
4. The configured callback is reachable by the GitHub subscription without an
   interactive Cloudflare Access login. Do not weaken Access, add a public
   bypass, or create a tunnel to satisfy this condition.

If any condition fails, stop before launching a work session. The smallest
missing capability is external transport provisioning: an authorized,
noninteractive signed GitHub-App delivery route plus an existing approved HMAC
secret reference. It is not a code change and cannot be replaced by the
existing read token or by fabricated local deliveries.

## Owner-run live driver after preflight passes

Use a new low-risk authorized task. Record the actual issue, PR, source head,
profile revision, check ID/App/output digest, required-check IDs, signed
work/verifier run keys and turns, continuity run, wake ID, and owner epoch in
the owner-held evidence record.

1. Bind or launch only the configured work and independent verifier. Confirm
   their durable signed bindings, distinct identities, terminal successful
   turns, and an idle owner before submitting a verifier result.
2. Let GitHub deliver an actual exact-head completed CI event. Read the stored
   delivery, its linked item, and current provider facts; never POST a copied
   payload as a live substitute.
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
