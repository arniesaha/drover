# OpenClaw + AgentWeave Validation Matrix

This matrix is for implementation agents working from `docs/openclaw-agentweave-implementation-plan.md`. It lists the minimum scenarios that should be covered before Nexus claims OpenClaw/AgentWeave integration readiness.

---

## Parser scenarios

| Scenario | Fixture/test input | Expected result |
|---|---|---|
| OpenClaw canonical UUID + session key | Native event has `session_uuid` and `session_key` | `AgentEvent.session_id` equals UUID; `raw_data.session_key` equals route key. |
| OpenClaw legacy route-key only | Native event lacks UUID but has `session_key` | Event ingests; `raw_data.session_uuid_missing = true`; route key preserved. |
| OpenClaw source event mapping | Native `message.queued`, `message.processed`, `command:new`, tool event | Event type maps to contract vocabulary; original event name remains in `raw_data.event_name`. |
| OpenClaw parent/child session | Lifecycle event includes parent and child ids/keys | Parent/child fields are preserved in `raw_data`. |
| OpenClaw redaction | Native event marks content redacted/previewed | Redaction object is preserved and no full secret-like content enters fixture assertions. |
| AgentWeave canonical session | Span has `prov.session.id` | `spans.session_id` populated. |
| AgentWeave session key only | Span has `prov.session.key` but no `session.id`/`prov.session.id` | `session_id` null; `session_key` populated. |
| AgentWeave OpenClaw harness | Span has `prov.harness=openclaw` | `harness=openclaw`. |
| AgentWeave repository attrs | Span has `prov.repo.owner`, `prov.repo.name`, `prov.git.branch` | Dedicated repo columns populated. |
| AgentWeave Mux attrs | Span has `mux.provider`, `mux.model`, route reason | Routing evidence columns populated; attrs still preserved in JSON. |
| Preview limit | Preview exceeds 2 KiB | Stored preview is 2 KiB; `preview_truncated=true`; `preview_bytes=2000`. |
| Missing optional metadata | Span lacks session/agent/project/redaction attrs | Span ingests; optional fields null/default; health warning later. |

---

## Storage/schema scenarios

| Scenario | Expected result |
|---|---|
| Old span partition without new columns | `spans` view reads successfully with null/default new fields. |
| New span partition with new columns | `spans` and `spans_enriched` expose new fields. |
| Duplicate span ingest | Existing `dedup_key` is skipped idempotently. |
| Repo attribution from explicit attrs | Raw `spans` row has repo columns without relying on fallback. |
| Repo attribution from session fallback | `_normalize_row` can fill repo from `agent_events` session map when exact session id matches. |
| Agent/day fallback | Only `spans_enriched` uses weak aggregate fallback; raw rows remain source-faithful. |
| Attributes JSON compatibility | New and unknown attrs round-trip in `attributes_json`. |

---

## Linking scenarios

| Scenario | Link method | Confidence |
|---|---|---|
| Span session UUID equals native event session UUID | `canonical_session_id` | `exact` |
| Span session key equals native event raw session key | `session_key` | `strong` |
| Span parent session id equals native event session id | `parent_session_id` | `strong` or `weak`, depending on implementation |
| Span only shares agent/date/project | `agent_day_project` | `weak`; aggregate only, not replay |
| No match | `unmatched` | `none` |

Linking must be additive. A bad link candidate must not rewrite `spans.session_id` or `agent_events.session_id`.

---

## Health/quality scenarios

Diagnostics should be read-only and should handle empty/partial stores gracefully.

| Scenario | Expected status |
|---|---|
| No OpenClaw configured or no OpenClaw data in window | Skip or neutral if source is optional. |
| Required OpenClaw source configured but no recent native events | Warning/critical depending on existing source severity conventions. |
| Recent spans but low session id coverage | Warning. |
| Recent spans but malformed missing trace/span ids | Critical if rows exist with invalid required identity. |
| Low exact linkability but session-key fallback works | Warning or degraded, not critical. |
| Missing redaction metadata on previews | Warning and previews must remain bounded. |
| Mux routing spans present but routing columns empty | Warning. |

---

## MCP/handoff scenarios

| Scenario | Expected user-facing behavior |
|---|---|
| Handoff for linked OpenClaw session | Includes recent turns plus compact provenance evidence: trace/span ids, model/cost/routing facts where available. |
| Handoff for native events without spans | Still works, just lacks cost/trace evidence. |
| Handoff for spans without native events | Can summarize provenance/project activity, but does not invent a replay transcript. |
| Session key disagreement | Shows/confidence-marks provisional link; does not silently merge unrelated sessions. |
| Sensitive/redacted previews | MCP output respects preview policy and does not emit full raw payloads. |

---

## Required validation commands

Run focused tests first:

```bash
python -m pytest \
  tests/test_parsers.py \
  tests/test_agentweave_parser.py \
  tests/test_otlp_adapter.py \
  tests/test_otlp_ingest.py \
  -q
```

Run any new schema/quality/MCP tests added by the implementation:

```bash
python -m pytest tests/test_quality.py tests/test_dogfood_smoke.py -q
```

If a listed test file does not exist, either create the narrow test needed for the implemented behavior or document why the behavior is covered elsewhere.

Finish with:

```bash
python -m pytest -q
```

---

## Manual review checklist

Before asking for review, confirm:

- [ ] `session_id` and `session_key` remain separate everywhere.
- [ ] OpenClaw source names are preserved in `raw_data.event_name`.
- [ ] New span fields are additive and old partitions remain readable.
- [ ] Missing optional metadata never fails ingest.
- [ ] Diagnostics are read-only and bounded.
- [ ] Mux facts are treated as evidence only.
- [ ] AgentWeave remains provenance/observability; Nexus remains context/handoff.
- [ ] Public docs avoid vague AI-platform jargon, avoid productizing `lakehouse`, and avoid enterprise context-layer language.
- [ ] Fixtures contain no secrets, tokens, private IPs, or private hostnames.
