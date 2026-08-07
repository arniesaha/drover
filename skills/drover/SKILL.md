---
name: drover
description: Use when an agent needs to resume work, recall prior sessions, search project history, or create a handoff across local or personal agent harnesses.
---

# Drover

## Overview

Drover is the local-first command, context, and handoff layer for personal and
open-source agent harnesses. Use it to ground continuation work in recorded
sessions rather than asking the user to reconstruct prior context.

## Recall workflow

Use the MCP endpoint before direct SQL. It normally runs at
`http://127.0.0.1:7077/mcp` and exposes `drover_*` tools.

| Need | Preferred tool |
| --- | --- |
| Resume with one bounded context bundle | `drover_resume_context` |
| Summarize current repository state | `drover_project_brief` |
| Continue a repository, task, or branch | `drover_handoff` |
| Find recent candidate sessions | `drover_recent_sessions` |
| Replay one session chronologically | `drover_session_replay` |
| Search event text or metadata | `drover_search` |

Start with the narrowest repository/task scope the user supplied. Treat results
as historical evidence: verify drift-prone code, deployment, and GitHub state
against the live system before changing it.

If MCP is unavailable and a direct data-quality investigation is required, use
`drover-local-lakehouse`. Ordinary recall does not justify direct database
access.

## Product boundary

Drover owns session control, a durable local archive, replay/search, summaries,
project briefs, and handoff bundles. AgentWeave may supply provenance; tools
such as Langfuse may evaluate runs. Drover can retain their identifiers as
evidence without becoming a tracing dashboard, eval platform, model router, or
hosted memory service.

See `references/drover-agentweave-langfuse-positioning.md` when explaining this
boundary publicly.

## Safety and compatibility

- Prefer read-only recall and diagnostics. Do not restart services, stop
  shippers, run backfills, or retry derived-data jobs for routine recall.
- Never print credentials or raw secret-bearing traces.
- Current local state lives under `~/.drover`; the live database is normally
  `~/.drover/drover.duckdb`.
- Do not use retired GCP or BigQuery backends for current queries.
- Historical `nexus.*` telemetry, `~/.nexus` imports, and `nexus_*` aliases are
  compatibility inputs only. New guidance, calls, and integrations use Drover.
- Use canonical event identity fields such as `dedup_key`; do not infer missing
  repository attribution from weak evidence.

## Common mistakes

| Mistake | Correction |
| --- | --- |
| Calling `nexus_*` tools | Call the equivalent `drover_*` tool. |
| Treating a brief as current deployment truth | Verify live state before acting. |
| Opening DuckDB for a normal handoff | Use MCP first. |
| Restarting Drover because recall is incomplete | Diagnose the specific surface first. |
