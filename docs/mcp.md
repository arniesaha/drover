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

Existing narrower bundle/activity limits still apply. Project Activity's detailed
truncation flags move to `truncation_details`; `truncated` is now a boolean.
Files touched samples at most 101 input events and reports truncation if the
sample or resulting file list exceeds its limit. Fleet `count` is the pre-cap
live-session count; the returned list can be smaller with `truncated: true`.

Each MCP read has a 15-second caller deadline and returns `status: timeout` if
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
