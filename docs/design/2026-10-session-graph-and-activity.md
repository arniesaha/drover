# Session Graph And Project Activity Without Spans

Status: accepted, 2026-10-01. Issue #473 (re-scoped), program #476.

Spans leave the production surface (see
[Optional span integration](../optional-span-integration.md)). Both views were
span-backed: `drover-server session graph` drew a span tree, and the MCP tool
`drover_project_activity` summed span cost by repo. Neither has an app screen
today. This note decides what each view answers using Drover's own data.

## What Drover Already Holds

- `harness_sessions` (control plane): host, harness, repo/branch/cwd, status,
  `awaiting` (`input`/`approval`), `started_at`/`last_activity`/`ended_at`,
  `last_error`, and one lineage hop: `source_session_id` + `handoff_mode`
  (`nexus_handoff`, `native_resume`, `factory_observer`). Factory runs encode
  `factory/<run_id>@<revision>` there.
- `live_session_recaps` and session previews: a readable line per session.
- `agent_event_day_summary`: per-day, per-session first/last event and repo,
  for native sessions as well as launched ones.
- PostgreSQL `session_memory`: summary, next steps, open questions (Phase 3).
- `session_usage`: per-session token totals from the harness stream.

Not held: a delegation parent, a session title, or commit/PR refs (only the
iOS client extracts PR URLs from transcripts). Worktree sessions do record
their branch.

## Session Graph

- **Who:** the operator, or an orchestrating agent, holding one session id or
  a Factory run id.
- **Question:** "What is working on this piece of work, who started it, and
  where is it stuck?"
- **Next action:** open the stuck session (awaiting input/approval or
  errored), or stop a duplicate.
- **Answer from own data:** a tree rooted at the work: Factory run, handoff
  chain, or explicit parent. Each node carries harness, host, state (`running`,
  `awaiting_input`, `awaiting_approval`, `idle`, `done`, `failed`), last
  activity, last error, repo/branch and tokens. `stuck` lists nodes awaiting a
  person, errored, or running with no activity for 30 minutes.
- **Gap closed here:** add an optional `parent_session_id` launch field so an
  orchestrator can record delegation explicitly. `source_session_id` cannot be
  reused for this: the host adopts an existing live session with the same
  source, which would collapse siblings into one. Nothing is inferred.
- **Verdict: ship** as a CLI (`drover-server session graph`) and an
  authenticated HTTP route (`GET /harness/sessions/{id}/graph`). Most sessions
  today are single nodes, so no app screen is added for the release. The
  answer is still correct and useful for Factory runs and handoff chains, and
  becomes richer as orchestrators send `parent_session_id`. The span tree
  stays behind `--spans` when the integration is on.

## Project Activity

- **Who:** the owner returning to a project, or an agent starting work in it
  via MCP.
- **Question:** "What happened on this project recently, and what is still
  open?"
- **Next action:** resume an open session, answer a waiting one, or pick up a
  listed next step.
- **Answer from own data:** a per-project timeline for a window (default 7
  days, max 30). Sessions are grouped by UTC start day with a title (recap
  line, else summary line), harness, host, state, duration, branch and tokens.
  Open items are waiting/errored sessions plus the latest summaries' next
  steps and open questions. Counts are sessions, active hours (union of
  session intervals, so parallel sessions are not double counted) and tokens
  from `session_usage`. Commit/PR refs are omitted rather than guessed.
- **Verdict: ship** in MCP `drover_project_activity` and an authenticated
  HTTP route (`GET /projects/activity`), with hard caps: 20 projects, 200
  sessions, 30 days, 20 open items, and 300-character text fields. The iOS
  cockpit's Popular Projects ranking already reads sessions and tokens from
  the same sources; with spans off it hides cost and latency instead of
  showing zeros. No new app screen for the release.

## Not Doing

- No inference of parentage or project from traces.
- No dollar cost; it was never priced (#288).
- No per-turn latency; that waits for native harness hooks after the first
  production release.
