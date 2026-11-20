# Local native-candidate canary: M3 committed provenance

This brief is valid only when an explicitly configured `local_file` canary goal
provides a **hub-signed** `committed_provenance` work binding for the exact
candidate. It does not change the marker-only default. A candidate body, label,
or expected-effect reference cannot enable this exception.

## Permitted worker action

For that exact `work` run only, use the daemon-assigned isolated worktree to
create the harmless source, focused-test, and JSON evidence files named by the
reviewed candidate body. Commit those files on the session branch so a separate
`verify` run receives immutable parent provenance. This is local-only: do not
push, merge, deploy, change configuration or data, access credentials, invoke
external services, or write outside the assigned worktree.

The exception is unavailable to verifier runs. A verifier must inspect the
already committed parent files and rerun the named local focused check; it must
not create, replace, commit, or reconstruct missing evidence. Missing, changed,
or untracked-only evidence remains a failed verification.

## Native owner action

The normal expected-effect, native lease, action-intent, receipt, and ACK
contracts remain unchanged. A generic owner marker never proves the candidate
effect. The signed session-relative `local-file:` artifact must be attested in
the worker session worktree before the owner can settle the terminal wake.
