# MCP read contract

Every registered read tool uses `src/drover/server/mcp/contract.py`. Limits must
be positive integers; oversized limits clamp before execution and set top-level
`truncated: true`. Audit reads reject more than 25 harness IDs. The response
bounds also cover nested collections, text, keys and metadata, using escaped JSON
bytes. No response contains partial JSON. Each text field has a 4,096 UTF-8 byte
cap; the entire serialized response has a 65,536 byte cap.

| Tool | Maximum items per collection |
| --- | ---: |
| drover_memory_acceptance | 25 |
| drover_handoff | 20 |
| drover_session_replay | 100 |
| drover_session_summary | 100 |
| drover_active_sessions | 100 |
| drover_search | 100 |
| drover_recall_bundle | 20 |
| drover_files_touched | 100 |
| drover_project_brief | 100 |
| drover_recent_sessions | 20 |
| drover_recent_contexts | 100 |
| drover_context_brief | 100 |
| drover_open_loops | 100 |
| drover_resume_context | 20 |
| drover_recall | 20 |
| drover_task_status | 100 |
| drover_project_activity | 200 |
| drover_active_handoff | 100 |
| drover_fleet_status | 100 |
| drover_data_quality | 100 |
| drover_pipeline_observatory | 20 |
| drover_provider_quota | 50 |

Existing narrower bundle/activity limits still apply. Project Activity's detailed
truncation flags move to `truncation_details`; `truncated` is now a boolean.
Files touched samples at most 101 input events and reports truncation if the
sample or resulting file list exceeds its limit. Fleet `count` is the pre-cap
live-session count; the returned list can be smaller with `truncated: true`.

Each MCP read has a 5-second caller deadline and returns `status: timeout` if
exceeded. Four read calls can execute at once per server; further calls return
`status: busy`. Timed-out execution retains its admission slot until it finishes.
These are response-level deadlines, not cancellation of SQL or LLM work. Phase 4
(#481) owns disposable query processes and hard execution cancellation. This
contract does not introduce analytical storage or query process changes.

`drover_active_sessions` and `drover_fleet_status` use the authoritative harness
registry, including live/awaiting sessions regardless of analytical ingestion or
summary completion, excluding retired hosts and terminal sessions. Responses
identify `state_source`, `control_store` (Postgres on the hub), and `authoritative`.

`drover_recall` rejects a wrong vector dimension. Callers can supply
`query_embedding_model`; a mismatch with the configured hub model is an error.
Omitting it asserts that the vector uses the configured model.
`drover_session_close` is a mutation and is outside the read contract.

## Provider quota

`drover_provider_quota` shows provider quota for every account across hosts. It
is a read tool (v0.5.7, #528).

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `provider` | string | all providers | Filter by provider: `google`, `openai`, or `anthropic`. |
| `fresh` | boolean | `false` | If `true`, probe the online hosts again before the read. |

The function also takes `timeout_s` (default `3.0` seconds). It limits how long
a `fresh` probe waits for hosts. The MCP tool does not expose `timeout_s`.

The response has these fields:

- `accounts`: one record for each account, with `provider`, `account_label`,
  `plan`, `hosts`, `status`, `windows`, and `updated_at`.
- `routing_hint`: a short text that ranks pools by headroom.
- `updated_at`: the newest account time.

The tool masks email account labels. For example, `alice@example.com` becomes
`a***@example.com`.

Agy Claude/GPT ("3p") buckets with a sliding reset are omitted (#527). When agy
returns a 429 error, Drover records the window as `observed_exhausted`.

> **Known limitation:** If the hub has authentication on, `fresh=true` returns
> 401 host errors. The MCP server has no hub token. Use `fresh=false` on these hubs.

## Single hub recall endpoint and freshness

The hub is the single supported recall endpoint. Register the hub's `/mcp` URL
in every agent, including agents on collector hosts. Per-host analytical MCP
instances are **unsupported**: there is no recall federation or fallback to a
host's analytical database. Hosts collect events and run harnesses; the hub
serves recall from its ingested events and PostgreSQL memory. Pond is removed;
recall bundles identify `sources: ["hub"]`.

Every MCP read envelope and each returned recall/search/summary/handoff record
includes:

- `store: "hub"`, the logical store identity.
- `host`, the answering hub's hostname on the envelope; on individual records,
  the recorded producer host/agent identity when available, otherwise the hub.
- `data_watermark: {"timestamp": "<UTC ISO-8601>", "basis": "..."}`.
  Summary and project-brief records use their generation time
  (`summary_generated_at`), and active handoffs use their saved brief time
  (`brief_generated_at`); event records use the ingested event's timestamp
  (`event_time`). Context/control records can use their saved update time.
  Bundle projections carry their source time. The envelope uses the latest
  watermark among returned data items, so unrelated fresh activity cannot hide
  a stale scoped result.

A missing watermark is explicit: `{"timestamp": null, "basis": "unknown"}`.
Retrieval timestamps and healthy host heartbeats do not imply fresh recall data.
No-data optional MCP reads return an `unavailable` envelope with identity and an
unknown watermark. `timeout` and `busy` envelopes also carry identity and an
unknown watermark. Implementation/validation failures return a bounded
`status: error` envelope with `error_type`, `error`, identity, and an unknown
watermark. Identity and watermark metadata count toward the byte budget.

## Portable profile

`drover_profile(scope="first_turn")` returns a tier-filtered portable profile
within a 1,500-token ceiling. The current MCP transport reads as general.
`drover_profile_propose(layer, kind, tier, body, ...)` creates a pending proposal.
See [portable profile](portable-profile.md) for credential-authenticated HTTP,
trusted auto-acceptance, private review, reversal and markdown import.


Client startup examples and the single-call profile contract are in
[docs/integrations](integrations/README.md). Profile bundles report
`oldest_item_age_seconds` alongside their rendered-source watermark. Empty
profiles retain an unknown watermark even when unrelated hub data is fresh.
Clients own startup loading; trusted profile reads use authenticated HTTP.
