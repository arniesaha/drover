# #1043 owner-held live work, verification, and receipt brief

This is the reviewed task contract for the exact GitHub event-adoption pilot:
issue #1043, PR #1042 head
`bde283aa6bd4f97073867930153c8d12e9b8b85e`, profile revision
`df895b4aafeb94a8d356cbcd9dfc277642e67989dc5ce9202a07f96544c95cfe`, and
behavioral check `github-event-adoption-1043-outcome` from App `15368`. The
installed runtime merge commit is separate provenance and is not acceptance
source evidence. GitHub API polling is the configured ingestion transport; it
is not a webhook and the bootstrap cron is repair only.

## Bound work and verification turns

The bound work deliverable is read-only evidence: inspect the already merged
PR1042 source, run the profile's named focused regressions, and retain their
exit, JUnit, and log-hash observations. Do not edit source, create commits,
push, launch another task, or perform out-of-scope work. While its signed
structured turn is still running, the work agent must call its own
`drover_work_update(action="complete")` MCP tool. The coordinator must not
submit that operation for it. The deferred completion stays pending until that
exact successful work turn terminates and current provider facts and required
checks permit continuation.

The verifier must be a distinct bound session and turn. It independently reads
current provider source, required checks, and the retained behavioral check
output. While its own signed structured turn is still running, it must call
`drover_work_update(action="verified", evidence_key="check-run:<published-id>")`.
The coordinator must not submit verification. Caller pass flags, issue text,
and mutable candidate files are not evidence. A stale head, changed profile or
criteria, wrong App/check/output, missing or failed required check, or an
unfinished/busy work or verifier turn must stop without a close or ACK.

## Native receipt boundary

After a provider-verified close, the installed GitHub native bridge alone
re-reads the closed current facts and terminal bindings. It may create the
bounded deterministic digest receipt through `github_native_owner_effect`,
persist the exact fenced action intent, and ACK only the matching wake,
fingerprint, owner epoch, and integration fence. A close is not an ACK.
Recovery reconciles a complete matching receipt before ACK and never repeats
the effect. This brief grants no external write: no merge, deployment,
publication, configuration, credential, deletion, payment, public action, or
manual/synthetic acknowledgement.

Preparation is not installation or acceptance. The owner must preserve the
exact brief file and checksum through final private configuration validation;
any changed contract requires a new reviewed packet revision.
