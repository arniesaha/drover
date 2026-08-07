# Drover, AgentWeave, and Langfuse Positioning

## North star

> AgentWeave captures provenance. Langfuse evaluates behavior. Drover remembers
> context and carries work forward.

## Responsibility split

### AgentWeave: execution provenance

AgentWeave answers: **What happened during execution?** It owns spans, traces,
delegation graphs, model and tool instrumentation, token/cost facts, and
OpenTelemetry-compatible provenance.

### Langfuse: observability and evaluation

Langfuse answers: **How good was the run?** It owns trace inspection, datasets,
eval scores, prompt/model comparisons, regression tracking, and judge workflows.

### Drover: command, context, and continuation

Drover answers: **What is running, and what should the next agent know?** It owns
session control, the local session archive, replay and search, summaries,
project briefs, active handoffs, and context bundles for downstream agents.

## Boundary principle

Drover may store trace IDs, span IDs, evaluation IDs, scores, and selected safe
provenance facts as evidence for recall. It should not become a trace dashboard,
an eval platform, a model router, or an enterprise control plane.

Good: “Drover links this handoff to AgentWeave trace and span evidence.”

Bad: “Drover replaces tracing, evaluations, routing, and monitoring.”

## Evaluation loop

1. Capture real handoff episodes: continuation request, target repository or
   task, context returned, replay evidence used, and downstream result.
2. Export curated episodes to an evaluation dataset with session, trace, and
   context-bundle identifiers.
3. Score handoff sufficiency, grounding, freshness, precision, actionability,
   privacy, and absence of hallucinated project state.
4. Feed evaluation identifiers and failure categories back as annotations that
   improve summarization and retrieval—not as a second dashboard.

## Public language

Prefer: local-first command and context layer, durable archive of agent work,
MCP recall and project handoffs, observability-aware, and context for
continuation.

Avoid: observability platform, trace dashboard, Langfuse alternative, eval
platform, model router, enterprise context layer, or hosted memory SaaS.

Historical `nexus.*` telemetry is a compatibility detail, not a second public
product name.
