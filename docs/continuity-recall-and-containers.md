# Continuity recall and context containers

## One recall endpoint

The hub is the authoritative recall endpoint for search, semantic recall,
recall bundles, and handoff. Connect agents to the hub MCP endpoint. A host-local
analytical instance must proxy to the hub or identify its results as
non-authoritative. Drover's local instances use the latter behavior; they do not
federate results or imply that local history covers the fleet. The removed Pond
integration is not a recall source.

Every MCP read envelope and returned data record includes additive provenance:

| Field | Meaning |
| --- | --- |
| `store` | `hub` for a registered central PostgreSQL store, otherwise `local` |
| `store_authoritative` | Whether this logical store is the authoritative hub |
| `host` | Answering process host on the envelope; known producer host on records, otherwise answering host |
| `data_watermark.timestamp` | An observed data timestamp, normalized to UTC; null when unavailable |
| `data_watermark.basis` | The timestamp field or store observation used |

`authoritative` on fleet responses retains its existing control-plane meaning.
`store_authoritative` describes recall scope separately. A harness name is not a
host identity. Older response fields, including recall bundle `sources`, remain
for compatibility; consult the provenance fields for authority.

Non-empty envelopes summarize returned data. Summary and brief generation times
have priority over session end time. Context generation times come from source
generation, with activity timestamps available as a fallback. Re-projecting or
stamping results again preserves their observed watermark. Retrieval time and
certification time never replace source data time.

An empty result can still report the latest observed derived-store generation,
context-store update, or persisted event ingestion watermark. These store-wide
bases are explicitly named and do not claim that the query matched that data.
No history scan is performed just to decorate an empty result. Null with
`basis: unknown` means no timestamp was available within the read contract,
including busy, timeout, and error envelopes. Selected DuckLake failures never
fall back to a legacy catalog. Row, text, response-byte, and five-second admission
limits remain in `server/mcp/contract.py`.

## Container producer

The hub worker reads canonical final session summaries and project briefs from
PostgreSQL. It creates one stable container per session and one per project
brief. Keys contain a SHA-256 digest of source identity, so reruns upsert the same
rows. Reruns preserve creation and source-generation timestamps; older sources
cannot overwrite a newer container. A source deletion does not delete an existing
container, and independently curated containers are retained.

Each container carries a label, type, confidence, evidence link, session/task
links, next action, open questions, summary, and last activity. Evidence uses
`drover://session/<id>` or `drover://project/<key>` references plus the source
generation timestamp and classification basis. `source_harness` comes from the
central session registry when known, including native session aliases.

Classification is deliberately conservative:

| Evidence | Type | Confidence |
| --- | --- | --- |
| Explicit valid `owner/name` project key | `code_project` | 0.95 |
| Other session evidence | `general_activity` | 0.5 |

These confidence values describe a rule, not a model probability. The writer does
not infer personal, operational, research, or conversation types from prose. The
existing vocabulary and curated classifications remain supported by the readers.

Only allowlisted derived fields are copied. Raw prompts, transcripts, tool
payloads, environment values, and arbitrary source metadata are excluded.
Credential-shaped values are redacted before truncation or persistence. Text over
16,384 characters is replaced by an explicit omission notice before credential
regexes run; copied text is capped at 4,096 characters and labels at 160.
Credential-shaped identities are rejected rather than turned into broken links.

An existing `redaction_policy` is never weakened. `metadata-only` suppresses
copied narrative, next actions, and questions; unknown explicit policies prevent
writer updates. Resume returns no linked summaries for those restrictive
policies. The default `session-summary-redacted` policy permits redacted linked
summaries. Older published rows with an absent policy use that default when
resuming. DuckLake byte limits are checked before redaction.

Compatibility mode uses the existing `context_containers` table. DuckLake mode
publishes and certifies an authoritative source revision through the coverage
APIs, under the projection fence. It does not import the compatibility table or
bypass verification. Each pass refreshes the publication observation and
certificate while retaining source timestamps and a content-hash watermark.
A failed certificate leaves coverage explicitly unavailable.

The four context tools use these rows directly: `drover_recent_contexts` lists
containers, `drover_context_brief` selects one by ID or label, `drover_open_loops`
filters known actions/questions, and `drover_resume_context` retrieves the
container and permitted linked summaries.

## Configuration and backfill

The worker is off by default:

```toml
[context_containers]
enabled = false
```

Enabling it requires the central PostgreSQL control store. The all or analytics
role runs a pass every 60 seconds; the API-only role does not start derived
workers. The flag is independent of model-worker flags because this producer
does not call a model. This change does not enable the flag on any host.

Backfill uses the same writer and is dry-run by default, even with the worker
flag disabled:

```sh
drover-server --config <config.toml> context backfill-containers
drover-server --config <config.toml> context backfill-containers --dry-run
```

The JSON report includes source, created, updated, unchanged, and applied counts.
Dry-run writes no containers, source revisions, or certificates, and does not
create a missing analytical database. Applying requires explicit `--apply`:

```sh
drover-server --config <config.toml> context backfill-containers --apply
```

The compatibility table must already be initialized before apply. For DuckLake,
the selected catalog and serving proof must be valid, and coverage tables must
have been explicitly provisioned through
`drover.server.lake.coverage.provision_coverage`. The writer and CLI do not create
proof tables or silently repair an invalid selection. A separate CLI process
cannot open a compatibility catalog held by the running server; use an offline
maintenance window for compatibility-mode apply.

The default combined source/container bound is 1,000. `--max-containers` can raise
the compatibility-mode backfill bound. DuckLake publication still enforces its
existing 1,000-row and 1 MiB snapshot limits. Oversized snapshots are rejected,
never silently truncated. The worker logs a failed pass and retries on its next
interval. Production enablement, production backfill, larger publication
protocols, profile generation, and UI changes are outside this change.
