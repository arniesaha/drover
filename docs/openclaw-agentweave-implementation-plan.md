# OpenClaw + AgentWeave Implementation Plan

> **For Claude Code / Hermes:** implement this plan task-by-task. Keep the public product boundary from `README.md`: Nexus is a local-first context store and handoff layer, not an agent runtime, tracing dashboard, model router, memory SaaS, or enterprise context platform.

**Goal:** turn the design contracts in `docs/openclaw-adapter-contract.md` and `docs/agentweave-integration.md` into working Nexus parser, schema, ingest, health, and MCP behavior.

**Architecture:** OpenClaw native events and AgentWeave spans remain independent source streams. Nexus normalizes both into local Parquet/DuckDB rows, then links them non-destructively by canonical session UUID, session key, trace/span ids, parent links, and bounded attribution fallbacks. Missing metadata should reduce confidence and surface health warnings; it must not block ingest or rewrite identities.

**Tech stack:** Python, Pydantic models, DuckDB views/macros, PyArrow/Parquet, pytest, existing Nexus CLI/MCP modules.

---

## Source docs to read first

- `docs/openclaw-adapter-contract.md`
- `docs/agentweave-integration.md`
- `docs/openclaw-agentweave-schema-evolution.md`
- `src/nexus/models.py`
- `src/nexus/parsers.py`
- `src/nexus/server/otlp/ingest.py`
- `src/nexus/schema.py`
- `src/nexus/server/quality.py`
- `src/nexus/server/mcp/tools.py`
- Tests: `tests/test_parsers.py`, `tests/test_agentweave_parser.py`, `tests/test_otlp_ingest.py`, `tests/test_otlp_adapter.py`

Related tracking:

- Nexus #106: OpenClaw adapter contract
- Nexus #108: AgentWeave provenance input
- Nexus #102: span metadata completeness
- AgentWeave #187: canonical OpenClaw UUID vs session key
- AgentWeave #216: current OpenClaw bridge APIs
- AgentWeave #217: OpenClaw bridge + Mux routing/context attrs

---

## Acceptance criteria

The implementation is done when all of these hold:

1. OpenClaw parser/adapter preserves canonical session UUID and route/session key as separate values.
2. OpenClaw events carry `raw_data.harness = "openclaw"` and normalized event types while preserving source event names.
3. OpenClaw parent/child session links are captured when available and exposed in a read-oriented view or helper suitable for MCP handoff.
4. AgentWeave span parser extracts new contract fields without breaking old partitions.
5. `session_id` remains the canonical durable session id; `session_key` is stored separately and never overwrites it.
6. Span rows can store `harness`, `session_key`, `cwd`, `repository`, redaction flags, Mux routing facts, and preview truncation metadata when present.
7. Native OpenClaw events and AgentWeave spans link non-destructively by UUID first, then session key, then confidence-scored fallback.
8. Health/quality output reports OpenClaw/AgentWeave freshness, metadata completeness, joinability, and redaction status.
9. Existing tests pass; new tests cover missing metadata, old partition compatibility, OpenClaw UUID/session-key separation, parent links, and Mux attrs.
10. No public docs or test fixtures introduce private hostnames, LAN IPs, secrets, credentials, or enterprise-context positioning.

---

## Task 1: Add implementation fixtures

**Objective:** create small deterministic fixtures that encode the contract before changing parsers.

**Files:**

- Create: `tests/fixtures/openclaw_session_contract.jsonl`
- Create: `tests/fixtures/agentweave_openclaw_trace_contract.json`
- Modify: `tests/test_parsers.py`
- Modify: `tests/test_agentweave_parser.py`

**Fixture requirements:**

`openclaw_session_contract.jsonl` should include:

- one session/lifecycle record with canonical UUID, session key, cwd/workspace/repository/project/topic, version/runtime identity
- one user turn
- one assistant turn
- one tool call or tool result
- one child/subagent lifecycle event with parent session UUID/key and child session UUID/key
- redaction metadata with a preview limit

Use fake but realistic values only, for example:

- session UUID: `018f-openclaw-main-0001`
- session key: `agent:main:main`
- project: `example/nexus-demo`
- cwd: `/tmp/nexus-demo`
- repository: `https://github.com/example/nexus-demo.git`

`agentweave_openclaw_trace_contract.json` should include:

- an OpenTelemetry/Tempo-style trace dict with root agent turn, LLM span, tool span, and optional routing span
- `prov.harness = openclaw`
- `prov.session.id = 018f-openclaw-main-0001`
- `prov.session.key = agent:main:main`
- `prov.parent.session.id` on the child span when applicable
- `prov.cwd`, `prov.repository`, `prov.project`, `prov.task.label`
- `mux.*` or `prov.routing.*` attrs for selected provider/model and route reason
- redaction attrs and prompt/response previews longer than the truncation limit in one field

**Tests to write first:**

- `test_parse_openclaw_contract_preserves_session_uuid_and_key`
- `test_parse_openclaw_contract_sets_harness_and_normalized_type`
- `test_parse_openclaw_contract_preserves_parent_child_links`
- `test_parse_agentweave_openclaw_contract_extracts_harness_session_key_and_routing`
- `test_parse_agentweave_openclaw_contract_marks_preview_truncation`

Expected before implementation: new tests fail because parser/schema fields are missing.

---

## Task 2: Normalize OpenClaw native events

**Objective:** update `parse_openclaw_sessions` so current and future OpenClaw JSONL exports satisfy the adapter contract.

**Files:**

- Modify: `src/nexus/parsers.py`
- Modify: `tests/test_parsers.py`

**Implementation notes:**

Add small helpers rather than a large inline parser:

- `_openclaw_session_identity(data, current_session_id, current_session_key) -> tuple[str, str | None]`
- `_normalize_openclaw_event_type(source_type, data) -> str`
- `_openclaw_raw_data(data, session_state) -> dict`

Preserve compatibility with existing OpenClaw rows:

- Current parser treats `type == "session"` as session metadata and skips creating an event. Keep that behavior unless the source record is explicitly useful as a lifecycle event.
- Existing `event_type == "message"` should continue producing `Message(role=..., content=...)`.
- New normalized event types should be one of the contract vocabulary: `session_start`, `session_end`, `user_turn`, `assistant_turn`, `tool_call`, `tool_result`, `command`, `lifecycle`, `error`, `unknown`.

Session identity rules:

1. Prefer canonical UUID fields: `session_uuid`, `sessionUuid`, `session.id`, `sessionId`, then `id` on session metadata records.
2. Preserve route key fields separately: `session_key`, `sessionKey`, `routeKey`, `channelKey`.
3. If canonical UUID is unavailable, keep current stable session id behavior but set `raw_data.session_uuid_missing = true`.
4. Do not store a route key in `session_id` when a canonical UUID is present.

Raw data should add or preserve:

- `harness = "openclaw"`
- `harness_version`
- `runtime_id`
- `runtime_api`
- `session_uuid`
- `session_key`
- `parent_session_uuid`
- `parent_session_key`
- `child_session_uuid`
- `child_session_key`
- `agent_id` and `agent_type`
- `channel` and `source_surface` with coarse labels only
- `cwd`, `workspace_dir`, `repository`, `project`, `topic`
- `event_name` with the original source event name
- `redaction` object when present
- `provenance.trace_id`, `provenance.span_id`, `provenance.parent_span_id` when present

Run:

```bash
python -m pytest tests/test_parsers.py -q
```

---

## Task 3: Extend AgentWeave span parsing

**Objective:** extract all contract fields from AgentWeave/OTel attrs while preserving old span rows.

**Files:**

- Modify: `src/nexus/parsers.py`
- Modify: `tests/test_agentweave_parser.py`

**Parser additions:**

Add mappings for these string fields when present:

- `prov.harness` or `harness` -> `harness`
- `prov.session.key` -> `session_key`
- `prov.cwd` -> `cwd`
- `prov.repository` -> `repository`
- `prov.git.branch` -> `branch`
- `prov.repo.owner` -> `repo_owner`
- `prov.repo.name` -> `repo_name`
- `prov.routing.provider`, `mux.provider`, or equivalent -> `routing_provider`
- `prov.routing.model`, `mux.model`, or equivalent -> `routing_model`
- `prov.routing.reason`, `mux.reason`, or equivalent -> `routing_reason`
- `redaction.level` -> `redaction_level`
- `redaction.fields` -> `redaction_fields` if representable; otherwise keep in `attributes_json`
- `sensitivity` -> `sensitivity`

Add boolean/int metadata:

- `preview_truncated`: true when any preview was cut by `_truncate`
- `preview_bytes`: configured truncation size

Session rules:

- `session_id` should prefer `session.id` then `prov.session.id`.
- `session_key` should never overwrite `session_id`.
- If only `prov.session.key` exists, leave `session_id` null and populate `session_key`.

Preview handling:

- Keep the current 2 KiB default unless a repo-wide constant already exists.
- Prompt/response/tool previews should be truncated before storage.
- If redaction metadata is missing, do not reject the span; mark metadata as unknown and keep previews bounded.

Run:

```bash
python -m pytest tests/test_agentweave_parser.py -q
```

---

## Task 4: Evolve span storage schema safely

**Objective:** make new span fields durable in Parquet/DuckDB without breaking older partitions.

**Files:**

- Modify: `src/nexus/server/otlp/ingest.py`
- Modify: `src/nexus/schema.py`
- Modify: `tests/test_otlp_ingest.py`
- Modify/create schema-oriented tests if existing tests are insufficient

**Columns to add to `_SPANS_COLUMNS` and Arrow schema:**

- `harness` string
- `session_key` string
- `cwd` string
- `repository` string
- `routing_provider` string
- `routing_model` string
- `routing_reason` string
- `redaction_level` string
- `sensitivity` string
- `preview_truncated` bool
- `preview_bytes` int64
- optional: `link_confidence` string if implemented at ingest time; otherwise keep it for a later linking task

**Compatibility requirements:**

- `read_parquet(..., union_by_name=true)` must continue reading old partitions that lack these columns.
- Seed/empty parquet fixtures must include the new columns if there is a seed writer.
- `_normalize_row` should default missing new fields to `None` or `False` as appropriate.
- Attribute JSON should remain complete enough for future reprocessing.

Run:

```bash
python -m pytest tests/test_otlp_ingest.py tests/test_agentweave_parser.py -q
```

---

## Task 5: Add non-destructive span/session linking helpers

**Objective:** expose joinability between native OpenClaw events and AgentWeave spans without rewriting either stream.

**Files:**

- Modify: `src/nexus/schema.py`
- Add or modify tests for schema/view behavior; if no helper exists, create a focused test module.

**Design:**

Add a read-oriented view or macro, for example `openclaw_span_links` or `span_session_links`, that emits:

- `trace_id`
- `span_id`
- `span_session_id`
- `span_session_key`
- `event_session_id`
- `event_session_key`
- `agent_id`
- `harness`
- `link_method`: `canonical_session_id`, `session_key`, `parent_session_id`, `agent_day_project`, or `unmatched`
- `link_confidence`: `exact`, `strong`, `weak`, `none`

Join order:

1. `spans.session_id = agent_events.session_id` for exact UUID matches.
2. `spans.session_key = agent_events.raw_data.session_key` where both exist.
3. `spans.parent_session_id = agent_events.session_id` for child/subagent context.
4. Project/day fallback only for aggregate project activity, not replay.

Use bounded/date-specific macros where possible. Do not make broad views that join all historical `spans` and `agent_events` if that reintroduces file-descriptor pressure. If a broad view is needed, document that operators should use bounded macros for live diagnostics.

Run:

```bash
python -m pytest tests/test_schema.py tests/test_session_graph.py -q
```

If those files do not exist, create narrow tests around `bootstrap_schema` and a temp parquet/db fixture.

---

## Task 6: Surface health and quality checks

**Objective:** make the contract observable through read-only diagnostics.

**Files:**

- Modify: `src/nexus/server/quality.py`
- Modify: `src/nexus/server/doctor.py` if that is where span health belongs
- Modify related tests, likely `tests/test_quality.py` or create one if absent

Health dimensions:

- recent OpenClaw native events observed
- recent OpenClaw AgentWeave spans observed
- percent of OpenClaw events with canonical session UUID
- percent with separate `session_key`
- percent with parent/child links among subagent/lifecycle events
- percent of AgentWeave spans with `trace_id`, `span_id`, timestamps
- percent with `session_id`, `session_key`, `agent_id`, `harness`
- percent of OpenClaw spans linkable to native events by exact or strong methods
- percent with repo/project/cwd/repository attribution
- percent of previews redacted/truncated within policy
- percent with Mux routing facts when routing spans exist

Output rules:

- Diagnostics must be read-only.
- Missing optional metadata should be warning/degraded, not ingest failure.
- Exact duplicate `dedup_key` or missing required span identity remains critical.

Run:

```bash
python -m pytest tests/test_quality.py tests/test_dogfood_smoke.py -q
```

Adjust file list to existing tests.

---

## Task 7: Add MCP-facing context affordances only if backed by data

**Objective:** expose useful OpenClaw/AgentWeave context through existing Nexus MCP tools without overbuilding a dashboard.

**Files:**

- Modify: `src/nexus/server/mcp/tools.py`
- Modify MCP tests if present

Preferred small changes:

- Include trace/span ids as evidence in handoff/session replay outputs when linked.
- Include `harness`, `session_key`, and `link_confidence` in diagnostic/detail responses.
- Keep cost/model/routing facts as compact evidence, not a full trace UI.

Avoid:

- New dashboard endpoints.
- Unbounded trace tree dumps.
- Calling Mux or AgentWeave live services from MCP tools unless explicitly requested.

Run:

```bash
python -m pytest tests/test_mcp_tools.py tests/test_dogfood_smoke.py -q
```

Adjust file names to existing tests.

---

## Task 8: Full validation and PR hygiene

**Objective:** prove implementation is safe and ready for review.

Run targeted tests first:

```bash
python -m pytest \
  tests/test_parsers.py \
  tests/test_agentweave_parser.py \
  tests/test_otlp_adapter.py \
  tests/test_otlp_ingest.py \
  -q
```

Then run the full suite:

```bash
python -m pytest -q
```

Before pushing:

```bash
git status --short --branch
git diff --stat
```

PR body should include:

- which contract fields are implemented
- which metadata remains dependent on AgentWeave/OpenClaw upstream fixes
- test commands and results
- any intentional follow-ups

---

## Implementation guardrails

- Do not infer secrets or private deployment details into fixtures.
- Do not store credentials, tokens, raw headers, webhook secrets, or private IPs.
- Do not add enterprise/context-layer/product language.
- Do not let session keys overwrite canonical session UUIDs.
- Do not fail ingest just because optional AgentWeave/OpenClaw metadata is missing.
- Do not make broad DuckDB diagnostics that scan all historical partitions when bounded macros would work.
- Prefer additive fields and `union_by_name=true` compatibility over destructive migrations.
- Keep Mux as routing evidence only; Nexus must not route models.
- Keep AgentWeave as provenance/observability; Nexus must not become the tracing dashboard.
