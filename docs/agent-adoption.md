# Nexus Agent Adoption

Status: active dogfood  
Tracking issue: [#179](https://github.com/arniesaha/nexus/issues/179)

## Adoption Contract

An agent is Nexus-ready when all of these are true:

1. It emits events or spans with stable session identity.
2. Its events include enough attribution to map work to a repository or project
   when that is appropriate.
3. It can call the Nexus MCP server.
4. It has a local instruction or skill telling it when to call Nexus.
5. It passes a read-only smoke check that proves handoff, replay, project brief,
   and data-quality tools work from that agent environment.

## Current Matrix

| Agent / runtime | Emits to Nexus | MCP configured | Nexus skill/instructions | Status | Next check |
|---|---:|---:|---:|---|---|
| Mac Mini Claude/Max | Yes | Yes | Yes | Active dogfood | Smoke `nexus_recent_sessions` and `nexus_session_replay` from Max |
| NAS OpenClaw main | Yes | Yes | Yes | Active dogfood | Smoke `nexus_handoff`, `nexus_project_brief`, and `nexus_data_quality` |
| Paperclip agents | Yes/partial | No | Yes/partial | Needs MCP rollout | Add Nexus handoff/quality checks to completion contract |
| Codex CLI sessions | Yes/partial | No | Yes in this workspace | Needs validation | Smoke `nexus_handoff` and `nexus_data_quality` |
| Work MacBook Claude | Yes via shipper | No/unknown | Unknown | Data source | Confirm MCP config and skill install |
| Future agents | No | No | No | Not onboarded | Use checklist below |

## MCP Tools Agents Should Know

Core handoff:

- `nexus_data_quality`
- `nexus_handoff`
- `nexus_recent_sessions`
- `nexus_session_summary`
- `nexus_session_replay`

Project context:

- `nexus_project_brief`
- `nexus_project_activity`
- `nexus_files_touched`
- `nexus_task_status`

Live work:

- `nexus_active_handoff`
- `nexus_active_sessions`
- `nexus_fleet_status`
- `nexus_pipeline_observatory`
- `nexus_session_close`

## Smoke Checklist

For each agent runtime:

1. Confirm it can reach the MCP endpoint.
2. Call `nexus_data_quality(hours=24)` and record status/score.
3. Call `nexus_recent_sessions(repo_owner="arniesaha", repo_name="nexus")`.
4. Pick one recent session and call `nexus_session_replay`.
5. Call `nexus_project_brief(repo_owner="arniesaha", repo_name="nexus")`.
6. Confirm the agent's own latest activity appears in Nexus after a new turn.
7. Confirm repo/project attribution is present unless the activity is intentionally
   general workspace activity.
8. Call `nexus_pipeline_observatory(max_artifacts=5, max_projects=5)` and confirm
   saved summaries, briefs, and project readiness are visible.

## Quality Signal

`nexus-server quality --json` includes an `agent_adoption` category. It annotates
this rollout matrix with observed project-event volume from the runtime audit
and warns when high-volume project agents are not fully Nexus-ready.

Current built-in runtime groups:

- `mac-mini-max`
- `openclaw-main`
- `paperclip-agents`
- `codex-cli`
- `work-macbook-claude`

Prometheus/Grafana gauges:

- `nexus_agent_adoption_ready{runtime,status}`
- `nexus_agent_adoption_observed_events{runtime}`
- `nexus_agent_adoption_unmatched_high_volume_agents`

## Skill / Instruction Requirements

Every participating agent should have a short rule like:

> Before starting non-trivial Nexus, Paperclip, AgentWeave, or long-running
> coding work, check Nexus for recent project context and data quality. At
> completion, ensure the session is summarized or closable, and include durable
> evidence links in the relevant issue.

For Paperclip agents, this should be part of the completion contract: an issue
that affects Nexus reliability is not done until the worker includes a quality
snapshot or explains why it is not relevant.

## Open Questions

- Should Nexus expose an MCP discovery endpoint that lists available tools and
  recommended prompts for each agent?
- Should agent capability inventory live in DuckDB, a YAML file, or both?
- Which parts of this matrix are safe for open source, and which are private
  dogfood configuration?
