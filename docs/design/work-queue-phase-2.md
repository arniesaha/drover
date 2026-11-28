# Work queue phase 2

Control Postgres migration 18 (`WORK_QUEUE_MIGRATION`) adds `execution_links`
and append-only `decision_events`. Links retain provider and external goal refs,
run role, run key, canonical session/PR association, failure count and timestamps.
Events hold identities, action codes, categories and outcomes. They contain no
issue body, question text, comments, prompts or local checkout paths. Database
triggers reject updates and deletes of audit history.

The API owns one serialized `WorkService`. MCP forwards the verified caller's
bearer credential to that API; analytics never launches or mutates locally.
`drover_work_list/get/launch/link/bind_existing/update/question/decide` map to
`/work/<operation>`.
List and get use GET; the other operations use POST. Read responses use the
existing MCP row/text/byte bounds, admission and freshness envelope. HTTP reads
also bound text and bytes. Restricted credentials and cookie-only callers cannot
use these tools. Actor identities are derived from existing bearer credentials.

Enable the live service with `work_queue.enabled = true`,
`work_queue.shadow_mode = false` and the Postgres control backend. Keep the
versioned goal policy reference from phase 1. Configure `principal_roles` to map
an authenticated `credential:<id>` to the coordinator role from that policy.
The legacy operator bearer is identified as `control:operator` and also needs an
explicit role mapping.

**Who may launch.** `launch`, `link`, `bind_existing`, `update`, `question` and `decide` require the
policy's coordinator role (`authority.coordinator`, `nix` in policy v1). An
unmapped principal is a `worker` and gets a 403 `coordinator required`. The direct
hub API call uses the legacy bearer (`control:operator`); the MCP
`drover_work_launch` tool forwards the *caller's own* device/host credential, which
is `credential:<id>` and so needs its own `principal_roles` entry. A config that
only maps `control:operator` accepts curl but refuses MCP. Grant the role
deliberately, per credential. Launch denials now return `reason` (capacity,
`launch intent uncertain`, `systemic review required`, `item provider
unavailable`, `quota or capability unavailable`, `coordinator required`) and log it
server-side; the MCP tool reports `work control HTTP <status>: <error>; <reason>`.

**Slots.** A worker whose final turn declared `done`, or that the idle-undeclared
sweep reported, is idle at `awaiting: input` and no longer counts against the
goal/workspace worker caps. A new user turn makes it count again, so a goal can briefly exceed its cap if the
owner resumes a released worker. The same item still cannot be launched twice while
the finalised session exists. If the lookup fails, every live worker keeps its slot. `blocked` and
`needs-approval` workers keep their slot. Each goal accepts `execution_cwds`, a host ID to authorized
checkout mapping, and an optional opaque `brief_ref`. Keep local paths in private
runtime configuration. Set `analytics_boundary.api_url` to the control API for
MCP proxying. No real credentials, paths or workspace identities belong here.

Only the coordinator launches, links, schedules verification and accepts its
result. Host credentials can report progress/failure for sessions on their bound
host. Paired devices may resolve questions. Provider capabilities are refreshed
before decisions; unsupported approvals return a provider navigation URL.
Rejection queues revision without increasing failure count. Question text stays
in the authorized session or brief; questions accept only identities and action
codes. Get returns their metadata projection, including resolved/uncertain state.

Launches use existing provider quota hint semantics and capability declarations.
Each independent subscription must have known headroom for estimated percentage
cost plus the policy reserve fraction of its available quota. The shipped policy
reserves 20 percent. Calculations retain exact percentages rather than rounded
hint text. Worker capacity includes verification and uncertain intents. Unknown
quota or capability, provider outages and uncertain runs pause new launches.
Verification uses the linked work checkout on the same host so it checks the
resulting artifact.

A launch intent commits before calling the harness with that key as
`client_session_id`. Repeated requests resolve the existing link. After a lost
response or restart, a nullable link resolves through the control registry or a
read-only daemon lookup by key, including terminal sessions. Missing or
unreachable outcomes remain inspectable and block relaunch. The daemon lookup
never sends another launch request.

`bind_existing` is a coordinator-only, exact-GitHub-pilot operation. It has
only `goal_id` and `number`; reviewed server configuration, never its caller,
selects a settled work session/turn and a distinct settled verifier session/turn.
It does not alter either session's `client_session_id`, launch a replacement,
or apply to `local_file`. A lifetime-completed session uses its exact successful
terminal exit. A supported structured session may remain `running` only when
its exact durable input/result window and the owning daemon's idle-turn fact
both pass; routine `awaiting` state is never enough. The service reads their
retained repository identity, pins deterministic work and verify links, and
rechecks both before an acceptance request. Its precise configuration and error
contract is in the
[pilot contract](../operations/github-acceptance-pilot.md#existing-session-reuse).
Only the current installed exact pair can use that resumable exception at
`verified`; an older pair remains append-only history. A later reviewed pair
can be bound only when the old pair has no decision event beyond its original
binding, so an uncertain provider effect always remains inspection-required.
If the later pair uses newer turns of the same retained sessions, the original
existing link keeps the database's sole `session_id` association. The newer
link is deliberately sessionless at the row level but has its immutable binding
event; it is not a generic unresolved launch and may be retried only under the
same existing-session scope, source, profile, role, and binding-only-history
checks.

`update(action="complete")` schedules a separate verify run and leaves the issue
open in review. For the default path, acceptance requires that run to finish,
affirmative behavioral and rerun-check results, an opaque evidence identity,
and unchanged acceptance criteria. An exact-issue GitHub pilot profile replaces
caller assertions with provider-resolved PR/head, check-run/output, and the
installed profile's human-review or independent-check facts; its
request/response, unavailable, amendment, and idempotency behavior are
specified in the [pilot contract](../operations/github-acceptance-pilot.md).
For a separately installed `github_event_delivery_profiles` exact revision,
the signed GitHub webhook worker may continue a deferred work or verifier
report only after an exact-head completed CI rollup. `WorkService` re-reads the
PR source, immutable criteria/profile revision, and required checks through the
provider before it schedules a verifier or accepts evidence. Webhook fields are
not acceptance facts. Missing webhook configuration, stale delivery,
unavailable reads, or failed/pending/skipped required checks leave the durable
intent pending; the event worker has no provider-write path or model loop.
Work runs and failed behavioral checks cannot close issues. A failure queues
revision until the configured attempt budget is exhausted, then blocks and
writes the generated systemic-review comment. Further launches remain blocked
for coordinator inspection. A bounded changed-approach override after review
is outside this implementation; there is no automatic fourth attempt.

Provider update/decision intents precede consequential external actions. Outcomes
append afterward; a timeout keeps the intent uncertain and prevents automatic
repetition. Public comments use an explicit allowlist of generated sanitized
text. Free-form text is rejected by the tool schema rather than copied to audit.
Attention UI, notifications and legacy cutover remain separate work.
