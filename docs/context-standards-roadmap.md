# Drover Context Standards Roadmap

Status: proposed roadmap
Date: 2026-08-05

## Goal

Use external agent-context standards and tools where they sharpen Drover's
local-first cockpit and durable handoff story, without turning the first public
release into a generic ontology platform, hosted agent framework, or enterprise
governance product.

The near-term product promise stays simple:

> Start, watch, steer, and resume local CLI agent sessions with context intact.

Standards should support that promise by making context easier to trust,
inspect, export, and evaluate. They should not become the main feature.

## Research Inputs

Reviewed references:

- OKF v0.2: markdown plus YAML-frontmatter knowledge bundles with first-class
  provenance, trust, lifecycle, freshness, and attested computation fields.
  Reference: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
- Agent Behavior: repo-local `.agents/behaviors/<name>/BEHAVIOR.md` specs for
  describing recurring agent conduct, aimed at trace review, eval design, and
  prompt alignment rather than runtime instruction loading.
  Reference: https://www.agentbehavior.dev/
- Graphify: local code/docs/media graph extraction that outputs `graph.json`,
  wiki markdown, graph reports, path/query/explain surfaces, and edge evidence
  labels such as extracted, inferred, or ambiguous.
  Reference: https://github.com/safishamsi/graphify
- CodeGraph: local per-repo semantic code graph for agents, with MCP wiring,
  auto-sync on file changes, supported agents including Claude Code, Codex,
  Gemini, Cursor, OpenCode, Hermes Agent, Antigravity, and Kiro, and benchmark
  claims around fewer tool calls and less token spend for codebase questions.
  Reference: https://github.com/colbymchenry/codegraph
- MCP: the standard serving surface Drover already uses for tools and should
  use for context resources and prompts as the context layer matures.
  Reference: https://modelcontextprotocol.io/specification/2026-07-28
- OpenTelemetry GenAI semantic conventions: the right direction for span,
  event, metric, and provider/tool telemetry vocabulary.
  Reference: https://github.com/open-telemetry/semantic-conventions-genai
- W3C PROV: useful background vocabulary for entity/activity/agent provenance,
  derivation, generation, versioning, and reproducibility, but heavier than
  Drover needs in the first release.
  Reference: https://www.w3.org/TR/prov-overview/
- A2A: relevant later if Drover exposes agents to other agents as peers. It is
  not needed for the first public release because Drover's near-term surface is
  personal CLI harness control plus context recall, not cross-vendor agent
  collaboration.
  Reference: https://github.com/a2aproject/A2A

## Fit To Drover

### OKF

Verdict: adapt for portable curated context.

OKF fits Drover's context store better than a bespoke markdown convention. It
is human-readable, git-diffable, agent-parseable, and gives Drover a vocabulary
for the questions every handoff needs to answer:

- What source sessions, spans, files, or commits produced this context?
- Which agent or process generated it?
- Has a person or deterministic process verified it?
- Is it stale?
- Is it draft, stable, or deprecated?

Drover should not expose OKF as the core product in the first release. Instead,
the context store should be able to export and import selected curated records,
project briefs, decisions, behavior specs, and runbooks as OKF-shaped bundles.

### Agent Behavior

Verdict: adopt as a repo-local review/evals artifact, not as a runtime memory
format.

Agent Behavior complements Drover's trace lake. It defines what good recurring
agent conduct looks like, while Drover records what actually happened. The fit
is strongest for public-release dogfooding:

- behavior specs for cost-sensitive actions, production changes, privacy, and
  evidence-backed handoffs
- trace review against those specs
- future eval cases generated from behavior specs

Drover should not inject every behavior spec into session prompts by default.
The useful surface is discovery, indexing, review, and optional context packs
when a behavior is directly relevant.

### Graphify

Verdict: spike after the first public release; optionally document as an
inspiration now.

Graphify addresses a real Drover gap: sessions and summaries know files were
touched, but they do not know the structure of a codebase. A repo graph could
help a resumed agent answer:

- Which files, symbols, docs, and concepts are central?
- What path connects a decision to the code it affected?
- Which relationships are extracted from code versus inferred by an LLM?

For the first public release, keep this out of the product surface. Afterward,
run a local spike that imports Graphify output into Drover as a secondary
context source.

### CodeGraph

Verdict: strong post-public spike candidate for code intelligence; do not
bundle for the first public release.

CodeGraph is closer to Drover's immediate resumed-agent use case than a general
ontology system. It builds a local code graph, exposes it to agents through MCP,
auto-syncs on project changes, and is explicitly positioned around reducing
file-by-file discovery during coding sessions. That overlaps with Drover's
context-store question:

- Can a resumed session know relevant symbols, call paths, and blast radius
  before spending turns on `rg`, `find`, and file reads?
- Can Drover attach code-graph evidence to a handoff without owning a graph
  parser?
- Can the iOS/session UI show "affected code paths" for recent work?

The fit is promising, but first-public Drover should avoid taking a dependency
on another agent installer or shipping a second MCP server as part of the
default story. The practical path is an opt-in spike: index this repo with
CodeGraph, compare resumed-agent behavior against Drover's existing search and
replay tools, and decide whether Drover should ingest selected code-graph facts
or simply document CodeGraph as a complementary local MCP server.

### MCP

Verdict: keep as the integration boundary.

Drover should continue exposing agent-facing capabilities through MCP tools.
As the context layer matures, add MCP resources and prompts where they reduce
tool-call ceremony:

- resources for project briefs, recent contexts, behavior specs, and OKF bundle
  entries
- prompts for "resume this project", "review this trace against behavior
  specs", and "prepare a handoff"
- tools for search, replay, active handoff, context quality, and context export

### OpenTelemetry GenAI

Verdict: continue aligning span ingestion and derived context with OTel GenAI.

Drover already imports AgentWeave/OTLP spans. Public-release work should keep
that path stable and avoid inventing incompatible telemetry names when a GenAI
semantic convention exists. The priority is interoperability and trace
legibility, not becoming a tracing vendor.

### W3C PROV

Verdict: borrow vocabulary, skip formal PROV serialization for now.

Drover already needs provenance. PROV's entity/activity/agent model is a useful
mental model for generated summaries, decisions, context exports, and replay
receipts. But formal RDF/OWL/XML PROV output is not a first-public-release
requirement.

### A2A

Verdict: defer.

A2A is about opaque agentic applications discovering capabilities and
collaborating over long-running tasks. Drover's first public release should not
present itself as an A2A agent server. A2A can return later if Drover needs to
let external agents discover and collaborate with a user's local harness fleet.

## Recommended Roadmap

### First Public Release

Do these:

- Keep the context model local-first: Parquet facts plus DuckDB serving tables.
- Keep the primary user story focused on command, memory, and handoff.
- Stabilize existing MCP tools under Drover naming with Nexus aliases only
  where migration requires them.
- Document the context artifacts Drover already has: sessions, summaries,
  project briefs, active briefs, decisions, context containers, embeddings,
  spans, and quality checks.
- Add an OKF assessment doc for issue #15, updated from OKF v0.1 to OKF v0.2.
  The assessment should classify each artifact as adopt, adapt, skip, or defer.
- Add a small `.agents/behaviors/` seed only if it directly improves public
  quality:
  - evidence-backed handoffs
  - production-environment changes
  - privacy/redaction boundaries
  - cost-sensitive actions
- Surface behavior specs as documentation/eval inputs, not as automatic prompt
  injection.
- Keep OpenTelemetry/AgentWeave span compatibility in the public story as
  provenance and observability input.

Avoid in the first public release:

- A general ontology UI.
- A graph database dependency.
- Graphify ingestion as a first-class feature.
- CodeGraph ingestion or bundled CodeGraph installation as a first-class
  feature.
- Formal PROV serialization.
- A2A server/client support.
- Automatic skill or behavior installation across agents.
- Claims that Drover is an enterprise governance, eval, or compliance platform.

### Soon After Public Release

Build these as separate, opt-in slices:

1. OKF export/import for curated context.
   - Export project briefs, decisions, runbooks, behavior specs, and selected
     context containers as OKF v0.2 markdown.
   - Store source refs back to Drover sessions, spans, files, and commits.
   - Add quality warnings for stale or unverified exported concepts.

2. Behavior-spec indexing and trace review.
   - Discover `.agents/behaviors/*/BEHAVIOR.md`.
   - Store specs as curated context records.
   - Add a read-only review tool that compares a session replay against one or
     more behavior specs.

3. Minimal context graph.
   - Add relational concept and edge tables in DuckDB, not a new graph server.
   - Lift only high-value nodes first: project, task, session, summary,
     decision, file, behavior spec, OKF concept, span.
   - Store evidence and confidence on edges.

4. Code graph spike.
   - Run CodeGraph and Graphify separately on one Drover-sized repo.
   - Compare MCP query ergonomics, graph freshness, evidence quality, and
     resumed-agent file crawling.
   - For Graphify, import `graph.json` as secondary context.
   - For CodeGraph, test direct MCP use first; only ingest selected facts into
     Drover if they improve handoff quality without duplicating CodeGraph.
   - Keep both optional unless one clearly reduces resumed-agent grunt work.

### Later

Consider these only after the above has proven useful:

- A2A discovery or agent cards for exposing local harness capabilities.
- PROV export for users who need formal provenance interchange.
- Rich ontology browsing in the UI.
- Behavior-derived eval generation and scoring.
- Skill/behavior recommendation loops with explicit user approval.

## Proposed Context Graph Slice

If Drover adds ontology-shaped context, keep it narrow and relational:

```text
context_concepts
  concept_id
  concept_type       -- project | task | session | decision | file | span | behavior | okf_concept | graph_node
  title
  description
  resource
  status
  trust_tier
  stale_after
  generated_by
  generated_at
  verified_by
  verified_at
  content_hash
  metadata_json

context_edges
  edge_id
  source_concept_id
  target_concept_id
  edge_type          -- summarizes | cites | touches | decided_in | derived_from | related_to | depends_on
  evidence_kind      -- exact | extracted | inferred | ambiguous
  confidence
  source_refs        -- session ids, span ids, file paths, commits, OKF paths
  created_at
```

This is enough for "why is this context being shown?" and "where did it come
from?" without dragging in RDF, SPARQL, or a graph database.

## Public Positioning Boundary

Public wording should say:

> Drover is a local-first cockpit and durable context store for personal CLI
> agent fleets.

Public wording should not say:

> Drover is an ontology platform, agent governance suite, agent eval platform,
> tracing vendor, or multi-agent enterprise fabric.

The standards story should be practical:

- MCP is how agents ask Drover for context.
- OTel/AgentWeave spans are provenance inputs.
- OKF is a future portable export/import shape for curated context.
- Agent Behavior is a future review/evals companion for traces.
- Graphify and CodeGraph are promising for codebase structure, but remain
  post-release spikes until they prove handoff value.
