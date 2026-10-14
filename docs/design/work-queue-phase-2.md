# Work queue phase 2

Control Postgres migration 18 (`WORK_QUEUE_MIGRATION`) adds `execution_links`
and append-only `decision_events`. Links retain provider and external goal refs,
run role, run key, canonical session/PR association, failure count and timestamps.
Events hold identities, action codes, categories and outcomes. They contain no
issue body, question text, comments, prompts or local checkout paths. Database
triggers reject updates and deletes of audit history.

The API owns one serialized `WorkService`. MCP forwards the verified caller's
bearer credential to that API; analytics never launches or mutates locally.
`drover_work_list/get/launch/link/update/question/decide` map to `/work/<operation>`.
List and get use GET; the other operations use POST. Read responses use the
existing MCP row/text/byte bounds, admission and freshness envelope. HTTP reads
also bound text and bytes. Restricted credentials and cookie-only callers cannot
use these tools. Actor identities are derived from existing bearer credentials.

Enable the live service with `work_queue.enabled = true`,
`work_queue.shadow_mode = false` and the Postgres control backend. Keep the
versioned goal policy reference from phase 1. Configure `principal_roles` to map
an authenticated `credential:<id>` to the coordinator role from that policy.
The legacy operator bearer is identified as `control:operator` and also needs an
explicit role mapping. Each goal accepts `execution_cwds`, a host ID to authorized
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

`update(action="complete")` schedules a separate verify run and leaves the issue
open in review. Acceptance requires that run to finish, affirmative behavioral
and rerun-check results, an opaque evidence identity, and unchanged acceptance
criteria. Work runs and failed behavioral checks cannot close issues. A failure
queues revision until the configured attempt budget is exhausted, then blocks
and writes the generated systemic-review comment. Further launches remain
blocked for coordinator inspection. A bounded changed-approach override after
review is outside this implementation; there is no automatic fourth attempt.

Provider update/decision intents precede consequential external actions. Outcomes
append afterward; a timeout keeps the intent uncertain and prevents automatic
repetition. Public comments use an explicit allowlist of generated sanitized
text. Free-form text is rejected by the tool schema rather than copied to audit.
Attention UI, notifications and legacy cutover remain separate work.
