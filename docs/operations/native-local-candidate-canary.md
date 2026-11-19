# Local native-candidate canary

This brief is valid only for an explicitly configured `local_file` work-queue
goal in an isolated canary allocation. It is not a production workflow and it
does not authorize public provider, repository, deployment, credential,
configuration, or data actions.

## Worker task

The local candidate body may request one harmless source-work marker in its
assigned configured worktree. It must use only that worktree, make no source
commit, and report the marker path and a focused local verification result.

## Native owner action

For one delivered wake, use the native continuity lease, consume, action-intent,
verify, and acknowledgement contract. Before any marker write, persist the
action intent for the exact wake and action fingerprint. Write one JSON marker
under the owner's configured isolated worktree at
`.drover-native-canary/actions/<wake-id>.json`. Its content must identify only
the wake and action fingerprint. If that marker already exists, reconcile it
instead of writing a second effect. Verify the marker, record the completed
receipt, and acknowledge the same delivery. Do not acknowledge a different wake
or act after a lost lease epoch.

For a candidate whose harmless effect is acceptance evidence, its local-file
manifest must carry `expected_effect_artifact`. The WorkService signs that
logical candidate reference into a session-relative
`local-file:.drover-native-canary/...` coordinator binding, and the hub stores
that immutable reference on the run before owner delivery. Before it sends an
owner turn, the hub asks the paired daemon to attest that the exact reference
names a regular file under the recorded source session's isolated worktree. A
completed action intent must use the exact reference; a generic wake marker is
not evidence for the candidate effect and cannot be acknowledged. The reference
is not a host path for the hub to dereference, and a missing or
out-of-worktree artifact leaves the wake pending without an owner turn.

The only allowed action is this local marker. A missing, malformed, or
out-of-scope instruction is a fail-closed condition: retain the durable wake
without an acknowledgement and request no external workaround.
