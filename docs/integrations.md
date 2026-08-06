# Integrations

Drover separates ingestion adapters from its normalized context model. Source
formats can evolve without becoming the public storage contract.

## Agent CLIs

The collector recognizes local session records from Claude Code, Codex,
Gemini, OpenClaw, Hermes, and compatible tools. Parsers preserve source-native
identifiers and raw metadata while producing normalized agent events.

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
