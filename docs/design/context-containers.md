# General context containers

Drover remains a local-first, personal, open-source harness context store and
handoff layer. Repository attribution is still valuable for code work, but it is
no longer the only shape of resumable context. A **context container** is a
confidence-aware record that describes what a local agent session was about and
how another agent can resume it.

## Container types

Supported `container_type` values:

- `code_project`: source-code project context. May include `repo_owner`, `repo_name`, and `branch` evidence.
- `operational_project`: infra/admin/runtime work that may not live in a repo.
- `personal_project`: personal planning, household, health, writing, or other personal goals.
- `research_thread`: exploratory investigation around a topic, paper, technology, product, or decision.
- `open_floor_conversation`: broad conversation without a committed project container yet.
- `general_activity`: explicit broad/local activity such as a home-directory shell or harness maintenance session. This is intentionally separate from missing repo metadata.

## Schema slice

`context_containers` is keyed by `context_id`, not repo. Repository columns are optional evidence:

- Classification: `container_type`, `label`, `source_harness`, `confidence`, `evidence`
- Resume state: `summary_md`, `last_touched_at`, `next_action`, `open_loop`, `session_ids`, `task_ids`
- Optional repo evidence: `repo_owner`, `repo_name`, `branch`
- Safety: `redaction_policy`, `created_at`, `updated_at`

This distinction means `/home/Arnab` or another broad local workspace can become an explicit `general_activity` container instead of being counted as a bad repo attribution. Runtime audit and quality reporting should continue to report intentional general activity separately from true attribution failures; this PR preserves the existing `general_workspace` split and adds a resumable container table/MCP surface for future classifiers.

## MCP surface

This slice adds pure MCP tool functions and registrations:

- `drover_recent_contexts(container_type?, source_harness?, limit?)`
- `drover_context_brief(context_id? | label?)`
- `drover_open_loops(container_type?, limit?)`
- `drover_resume_context(context_id? | label?, max_summaries?)`

Existing repo-first tools (`drover_handoff`, `drover_project_brief`,
`drover_recent_sessions`, `drover_project_activity`, `drover_recall`, etc.)
remain the preferred surface for code-repo handoffs.

## Redaction policy

Context containers must store resumable summaries and metadata, not raw private transcripts. The default `redaction_policy` is `session-summary-redacted`, meaning container text is derived from existing summarized material with unnecessary personal detail removed; credentials, tokens, API keys, passwords, and connection strings are never preserved and are replaced with literal `[REDACTED]`. If a container is metadata-only (for example, explicit `general_activity` created from cwd/source signals), use `metadata-only`. Future automated extractors should preserve source links (`session_ids`, `task_ids`) and avoid copying raw message content unless it is already covered by Drover summarization redaction.

## Migration path from agent-shared semantics

Existing agent-shared/HANDOFFS/DECISIONS/GOALS-like content maps naturally into context containers:

- HANDOFFS -> `summary_md`, `next_action`, `open_loop`, `session_ids`
- DECISIONS -> linked `decisions` rows plus `evidence` on the relevant container
- GOALS -> `label`, `container_type`, `next_action`, and `open_loop`
- Repo-scoped HANDOFFS -> `code_project` with repo evidence
- Personal/research/general handoffs -> `personal_project`, `research_thread`, `open_floor_conversation`, or `general_activity` without repo evidence

A future migration can scan summarized agent-shared content, assign a deterministic `context_id`, classify type/label/source with confidence and reason, link any source sessions/tasks, and upsert into `context_containers`. Low-confidence items should be kept queryable as `open_floor_conversation` rather than forced into a repo.

## Current slice and remaining work

Implemented now:

- Table/schema bootstrap for `context_containers`
- Context type vocabulary and validation
- MCP functions/registrations for brief, open loops, recent contexts, and resume
- Fixture test showing a non-code conversation is queryable and resumable

Remaining work for full issue completion:

- Automated classifier/extractor from raw sessions and summaries
- Deeper quality metrics that aggregate unknown/general/container coverage alongside repo attribution
- Backfill/migration command for agent-shared HANDOFFS/DECISIONS/GOALS content
- Richer UX examples once automated context creation exists
