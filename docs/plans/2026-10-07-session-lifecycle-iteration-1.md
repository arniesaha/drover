# Session lifecycle iteration 1

Authority: ADR 0002. Observe and record only. No lifecycle enforcement, archive
creation, hiding, worktree removal, prune, or archive refs. Existing daemon
cleanup remains outside lifecycle policy.

## Ordered commits

1. Status-first projection (#234). Add effective attention to
   `src/drover/server/harness/models.py:76`; use it in the shared hub wire
   projection (`src/drover/server/metrics.py:965`) and daemon projections
   (`src/drover/server/harness/daemon.py:3055`, `:4507`). Preserve raw rows.
   Test terminal matrix, concurrent structured sessions, unsequenced exits and
   restart/backfill with scoped registry and structured tests.
2. Honest terminate. Replace offline tombstoning in
   `src/drover/server/metrics.py:1830`, validate host stop acknowledgement in
   `:2711`, and reconcile pending intent on host registration (`:1590`) and
   session reconciliation (`:2391`). Persist intent before dispatch, fence by
   host and generation, keep unknown 404 pending. Existing daemon terminate
   (`src/drover/server/harness/daemon.py:3276`) remains an explicit user action.
   Migration 13 adds operations and session outcome/policy columns. Mirror
   additive local schema in `src/drover/server/harness/schema.py:39`.
3. Publications and inventory schema. Migration 14 adds contribution snapshots
   and worktree inventory tables. New lifecycle store module uses the existing
   control-plane connection. Host writes continue to update only their existing
   columns, preserving hub policy. Add authenticated publication routes in
   `src/drover/server/web/app.py:1171`, `:1544`.
4. Report scan and read-only daemon inventory. Add lifecycle config alongside
   `src/drover/config.py:385`; default report, idle_after 12h, reject enforce.
   Add host inventory endpoint and capability. Fail closed on missing/future
   activity, unknown PR verification, Factory correlation and explicit keep.
   Surface scan through authenticated hub API and health detail.
5. Document API/config, limitations, and CHANGELOG.

## API contracts

- Terminate: confirmed host stop preserves existing success response; uncertain
  response returns 202 with session_id, operation_id and state=pending.
- POST /harness/sessions/{id}/publications accepts repo (owner/name),
  pushed_branch, pushed_sha, session_head, base_sha, optional pr_number, and
  source (orchestrator, push_capture, operator). Snapshot hash is the idempotency
  key; multiple sessions may report the same PR. GET returns publications.
  No credentials, URLs or arbitrary extra fields accepted. PR verification
  stays unknown until a configured integration exists; no new GitHub client.
- GET /harness/lifecycle runs a report scan and returns mode, scanned_at,
  sessions with would_expire and reasons, per-host inventory/capability state,
  and summary. GET /healthz?detail=1 includes cached lifecycle summary.
- Host GET /lifecycle/worktrees reports git worktree list --porcelain results
  from known repositories. Canonical default-root entries with matching session
  records are owned; every other entry is foreign. No mutating Git commands.

## Configuration and validation

[lifecycle]: mode=off|report (default report), idle_after="12h".
No scheduled or request path dispatches policy stops. Explicit user-stop
reconciliation is independent of lifecycle mode.

Scoped checks for each commit: new lifecycle/projection tests plus relevant
existing proxy, registry, structured and daemon tests. PostgreSQL tests cover
fresh schema, upgrading existing schema and repeated bootstrap. Pin hashes for
13 and 14 without changing released hashes. Real temporary Git inventory tests
check foreign/symlink paths and prove report leaves refs and worktrees intact.
Public API tests cover authentication and invalid publication credentials.
Run git diff --check after each slice. Docs contain no em dashes or private
machine details. Commit only: no push, PR, merge, deploy or host configuration.
