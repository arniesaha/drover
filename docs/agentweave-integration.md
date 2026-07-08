# AgentWeave provenance input

Tracking: Nexus [#108](https://github.com/arniesaha/nexus/issues/108), Nexus
[#102](https://github.com/arniesaha/nexus/issues/102), AgentWeave
[#187](https://github.com/arniesaha/agentweave/issues/187),
[#216](https://github.com/arniesaha/agentweave/issues/216), and
[#217](https://github.com/arniesaha/agentweave/issues/217).

Implementation handoff docs:

- [`openclaw-agentweave-implementation-plan.md`](openclaw-agentweave-implementation-plan.md)
- [`openclaw-agentweave-schema-evolution.md`](openclaw-agentweave-schema-evolution.md)
- [`openclaw-agentweave-validation-matrix.md`](openclaw-agentweave-validation-matrix.md)

## Role separation

AgentWeave is the provenance and observability layer. It records execution
facts: spans, traces, delegation, model calls, token/cost/cache usage, tool
calls, routing decisions, and OpenTelemetry-compatible metadata.

Nexus is the durable local context store and handoff layer. It imports,
archives, links, summarizes, embeds, and recalls AgentWeave facts alongside
native harness session events. Nexus should not become a tracing dashboard,
eval suite, model router, or replacement for Tempo, Grafana, Langfuse, Phoenix,
or Mux.

Practical boundaries:

- Use AgentWeave/OTel for live provenance capture and trace semantics.
- Use Tempo/Grafana or similar tools for trace visualization and operational
  dashboards.
- Use Mux for model routing and policy. Nexus stores routing facts as evidence
  for later recall and cost analysis; it does not choose routes.
- Use Nexus for session archive, context recall, handoff briefs, project
  activity, replay, and durable provenance search.

## Import modes

Nexus should support three read-oriented ingestion modes. Deployments can use
one or more.

### Direct local OTLP ingest

AgentWeave exports OTLP spans directly to the local Nexus server:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
nexus-server run
```

The existing `nexus-server` OTLP receiver writes normalized rows into
`~/.nexus/parquet/spans/`. This is the lowest-latency local path.

### Pull from Tempo

Nexus can periodically pull a bounded window from Tempo and persist immutable
snapshots before normalizing spans. Tempo remains an upstream query source, not
the long-term context archive.

Localhost-first example:

```bash
export TEMPO_BASE=http://localhost:3200
curl "$TEMPO_BASE/api/search?q={resource.service.name=\"agentweave-proxy\"}&limit=100&start=$START&end=$END"
curl "$TEMPO_BASE/api/traces/$TRACE_ID"
```

Use overlapping windows, for example re-read the last 15 minutes on every run,
and dedupe by `trace_id + span_id`.

Suggested raw layout:

```text
~/.nexus/raw_objects/agentweave/tempo/
└── dt=YYYY-MM-DD/
    ├── search-window-<start>-<end>.json
    └── traces/
        └── <trace_id>.json
```

### Direct file/import API

For tests, air-gapped machines, and one-shot migrations, a direct importer can
read AgentWeave/OTel JSON and write the same normalized span rows. This path
should share the same parser and validation rules as the OTLP and Tempo paths.

## Normalized span fields

Minimum durable fields for each span row:

- Identity: `trace_id`, `span_id`, `parent_span_id`, `name`, `service_name`.
- Time: `start_time`, `end_time`, `duration_ms`.
- Harness/session: `harness`, `session_id`, `parent_session_id`, `session_key`.
- Agent: `agent_id`, `agent_type`, `agent_model`.
- Activity: `activity_type`, `task_label`, `status_code`, `status_message`.
- Attribution: `project`, `cwd`, `repository`, `repo_owner`, `repo_name`,
  `branch` when safely available.
- LLM/model: `llm_provider`, `llm_model`, `gen_ai.*` attributes.
- Usage: `prompt_tokens`, `completion_tokens`, `total_tokens`,
  `cache_read_tokens`, `cache_write_tokens`, `cost_usd`, latency fields.
- Routing evidence: route/provider/model selected, policy name, fallback reason,
  and Mux decision metadata when present.
- Previews: redacted/truncated prompt, response, tool input, and tool output
  previews.
- Linkage: `raw_object_uri`, `attributes_json`, and source export metadata.

Recommended AgentWeave/OpenTelemetry attributes:

- `prov.activity.type`: `llm_call`, `tool_call`, `agent_turn`, `routing`, etc.
- `prov.agent.id`, `prov.agent.type`, `prov.agent.model`.
- `session.id`, `prov.session.id`, `prov.parent.session.id`.
- `prov.session.key` for OpenClaw route/session key when distinct from UUID.
- `prov.harness` or `harness`, with `openclaw` for OpenClaw-origin spans.
- `prov.project`, `prov.cwd`, `prov.repository`, `prov.task.label`.
- `prov.llm.provider`, `prov.llm.model`, token, cache, and cost attrs.
- `gen_ai.*` semantic convention attributes.
- Mux routing attrs as facts, for example selected model/provider and fallback
  reason.

## Link to native harness sessions

AgentWeave spans are provenance facts, not the only session source of truth.
Nexus should link spans to native harness events where stable IDs exist and
keep both sources independently useful when links are missing.

Preferred link order:

1. Exact canonical session id: `spans.session_id = agent_events.session_id`.
2. OpenClaw UUID in `prov.session.id` once AgentWeave #187 is resolved.
3. OpenClaw `session_key` / `prov.session.key` for bridge data emitted before
   canonical UUID support.
4. Parent-child links: `parent_session_id`, trace parent/child spans, and
   native OpenClaw subagent session links.
5. Attribution fallback for aggregates only: `(agent_id, date) -> dominant
   repo/project` when exact session links are absent.

Linking must be non-destructive:

- Missing spans must not prevent native replay or handoff.
- Missing native events must not discard valid token/cost/model facts.
- Partial metadata should produce lower-confidence links, not rewritten session
  identities.
- If `session_id` and `session_key` disagree, keep both and mark the link as
  provisional.

## Redaction and size limits

Nexus should only embed or summarize previews that have already passed a
redaction policy. Importers should enforce local limits even if the upstream
span is larger.

Recommended defaults:

- Prompt preview: max 2 KiB after redaction.
- Response preview: max 2 KiB after redaction.
- Tool input/output preview: max 2 KiB each after redaction.
- Attributes JSON: keep full non-secret scalar metadata; omit or hash known
  secret fields.
- Raw trace snapshots: optional and stored under `raw_objects` with retention
  policy separate from Parquet rows.

Recommended flags:

- `redaction.level`: `none`, `preview`, `redacted`, or `sensitive`.
- `redaction.fields`: fields removed or transformed.
- `preview_truncated`: true when any preview hit a size limit.
- `sensitivity`: optional tags such as `secret`, `credential`, `private_path`,
  or `customer_data`.

If a span lacks redaction metadata, Nexus should treat free-text previews as
untrusted: truncate aggressively, avoid embeddings by default, and keep the
span metadata available for trace/cost linkage.

## Graceful handling of missing metadata

AgentWeave spans may arrive before bridge/runtime issues are resolved, or from
non-OpenClaw systems with different conventions. Importers should:

- accept spans with missing `session_id` if `trace_id` and `span_id` are valid,
- store unknown fields in `attributes_json`,
- use `harness = unknown` when no harness attr is present,
- preserve `session_key` separately from `session_id`,
- leave repo/project fields null rather than guessing exact attribution,
- mark link confidence when using fallbacks, and
- surface health warnings instead of failing the ingest pipeline.

## Derived context outputs

After import, Nexus can derive context artifacts:

- session summaries linked to native sessions and provenance facts,
- project briefs that include recent model/tool/cost evidence,
- span embeddings over safe previews and task labels,
- decision records extracted from span trees and native turns,
- handoff responses that cite trace/span ids as source evidence.

These are derived context products. The raw spans and native session events
remain the references.

## Health checks

Read-only checks should answer:

- Have AgentWeave spans arrived in the last configured window?
- What percent have `trace_id`, `span_id`, `parent_span_id`, and timestamps?
- What percent have `session_id`, `parent_session_id`, and `session_key`?
- For OpenClaw spans, is the canonical UUID distinct from the session key?
- What percent link to native harness sessions?
- What percent include repo/project/cwd/repository attribution?
- Are prompt/response previews redacted and within size limits?
- Are Mux routing facts present when routing occurred?

The runtime audit should classify recent span metadata without rewriting native
session truth:

- `linked_openclaw_spans`: OpenClaw/AgentWeave spans matched native OpenClaw
  session events by exact `session_id`, unique `session_key`, or another
  explicit link. Missing repo/project on the span is acceptable because Nexus
  can use the native session as the durable context source.
- `provenance_only`: routing/observability spans such as Mux decisions that are
  valid provenance but intentionally lack enough session/project metadata for
  recall attribution.
- `needs_attribution`: spans that are neither safely linked nor sufficiently
  attributed by explicit repo/project/cwd/repository metadata. These should stay
  queryable as spans but must not corrupt native session context by guessing.

This design gives Nexus #102 a concrete target for span metadata completeness
without requiring Nexus to be the observability UI.

## Implementation notes

- Keep the existing OTLP receiver as the direct ingest path.
- Reuse the AgentWeave parser for Tempo pulls and direct file imports.
- Add fields in Parquet with `union_by_name=true` compatibility so older span
  partitions continue to read.
- Add parser/ingest tests for missing or partial metadata before relying on new
  fields in MCP tools.
- Keep public docs localhost-first and omit private hostnames, LAN IPs, tokens,
  or deployment-specific paths.
