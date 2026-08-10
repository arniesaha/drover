# Integrations

Drover separates ingestion adapters from its normalized context model. Source
formats can evolve without becoming the public storage contract.

## Agent CLIs

The collector recognizes local session records from Claude Code, Codex,
Antigravity (agy), OpenClaw, Hermes, and compatible tools. Parsers preserve source-native
identifiers and raw metadata while producing normalized agent events.

Repository identity is taken from source metadata or the local Git checkout.
For paths collected on a different machine, add an explicit prefix map instead
of relying on machine-specific defaults:

```bash
export DROVER_REPO_ROOTS_JSON='{ "/srv/projects/example": "acme/example" }'
```

Claude Code encodes project paths into directory names and cannot distinguish
every literal hyphen from a path separator. Add ambiguous prefixes explicitly:

```bash
export DROVER_CLAUDE_CWD_MAP='{ "-srv-projects-agent-tools": "/srv/projects/agent-tools" }'
```

`DROVER_GENERAL_WORKSPACE_ROOTS` accepts an OS-path-separator-delimited list of
exact directories that should be classified as non-project workspace activity.
All three settings are optional and empty by default.

## Adoption Registry

The quality and observatory views can track whether each agent runtime emits
events, has Drover MCP configured, and has the Drover skill installed. This is
operator-specific, so the registry is disabled by default and produces no
missing-agent warnings until configured.

Set `DROVER_AGENT_ADOPTION_JSON` to a JSON array of runtime records:

```bash
export DROVER_AGENT_ADOPTION_JSON='[
  {
    "runtime": "claude-workstations",
    "agent_id_patterns": ["claude-*"],
    "emits_to_drover": true,
    "mcp_configured": true,
    "drover_skill_configured": true,
    "status": "active",
    "smoke_check": "drover_data_quality and drover_recent_sessions"
  }
]'
```

The three capability fields are required booleans. `runtime` and at least one
`agent_id_patterns` glob are also required. Invalid configuration is surfaced
as an adoption-category warning without stopping the server.

## AgentWeave And OpenTelemetry

Drover accepts OTLP spans and retains trace, span, session, model, token, cost,
cache, delegation, and routing provenance when present. AgentWeave is one
producer of those spans; Drover remains the durable local context and recall
layer rather than a replacement tracing UI.

Historical spans may contain `nexus.*` attributes. Drover reads those values
for compatibility. New integrations should emit Drover naming and stable
OpenTelemetry or W3C provenance fields where possible.

## MCP

`drover-server` exposes a streamable HTTP MCP endpoint at `/mcp`, normally on
port `7077`. Its tools use the `drover_*` prefix and cover fleet status,
handoff, recent sessions, replay, search, recall, project briefs, and data
quality.

Register it in Codex as `drover`:

```bash
codex mcp add drover --url http://127.0.0.1:7077/mcp
```

Use `uv run drover-server mcp tools` to inspect the live server surface.

## Context Backends

Summaries and briefs can use Anthropic credentials or a configured local
Ollama-compatible backend. Embeddings can use an OpenAI-compatible endpoint or
configured Ollama backend. These integrations are optional; durable ingest and
the command plane continue to work without them.

External model providers receive the content submitted to their configured
workers. Choose local backends when that data must remain entirely on your
hardware.
