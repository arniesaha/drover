# Session Recovery After Harness Redeploy

## Problem

`drover-harnessd` owns structured-session drivers in memory. Restarting the
daemon during a deployment destroys those drivers. Its local registry correctly
marks the affected sessions as errored, but the central registry can continue to
report the same sessions as running. The iOS app therefore opens a normal chat
composer, while `POST /harness/sessions/{id}/turns` reaches the restarted daemon
and returns `404 unknown structured session`. The app reduces that response to
`Could not send — try again.`

The live failure was reproduced after local merge `887092b`: central reported
`harness-d5494480-84c4-4671-8f1a-186fbc37825c` as running, the Mac harness
reported it as errored with no PID, and a turn POST returned 404. The event
history still contained the Codex native session ID, proving that the provider
conversation remained resumable even though the Drover driver was gone.

## Goal

When a user sends a message to a structured Claude or Codex session whose
driver was lost in a harness restart, recover that same Drover session lazily,
resume the provider-native conversation, and deliver the message exactly once.
The chat remains on the same session ID and keeps its existing transcript and
worktree.

## Non-goals

- Proactively restarting every historical session when a daemon boots.
- Replaying a turn that was already in flight when the daemon stopped.
- Claiming resumability for providers whose resume contract is not verified.
- Recovering sessions after their worktree or provider-native transcript has
  been deleted.
- Replacing the existing explicit handoff/continue workflow.

## Chosen approach

Recovery is lazy and server-side. The first turn sent after a restart follows
the ordinary proxy path. If the owning harness returns the specific 404 that
means the structured driver is absent, central Drover asks that harness to
recover the original session ID and then retries the original turn once.

This is preferred over proactive startup recovery because it does not spawn
idle provider processes or revive abandoned work. It is preferred over creating
a replacement Drover session because the current chat screen, transcript URL,
stream cursor, and worktree can remain attached to one stable session ID.

## Recovery data

Recovery uses persisted Drover state only:

- session ID, harness, cwd, worktree path, model, and thinking effort from the
  harness session row;
- the most recent non-empty `native_session_id` in that session's event history;
- the existing maximum event sequence so newly emitted events remain monotonic.

The native ID must be persisted back to the session row whenever a structured
driver first reports it. Event-history lookup remains a compatibility fallback
for sessions created before that persistence change.

No message text or secret-bearing trace is added to recovery logs.

## Harness recovery endpoint

The harness exposes an authenticated, host-local recovery action for one
session. It accepts the expected provider-native session ID supplied by central
Drover and validates it against any locally persisted value before starting a
driver.

Under a per-session recovery lock it:

1. returns success without starting anything if the manager already owns a live
   entry for the session;
2. loads the local registry row and rejects unknown, terminated, unsupported,
   or non-structured sessions;
3. verifies that the cwd/worktree still exists;
4. restores a Claude or Codex structured driver with the original Drover
   session ID and provider-native resume ID;
5. restores the sequence counter from the registry's current maximum;
6. changes the local row from errored to running, clears its restart-loss error
   and terminal timestamp, and records a metadata-only `session.recovered`
   event.

Claude resumes its persistent stream-json process with `--resume <native-id>`.
Codex restores the driver's thread ID so its next turn uses
`codex exec resume <native-id>`. Gemini recovery is rejected until its resume
contract is verified by a live fixture.

## Central retry flow

For a turn action only:

1. Central proxies the turn normally.
2. A success or non-recoverable error is returned unchanged.
3. On `404 unknown structured session`, central obtains the persisted native ID
   from the session row or event-history fallback.
4. Central calls the owning harness's recovery action once.
5. If recovery succeeds, central retries the original turn once and returns that
   response.
6. If recovery is unsupported or impossible, central returns a conflict with an
   actionable explanation: the session cannot be resumed and should be
   continued in a new session.
7. Central synchronizes the recovered status and native ID into its registry and
   invalidates its short-lived fleet cache.

Permission and interrupt actions do not trigger recovery. An approval prompt or
in-flight process cannot survive a daemon restart, so silently recreating those
actions would be misleading.

## Exactly-once and concurrency guarantees

The retry is safe because harnessd checks for a live manager entry before
dispatching a turn. Its 404 is emitted before the driver receives or records the
message. Central retries only that precise pre-dispatch failure and performs at
most one retry.

The recovery endpoint serializes by session ID. Concurrent recovery requests
either create one driver or observe the driver already created. After recovery,
normal driver rules still serialize turns: one request can be accepted, while a
concurrent overlapping request receives the existing `turn already in flight`
conflict and follows the app's queue behavior. No recovery code retries a 409,
timeout, broken connection, or ambiguous 5xx response.

## Client behavior

Successful recovery is transparent to the current chat screen. The existing
message stream receives `session.recovered` followed by the accepted user input
and provider output.

When recovery is unavailable, the server-authored conflict text is surfaced by
`ChatModel` instead of the generic send error. The composer keeps the unsent
text and attachments. The existing handoff/continue control remains the escape
hatch for creating a linked replacement session.

## Testing

Tests will be added before implementation for:

- native session IDs being persisted when Claude and Codex report them;
- restoring a Codex driver with its prior thread ID and monotonic event sequence;
- restoring a Claude driver with the correct structured `--resume` command;
- idempotent recovery when the session is already live;
- concurrent recovery creating only one manager entry;
- central 404 -> recover -> single retry success;
- no recovery or retry for 409, timeout, 5xx, permission, or interrupt actions;
- missing native ID, missing worktree, terminated session, and Gemini returning
  an actionable conflict without losing composer contents;
- the original session ID and existing transcript remaining unchanged.

The focused Python gates are `tests/test_harness_daemon.py`, structured Claude
and Codex driver tests, and `tests/test_metrics.py`. The full Python suite and
DroverKit tests run before deployment.

## Deployment and live verification

This change modifies both central Drover and harnessd, so deployment restarts
both services. Before that restart, create a disposable Claude or Codex session
and capture only its Drover and native IDs. After restart:

1. confirm the local harness reports the session as restart-lost;
2. send one uniquely identifiable diagnostic turn through the central API;
3. confirm the response is accepted, exactly one matching `user_input` event is
   present, and subsequent provider output arrives on the same session ID;
4. confirm the central and harness registries both report the session running;
5. terminate the disposable session.

The pre-existing user session used to diagnose this bug must not receive a
diagnostic message during deployment verification.
