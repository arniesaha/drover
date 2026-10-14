# Work queue phase 1: shadow polling

The [work queue spec](work-queue.md) governs this implementation. GitHub remains authoritative. Phase 1 adds provider reads, a versioned goal policy and a disposable actionable report. It creates no database tables, execution links, launches, notifications or decisions.

Enable the polling worker in the control API (`runtime.role = "api"` or `"all"`) using runtime TOML:

```toml
[work_queue]
enabled = true
shadow_mode = true
workspace_id = "workspace-example"

[[work_queue.goals]]
goal_id = "2026-w42"
repo = "example/project"
goal_label = "goal:2026-w42"
# Use milestone = 7 instead of goal_label for milestone membership.
# Optional: policy_path = "/authorized/config/goal-policy-v1.json"

[work_queue.goals.credential]
mode = "token"
token_env = "DROVER_WORK_QUEUE_GITHUB_TOKEN"
capabilities = ["issues_read", "pulls_read", "checks_read", "actions_read"]
```

Credentials follow the existing runtime environment-reference pattern. Config holds the reference, never the secret. Use a repository-scoped token, or set `mode = "app"`, `installation_id` and `repository_id` with an externally refreshed GitHub App installation token in the referenced variable. Installation repository membership is checked before reading the scope. Managed installation provisioning and token minting are outside this phase. Each goal has its own credential reference and repository boundary. Item refs from another workspace or repository are rejected. Token actors are resolved from GitHub authentication; App actors use the configured installation credential identity after the repository check. Request bodies cannot choose an actor or workspace.

The strict `GoalPolicy` schema in `src/drover/server/work_queue/config.py` covers authority, acceptance, priority, retries, capacity, quota, progress, window, polling and retention. Its JSON schema is available through `GoalPolicy.model_json_schema()`. All v1 default values live in the packaged `policy-v1.json`. A goal can reference a complete versioned policy file; the worker reloads it each pass. Unknown versions, unsupported fields and invalid thresholds fail validation. Policy changes have no provider or audit side effects in shadow mode; phase 2 owns consequential action auditing.

The default label set has four status labels (`status:queued`, `status:running`, `status:review`, `status:blocked`), configured agent labels, `priority:urgent`, `priority:normal` and a scoped `goal:<name>` label. Milestones are an alternative goal selector. Agent names and label namespaces come from the goal policy. The adapter reads existing label and milestone definitions without creating them. Missing, unknown or conflicting status labels, unknown or multiple agent/priority labels and conflicting goal labels produce diagnostics and skip that issue. Closed issues are terminal regardless of old labels.

Enabled polling requires the existing control credential store. Refresh-failure diagnostics use its server identity; successful provider observations use the authenticated provider actor. A paired device bearer credential can read `GET /work-queue/report`. The existing authentication gate applies, followed by a paired-device requirement. The response includes workspace and provider actor identities on every action and diagnostic, stable action IDs, proposed action kinds, freshness and per-goal diagnostics. Bodies, comment text, label text and provider error bodies are absent. The route uses `Cache-Control: no-store` and never triggers a provider refresh itself.

Queued issues produce launch candidates; running issues require execution inspection; review issues produce verification candidates; blocked issues wait. All candidates have `execution_allowed = false`. Running labels never establish session liveness. PR review/draft state, failed checks and pending environment approvals add inspection candidates. Missing attention-read capabilities are diagnosed. Environment decisions offer provider navigation only. Reads use GitHub REST pagination, ETags, a bounded disposable cache and rate-limit backoff. See the [GitHub workflow run API](https://docs.github.com/en/rest/actions/workflow-runs) for the pending deployment read.

Polling defaults to two minutes. Each report replaces the previous projection rather than appending actions. Stable IDs deduplicate overlapping scans and scopes. `new_action_ids` from a poll identifies newly unmet conditions; unchanged polls return an empty list while retaining unresolved candidates in the report. Failed refreshes suppress actionable candidates, preserve the last successful refresh time and retain deduplication state through repeated outages. `refresh_age_seconds` grows between reads and `stale` becomes true after the configured freshness threshold. A successful conditional read revalidates freshness. Criteria changes alter the verification candidate ID without exposing issue content.

The worker supports shutdown through the control service stop event. It starts only when enabled, runs serially in one process and does not run in the analytics role. Non-shadow mode is rejected until execution intents, quota checks, session lookup, verification and auditing are supplied in phase 2. Restart resets this disposable projection; stable IDs remain identical, and no side effects are possible. Persisted run-key recovery belongs to phase 2.

No legacy file reconciler was present in this checkout, so the report exposes actionable rows and diagnostics directly. Comparison against a deployed file reconciler and a live configured GitHub repository remains an operational shadow validation step.
