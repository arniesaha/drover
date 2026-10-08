# ADR 0002: Hub session lifecycle, first iteration observes and records only

- **Status:** Accepted
- **Date:** 2026-10-06
- **Decision owners:** Drover maintainers
- **Issue:** [#547](https://github.com/arniesaha/drover/issues/547)
- **Design:** [Hub-side session lifecycle](../design/session-lifecycle.md)

## Context

Sessions that are still running but waiting for input are never expired, and
their worktrees are never collected by the hub. Today an external nightly
`reap` script and manual cleanup fill that gap. The 2026-10-06 manual cleanup
showed why a naive policy is unsafe:

- Session work was squash-merged under other branch names, so neither the
  session branch nor commit ancestry shows whether it landed.
- Unmerged design work looked identical to abandoned work.
- Some worktrees held only untracked files; some lived outside the hub root.
- Terminate leaves the worktree behind, and reports success on host 404/502
  without confirming the process stopped.

The design document answers the seven open decisions in its section 9. This
ADR records the answers and narrows the first iteration.

## Decision

The first iteration observes and records. It does not stop, archive, hide or
delete anything on its own. Enforcement waits for a week of report output that
has been reviewed against reality on both NAS and Studio.

1. **Publication authority.** An authenticated orchestrator report API is the
   contract (`POST /harness/sessions/{id}/publications`, many-to-many, idempotent).
   Push-time capture is a second producer. GitHub refresh by the hub verifies PR
   state. Content comparison against main is out of scope for the first
   iteration; with squash merges it produces false "merged" results.
2. **State model.** Keep existing status values. Add `end_reason`,
   `archived_at` and a separate worktree state. No new status strings.
3. **Idle expiry.** Only sessions waiting for input, after 12h, with the
   fail-closed protections from the design (open PR, live owner or watcher,
   explicit keep, unknown evidence). First iteration runs in `report` mode
   only; `enforce` is not wired.
4. **Unfinished work.** Retained by default. A closed or missing PR does not
   make work disposable. "Archive then collect" is not built in this iteration.
5. **Ownership.** Only canonical `~/.drover/worktrees/*` with a matching
   session record. Everything else is reported, never removed.
6. **Archives.** Deferred, since nothing is collected. When built: Git bundle
   plus diff and untracked archives, kept on hub-controlled storage, no
   automatic expiry. Archive refs are never pushed to the public GitHub remote.
7. **Visibility and retirement.** 7 day terminal grace before `archived_at`
   hides a session, explicit history access always. The external `reap` script
   is retired only after hub enforcement has run for a week on both hosts,
   which belongs to a later iteration.

Two defects found by the design ship first, as their own change:

- Terminal sessions must project effective awaiting as null (status first) in
  cockpit, iOS and MCP, so a finished session can no longer appear to wait for
  approval (#234). Raw historical awaiting is not rewritten.
- Terminate must not tombstone a session or set `ended_at` on host 404/502.
  It records a pending, unconfirmed stop and reconciles when the host answers.

### First iteration scope

- The two defect fixes above.
- Additive schema: `end_reason`, `archived_at`, `retention_policy` (+ reason),
  publications ledger, worktree inventory records.
- Publications API and its use from the orchestrator when it opens PRs.
- Report-only lifecycle scan: "would expire" sessions and worktree inventory
  with keep/collect reasons, surfaced through hub health and an API.

Explicitly out of scope: enforcement, archive creation, collection, hiding
sessions, watcher leases beyond existing Factory owners, content matching,
retiring `reap`.

## Consequences

### Positive

- The highest-risk actions (stop, delete) are gated by real evidence from a
  week of reports instead of assumptions.
- Squash-merged and renamed work becomes recognizable through explicit
  provenance rather than branch names.
- Two user-visible correctness bugs are fixed independently of the policy work.

### Costs

- Worktree disk use and quiet sessions continue to need the external `reap`
  script and manual cleanup until enforcement ships.
- Orchestrators must adopt the publications API; sessions without reports stay
  unclassified and retained.
- The schema is designed for enforcement now but only partly exercised until
  the second iteration.
