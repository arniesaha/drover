# Prepared independent P1 runner - not installed or approved

Parent #1024; bounded pilot issue #1031; PR #1029 immutable head `5202c2359483a58b2916c58acac5f11ee2df20b8`.

## Concrete preparation completed

- The retained revision `a2faaad79483a3d310af06f13dcb97ffeadca3b0ca82fa56742e3f6777558f06` and check `140609025810` remain incomplete historical evidence: their three-command proof did not call `route()` with unknown quota. The amended complete-model revision is `228c9ccced9eac01eea57b8971d83195ab3f25f18e1870b30283f6a0128aed7c` and requires four successful commands plus the exact direct denial observation. It preserves the immutable candidate, criteria, App, and `independent_check` mode. Arnab's existing #1024 authority may install the reviewed amended profile after normal exact-head CI; this mode neither creates nor requires a second GitHub human approval.
- The retained three-command proof covers 36 tests, the strict boundary, and the held-out forbidden import, but it does **not** directly exercise unknown-quota admission. `run-proof.py` now requires a fourth fresh-process command that passes an unknown quota window to `route()` and records its explicit denial. `publish-check.py` rejects any receipt without that observation, including the retained three-command receipt. Python 3.14.7 validation is not a claim that the prepared hosted Python 3.11 workflow ran. Dependency lock SHA256: `6c13fe4a81a30bc31a2216f6cef1defef00ad53a46c159f9dab139747f607b22`.
- The prepared `.github/workflows/cohort1024-independent-quota.yml` and inert example use the existing GitHub Actions App15368. Producer code comes from protected main at the dispatched revision; candidate source is fixed, not an input. Credential-free proof and short-lived check publication are separate fresh hosted jobs. No self-hosted runner, new App, stored secret, or provider setup is proposed. This is for this reviewed immutable candidate, not a sandbox for arbitrary hostile source.
- `publish-check.py` checks source, command shape, direct unknown-quota denial, counts, complete profile revision and exact PR head, and reconciles only an identical existing publication. It will only publish an `independent_check` profile and makes no review request. No check, review, config or permission was written during preparation.

## Exact decisions remaining - after this reviewable preparation

1. Arnab reviews the exact `independent_check` profile and the retrieved check output. The mode deliberately does not create or require a GitHub human approval; an agent output is never recorded as one.
2. Arnab reviews and, if appropriate, merges the protected-main producer. Its fresh publication job alone has `checks: write`; proof remains credential-free. No attempt was made to mint an App token or alter repository-wide permissions.

After those decisions, use the amended complete profile keyed to issue1031 and
its new revision identity. The retained publication keeps its old identity and
three-command receipt. `publish-check.py` deliberately refuses to overwrite a
different prior publication, so the amended profile is the explicit corrected
identity for a later reviewed live proof. This commit does not install it.
In private reviewed server configuration, add the exact issue1031
`github_existing_session_bindings` record from retained work and independent
verifier session/turn IDs plus source head. A reusable structured session must
pass the pilot contract's exact input/result and trusted live idle-turn checks;
its `running` lifetime state is not rewritten to `completed`. Do not put session
IDs in this packet, change a session client ID, or launch replacements. With
`status:review` and the installed profile, an authorized coordinator calls
`POST /work/bind_existing` with only the goal and issue; it must return one
work/verify pair and no launch. Independently retrieve the check output, submit
the normal `verified` request, and verify one-close/retry. Reconcile uncertain
publication before any retry. The parent issue must remain open until every
cohort criterion, including the orchestration gap, is accepted.

If a new retained user input supersedes this reviewed pair before acceptance,
review and install a new exact binding for that latest work/verifier pair, then
repeat the same bind request. The prior pair remains audit history and cannot
verify after the configuration changes. This recovery is available only before
any old pair progress, verification intent/outcome, or uncertain close; those
states require reconciliation rather than another bind.

When that updated pair reuses the same retained sessions with newer turns, the
original link keeps the database session association and the newer link carries
the reviewed session ID in its immutable binding event. A returned null link
`session_id` in this narrow case is expected; it is not permission to launch,
attach, or alter a session. Repeat the identical bind request only to recover a
binding-only partial pair, then use the normal verified request.

## Structural orchestration verdict - still OPEN

The installed PR1030 continuity decision function was exercised read-only with preserved issue/authority/checkpoint facts:

- Stale idle owner: `nudge`; identical facts: same fingerprint, not advancement.
- Add blocked:human without any prepared handoff: `none/blocked_human`.
- Add a recent generic owner comment without a substantive result: `none`; stale clock is reset.
- Normalized facts contain no last-substantive-advancement or validated preparation/review-ready state.

Thus the supported product decision boundary does not enforce the preparation-vs-human-gate distinction or substantive advancement. Prompt corrections cannot accept this gap. This wake demonstrates **owner-mediated recovery** by preserving the original checkpoint and actually producing/running this packet; it does NOT prove autonomous Drover recovery. No live provider recovery side effect was exercised by the pure decision regression.

Actual OpenClaw scheduler run `588ab3da-f8a2-479a-85b7-abfd76bf7f25` returned NO_REPLY with `delivered=false`, `deliverySuppressionReason=silent`: unchanged-output suppression works on the real delivery path. Choosing NO_REPLY for an unchanged blocker remains owner policy, not product-level automatic semantic deduplication.

Remaining bounded structural acceptance: protect a substantive-advancement timestamp separately from check/activity, require concrete preparation or a validated external decision packet before a human hold, and exercise stalled-owner recovery plus duplicate-effect/delivery negatives on the supported pilot path. Preserve genuine human holds and distinguish this OpenClaw owner policy from Drover product enforcement. No generic new scheduler/native-owner rewrite is authorized by this packet.
