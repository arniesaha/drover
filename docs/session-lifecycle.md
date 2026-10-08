# Hub session lifecycle: iteration 1

The [ADR](adr/0002-hub-session-lifecycle-first-iteration.md) defines scope.
This iteration observes and records. Lifecycle policy never stops a process,
hides a session, creates an archive, writes archive refs, removes a worktree,
or prunes Git registrations. Existing daemon end cleanup and startup cleanup
retain their previous behavior. Explicit user terminate remains available.

## Configuration and reports

```toml
[lifecycle]
mode = "report"
idle_after = "12h"
```

`mode` accepts `off` or `report`; the default is `report`. `enforce` is rejected
with `lifecycle.mode must be off or report; enforce is not wired`.
`idle_after` accepts a positive integer followed by `s`, `m`, `h`, or `d`.
Changes take effect when the hub restarts. No host configuration is needed.

The public hub roles (`api` and `all` with the HTTP listener enabled) scan at
startup, then every five minutes plus up to 30 seconds of jitter. The analytics
role does not run lifecycle scans. Duplicate report workers only repeat
observations; they cannot schedule destructive operations.

Authenticated `GET /harness/lifecycle` runs a scan and returns:

```json
{
  "mode": "report",
  "scanned_at": "2026-10-07T12:00:00+00:00",
  "sessions": [
    {
      "session_id": "example-session",
      "would_expire": false,
      "reasons": ["publication_evidence_missing"]
    }
  ],
  "hosts": [],
  "summary": {"would_expire": 0, "worktrees": 0, "unsupported_hosts": 0}
}
```

`off` returns empty observations and performs no host probes. The harness fleet
snapshot includes `lifecycle`, the last successful scan summary. Public
`GET /healthz?detail=1` returns `{ok: true, lifecycle: ...}` with the same summary.
The ordinary `/healthz` response stays compatible with existing health checks.
Before the first completed scan, summary state is `not_scanned`. The scan time
shows freshness; a failed periodic scan leaves the last successful summary.

Idle candidates require `running`, no `ended_at`, structured input attention,
and known activity at least `idle_after` old. There is no fallback to heartbeat
or `updated_at`. Approval, tool-running attention, terminal/unknown statuses,
collectors, PTY attention, missing/future activity, and unverified host liveness
are protected. An explicit `keep` policy, live Factory owner lease, missing
Factory owner evidence, missing publications, open PRs, and missing/stale PR
verification also protect a session. Existing Factory ownership is read from
`factory_observer_runs`; no new watcher leases are introduced.

There is currently no configured GitHub PR-state integration in this repository.
PR verification therefore stays unknown and reported publications cannot by
themselves make a session eligible. TODO: refresh PR observations through the
hub's configured integration when one exists, with repository identity and
freshness checks. No new GitHub client or content matching is included.

## Publication reports

Use the hub's existing authentication for both routes:

- `POST /harness/sessions/{id}/publications` records a contribution snapshot.
- `GET /harness/sessions/{id}/publications` returns `{publications: [...]}`.

POST accepts exactly these fields:

```json
{
  "repo": "example/project",
  "pushed_branch": "feature/contribution",
  "pushed_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "session_head": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "base_sha": "cccccccccccccccccccccccccccccccccccccccc",
  "pr_number": 123,
  "source": "orchestrator"
}
```

`pr_number` is optional. SHAs must be full 40- or 64-character hexadecimal
object IDs. `repo` must be `owner/name`, and must match the session repository
when that identity is known. `source` accepts `orchestrator`, `push_capture`,
or `operator`. URLs, credentials, and unsupported fields are rejected before
storage. Authorization headers are never part of a stored report.

Success is HTTP 200 with `{publication: ...}`, including a stable publication
ID, report time, actor `authenticated_user`, and `pr_state=unknown`.
The current authentication boundary does not supply a finer actor identity.
Identical normalized snapshots for a session are idempotent. Different reports
create immutable snapshots, including moved branch heads or another PR.
Multiple sessions can report the same PR, and a session can report several PRs.
Missing sessions return 404; invalid reports return 400. A report is provenance,
not proof of merge or permission to delete work.

Orchestrators should report after a successful push and again when the PR
number becomes known, using the exact pushed SHA and session HEAD/base SHA at
that time. No PR-opening orchestrator or push interception producer exists in
this repo to wire automatically. Adoption by external producers is a separate
step; the API is ready for them. Do not infer contribution coverage from branch
names or ancestry, especially after squash merges.

## Explicit terminate and confirmation

The existing `POST /harness/sessions/{id}/terminate` persists a user stop intent
before contacting the host. A confirmed stop keeps the existing success body.
A host 404, 502, 504, transport error, accepted-but-unconfirmed response, or
invalid acknowledgement returns HTTP 202:

```json
{
  "session_id": "example-session",
  "operation_id": "stable-operation-id",
  "state": "pending",
  "status": "running"
}
```

A 202 is an accepted request, not proof that the process ended. The session keeps
its status and `ended_at`; clients should continue showing it until confirmation.
Repeated requests reuse the operation for the current host/session generation.
On host registration/heartbeat, the hub reads the host session, confirms a
terminal result, or retries explicit user intent for a known live session.
A host 404 alone stays pending. Reconciliation also runs on direct session
reads. Confirmed operations set `end_reason=user` and the observed end time
without changing historical attention. Already confirmed retries are idempotent.
Recovery increments the hub generation so old pending operations cannot settle
or replay against that recovered generation. Retired hosts are not dispatched.
Explicit stop reconciliation continues even when lifecycle reporting is off.

This iteration uses the existing daemon terminate/session APIs as confirmation.
The design's host operation journal, operation-ID receipt protocol, distributed
claims, backoff queue, and local generation negotiation remain future work.
The hub fences operations by its recorded host and generation. Only explicit
user stops are replayed; no policy stop queue is activated.

## Worktree inventory and persistence

New harnessd advertises `lifecycle.worktree_inventory=1`. Authenticated
`GET /lifecycle/worktrees` on the host inspects known session repositories and
preserved directories under canonical `~/.drover/worktrees/*`, then runs
`git worktree list --porcelain -z`. It also reads Git identity and status,
including ignored and untracked files. No mutating Git commands run.

Entries include path, repository identity, session ID when matched, branch or
detached HEAD, base SHA when known, observed HEAD, ownership, local-change
status, reasons, and `would_collect`. Only a canonical direct child of the
default root with a matching session/path can be owned. Symlinks, alternate
roots, main checkouts and ambiguous entries are foreign. Inspection failures
are reported in `errors`. No recursive search of unrelated repositories runs.

The hub reports each host as `reported`, `unsupported`, or `unavailable` and
persists observations keyed by host/path. Older hosts are `unsupported`, never
assumed empty. Dirty or ignored/untracked work, live sessions, explicit keep,
and unfinished/unknown contributions have keep reasons. A daemon may report
an untouched candidate, but the hub retains it pending complete publication
and owner checks. No inventory result authorizes collection in this iteration.
Cached inventory rows retain their observation time; current report output
identifies unavailable hosts rather than treating old inventory as fresh.

PostgreSQL migration 13 adds outcome, visibility, retention metadata and durable
operations. Migration 14 adds publications and worktree inventory. Released
migrations 1 through 12 are unchanged. Local DuckDB control stores receive
additive equivalent tables/columns. Host status/activity writes preserve
hub-owned retention and visibility fields. `archived_at` is stored but never
set by policy, and no new session status strings are introduced.

Terminal `completed`, `terminated`, `errored`, and `failed` sessions project
`awaiting=null` through hub/web/iOS-facing APIs, MCP, and daemon session APIs.
Raw historical attention stays available to registry readers. The existing
concurrent structured-session raw attention invariant remains tested.
