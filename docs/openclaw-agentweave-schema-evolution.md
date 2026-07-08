# OpenClaw + AgentWeave Schema Evolution Design

Tracking: Nexus [#106](https://github.com/arniesaha/nexus/issues/106), Nexus [#108](https://github.com/arniesaha/nexus/issues/108), Nexus [#102](https://github.com/arniesaha/nexus/issues/102).

This document is the implementation-facing schema companion to:

- `docs/openclaw-adapter-contract.md`
- `docs/agentweave-integration.md`
- `docs/openclaw-agentweave-implementation-plan.md`

It defines how to persist OpenClaw adapter fields and AgentWeave provenance fields without turning Nexus into a runtime, router, tracing dashboard, or enterprise context platform.

---

## Schema principles

1. **Additive only:** new fields are appended to Parquet rows and views. Older partitions must remain readable with `union_by_name=true`.
2. **Canonical identity is separate from routing identity:** `session_id` is the durable/canonical session UUID when available; `session_key` is the OpenClaw route/session key.
3. **Native events and spans stay independently useful:** link views may join them, but ingest must not require both streams.
4. **Confidence over guessing:** fallbacks should carry `link_method`/`link_confidence`; they should not rewrite stored source ids.
5. **Bounded diagnostics:** live health checks should prefer partition/date-bounded macros and avoid broad historical scans.
6. **Safe previews only:** prompt/response/tool previews are redacted/truncated before embedding or summarization.

---

## Current storage surfaces

### `agent_events` parquet/view

Current `AgentEvent` fields:

- `id`
- `session_id`
- `timestamp`
- `agent_id`
- `event_type`
- `message`
- `tool_calls`
- `tool_result`
- `token_usage`
- `cost_usd`
- `raw_data`

OpenClaw-specific contract fields should initially live in `raw_data` to avoid a large agent-event schema migration. First-class columns can be added later only for fields that are frequently queried.

### `spans` parquet/view

Current span columns include:

- trace identity: `trace_id`, `span_id`, `parent_span_id`
- time: `start_time`, `end_time`, `duration_ms`, partition `date`
- session/agent: `session_id`, `parent_session_id`, `agent_id`, `agent_type`, `agent_model`
- activity: `activity_type`, `task_label`, status-ish fields
- attribution: `project`, `repo_owner`, `repo_name`, `branch`
- model/usage: `llm_provider`, `llm_model`, tokens, cache tokens, `cost_usd`
- previews: `prompt_preview`, `response_preview`
- raw metadata: `attributes_json`, `raw_object_uri`, `dedup_key`

Span schema should get additive columns for the AgentWeave/OpenClaw contract.

---

## OpenClaw native event `raw_data` contract

`parse_openclaw_sessions` should preserve or add these fields in `raw_data`:

| Field | Type | Required? | Notes |
|---|---:|---:|---|
| `harness` | string | yes | Always `openclaw` for OpenClaw-derived native events. |
| `harness_version` | string | no | OpenClaw runtime/package version if available. |
| `runtime_id` | string | no | Stable install/runtime id, not a private hostname unless opted in. |
| `runtime_api` | string | no | Adapter/API version, e.g. `diagnostic-events/v1`. |
| `session_uuid` | string | yes when known | Mirrors top-level `session_id` when canonical UUID is known. |
| `session_uuid_missing` | bool | conditional | True when route key/legacy id is all the source has. |
| `session_key` | string | no | OpenClaw route/session key. Never overwrite `session_id`. |
| `parent_session_uuid` | string | no | Durable parent link. |
| `parent_session_key` | string | no | Parent route key. |
| `child_session_uuid` | string | no | Child/subagent lifecycle link. |
| `child_session_key` | string | no | Child route key. |
| `agent_id` | string | no | OpenClaw agent id inside runtime. Top-level `agent_id` remains Nexus source id unless explicitly changed. |
| `agent_type` | string | no | `primary`, `subagent`, `tool`, or `unknown`. |
| `channel` | string | no | Coarse label only: `terminal`, `web`, `api`, `webhook`, `unknown`. |
| `source_surface` | string | no | `cli`, `plugin`, `hook`, `scheduler`, `unknown`. |
| `cwd` | string | no | Used for local attribution; may be redacted if private. |
| `workspace_dir` | string | no | OpenClaw workspace root. |
| `repository` | string | no | Remote URL or `owner/repo`. |
| `project` | string | no | Stable project key, usually `owner/repo`. |
| `topic` | string | no | Human task label/thread title. |
| `event_name` | string | yes | Source OpenClaw event name/type before normalization. |
| `redaction` | object | no | Redaction metadata. |
| `sensitivity` | array/string | no | Tags such as `secret`, `private_path`, `unknown`. |
| `provenance` | object | no | `trace_id`, `span_id`, `parent_span_id`, `source`. |

Keep `raw_data` as the compatibility buffer. MCP tools and summaries may read from it, but tests should assert the core fields above.

---

## Span columns to add

Add these fields to `_SPANS_COLUMNS`, `_coerce_to_arrow`, and any seed/fixture writers:

| Column | Type | Source attrs | Default | Notes |
|---|---:|---|---|---|
| `harness` | string | `prov.harness`, `harness` | null/`unknown` in diagnostics | `openclaw`, `hermes`, `claude`, etc. |
| `session_key` | string | `prov.session.key` | null | Route/session key, separate from `session_id`. |
| `cwd` | string | `prov.cwd`, `cwd` | null | Use for attribution if safe. |
| `repository` | string | `prov.repository`, `repository` | null | Remote or `owner/repo`. |
| `routing_provider` | string | `prov.routing.provider`, `mux.provider`, `mux.selected_provider` | null | Evidence only. |
| `routing_model` | string | `prov.routing.model`, `mux.model`, `mux.selected_model` | null | Evidence only. |
| `routing_reason` | string | `prov.routing.reason`, `mux.reason`, `mux.fallback_reason` | null | Evidence only. |
| `redaction_level` | string | `redaction.level` | null/`unknown` in diagnostics | Do not assume safe if absent. |
| `sensitivity` | string | `sensitivity`, `redaction.sensitivity` | null | Keep compact; full list can stay in attrs JSON. |
| `preview_truncated` | bool | parser-computed | false | True if any preview hit limit. |
| `preview_bytes` | int64 | parser constant/config | null | Usually 2000. |

Do **not** remove or rename existing fields. Do **not** replace `attributes_json`; keep full non-secret metadata for future reprocessing.

Compatibility note: the public `spans` view and `spans_for_date()` macro coalesce
these nullable durable columns from explicit `attributes_json` attrs at read time.
This makes legacy AgentWeave/OpenClaw partitions usable for recall and cost
analytics without rewriting parquet. The stored rows remain unchanged; a physical
backfill should only be added later if compaction or export jobs need first-class
columns in the parquet files themselves.

---

## Parser mapping details

### Session ids

Recommended parser logic:

```text
session_id = attrs["session.id"] or attrs["prov.session.id"]
session_key = attrs["prov.session.key"]
```

If only `session_key` exists, leave `session_id` null. This prevents OpenClaw route keys from becoming durable archive ids.

### Harness

```text
harness = attrs["prov.harness"] or attrs["harness"] or attrs["service.name"]-derived only if explicit and stable
```

Avoid guessing `openclaw` from service name alone unless the service explicitly belongs to the OpenClaw bridge. Unknown is acceptable.

### Repository attribution

Priority:

1. Explicit `prov.repo.owner` + `prov.repo.name`
2. Explicit `prov.repository` parsed by existing attribution helpers
3. Existing `_repo_owner` / `_repo_name` / `gitBranch` style fields
4. Session-level `agent_events` fallback in `_session_repo_map`
5. Agent/day fallback only in `spans_enriched`, not in raw `spans`

### Routing facts

Routing fields must stay evidence. Nexus stores them for recall/cost analysis, but does not route future calls.

Accepted attrs can include both `prov.routing.*` and `mux.*` names because Mux bridge conventions may evolve. Preserve the full original attrs in `attributes_json` even when dedicated columns are populated.

### Preview truncation

Parser should return both the truncated preview and metadata:

```text
prompt_preview = first 2000 chars after redaction
response_preview = first 2000 chars after redaction
preview_truncated = true if any preview was shortened
preview_bytes = 2000
```

If redaction metadata is missing, store bounded previews but diagnostics should treat them as unknown safety.

---

## Link view/macro design

Add a read-oriented view or bounded macro that links spans to native events. Prefer a bounded macro if broad scans are expensive.

Recommended output columns:

| Column | Meaning |
|---|---|
| `trace_id` | Span trace id. |
| `span_id` | Span id. |
| `span_session_id` | `spans.session_id`. |
| `span_session_key` | `spans.session_key`. |
| `event_session_id` | `agent_events.session_id`. |
| `event_session_key` | Extracted from `agent_events.raw_data.session_key`. |
| `agent_id` | Canonical Nexus agent id. |
| `harness` | Span or event harness. |
| `repo_owner`, `repo_name`, `branch` | Best exact attribution. |
| `link_method` | `canonical_session_id`, `session_key`, `parent_session_id`, `agent_day_project`, `unmatched`. |
| `link_confidence` | `exact`, `strong`, `weak`, `none`. |

Join order:

1. Exact UUID: `spans.session_id = agent_events.session_id`.
2. Session key: `spans.session_key = json_extract_string(agent_events.raw_data, '$.session_key')`.
3. Parent session: `spans.parent_session_id = agent_events.session_id`.
4. Agent/day/project fallback for aggregates only.

Implementation note: DuckDB JSON extraction syntax depends on how `raw_data` is stored in Parquet. If it is serialized as string, use `json_extract_string(TRY_CAST(raw_data AS JSON), '$.session_key')`; if it is a struct/map, adapt accordingly and cover both with tests or a helper view.

---

## Diagnostics schema

Add a read-only health section under existing quality/doctor output. Suggested JSON shape:

```json
{
  "openclaw_agentweave": {
    "status": "ok|warning|critical",
    "window_hours": 24,
    "native_events_recent": 12,
    "spans_recent": 34,
    "native_session_uuid_percent": 95.0,
    "span_session_id_percent": 90.0,
    "span_session_key_percent": 100.0,
    "link_exact_percent": 80.0,
    "link_strong_percent": 10.0,
    "link_weak_percent": 5.0,
    "unmatched_span_count": 2,
    "repo_attribution_percent": 88.0,
    "redaction_known_percent": 92.0,
    "routing_fact_count": 10,
    "samples": {
      "unmatched_spans": ["trace/span"],
      "missing_session_key": ["trace/span"]
    }
  }
}
```

Severity guidance:

- Critical: malformed required span ids, duplicate `dedup_key`, no recent spans from a required configured source, or diagnostics query failure.
- Warning: missing session ids/keys, low linkability, missing redaction metadata, missing repo attribution.
- OK: data present and linkability above threshold.

Thresholds should be conservative and configurable or clearly documented. Do not make low-volume hobbyist data fail hard.

---

## Backward compatibility checklist

Before merging implementation, verify:

- Existing `tests/test_agentweave_parser.py` still passes.
- Existing `tests/test_otlp_ingest.py` still passes against old-style spans without new attrs.
- A span with only `trace_id`, `span_id`, `start_time`, and no session metadata still ingests.
- Existing parquet partitions read through `spans` with new columns null/defaulted.
- `spans_enriched` still works for historical data.
- Broad live diagnostics do not reopen file-descriptor pressure; bounded macros exist for heavy checks.
- MCP outputs do not leak full prompts/tool payloads beyond preview policy.

---

## Future follow-ups intentionally out of scope

These are useful but should be separate issues/PRs unless already requested:

- Full OpenClaw plugin implementation inside the OpenClaw repo.
- Live AgentWeave bridge changes; those belong in `arniesaha/agentweave`.
- UI/dashboard for traces.
- Automatic model-routing decisions from Nexus.
- Enterprise/team/multi-tenant context features.
- Migrating `agent-shared` semantics into general Nexus context containers.
