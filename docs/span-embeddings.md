# Span-derived embeddings

Issue #80 splits span-derived text embeddings from session-summary embeddings.

## Storage model

- `session_embeddings` remains keyed by `session_id` and stores vectors for `session_summaries.summary_md` only. These hits are synthesized handoff/session summaries.
- `span_embeddings` is a sibling table keyed by `span_id` and stores vectors for raw span-derived text. It carries source metadata (`trace_id`, `session_id`, `task_id`, `agent_id`, `repo_owner`, `repo_name`, `branch`) plus the exact `source_text` and `source_fields` used to produce the vector.
- `embed_jobs` remains the session-summary queue. `span_embed_jobs` is the span queue and is drained by the same `EmbedWorker`/backend configuration.

Keeping the tables separate prevents UI/MCP consumers from implying that a span hit has summary semantics.

## Span source semantics

A span embedding represents selected text already present on a `spans` row. It is not a summary and it is not reconstructed conversation history. The embeddable fields are:

- `name`
- `project`
- `task_label`
- `activity_type`
- `prompt_preview`
- `response_preview`

Before embedding or persistence, Nexus builds a labeled text block from non-empty fields, redacts common emails/tokens/secrets, and truncates the final text to `SPAN_EMBED_MAX_CHARS` (currently 4096 characters). `source_fields` records the included columns.

## Retrieval and audit

`nexus_recall` returns a `source_type` for every hit:

- `session_summary`: row came from `session_embeddings` joined to `session_summaries`.
- `span`: row came from `span_embeddings`; `span_id` and `source_text` are populated.

Runtime audit reports `session_embeddings_count` separately from `span_embedding_coverage`, including embedded span count, pending span jobs, recent span count, and coverage percent when calculable.

## Backfilling existing spans

New OTLP ingest enqueues `span_embed_jobs` automatically for newly inserted spans. Existing spans that predate the span embedding pipeline must be queued explicitly by an operator.

Dry-run first:

```bash
nexus-server --config /Users/arnabmac/.nexus/config.toml embeddings enqueue-spans --limit 1000 --since-days 7
```

Apply once the candidate count looks right:

```bash
nexus-server --config /Users/arnabmac/.nexus/config.toml embeddings enqueue-spans --limit 1000 --since-days 7 --apply
```

The command is idempotent: it skips spans that already have a `span_embeddings` row and spans that already have a `span_embed_jobs` row. When date partitions are present, it reads them through `spans_for_date(...)` rather than scanning the whole span parquet tree. After applying, use `runtime-audit` to confirm `span_embed_jobs` or `span_embeddings` is nonzero, then let the normal embedding worker drain the queue.
