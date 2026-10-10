# OpenClaw session collection

`drover-collect` captures OpenClaw conversations so they can be recalled and
summarized like any other harness. Collection is observe-only: Drover never
drives, schedules or writes to OpenClaw. For the profile bootstrap note that
OpenClaw loads at session start, see [the bootstrap note](openclaw-hermes.md).
Configuration keys are listed in
[Integrations](../integrations.md#openclaw-sessions).

## Which store is read

| OpenClaw install | Store | Source id |
| --- | --- | --- |
| Older releases | JSONL files under `<state dir>/agents/<agentId>/sessions` | `openclaw` |
| Current releases | `<state dir>/agents/<agentId>/agent/openclaw-agent.sqlite` | `openclaw_sqlite` |

With `store = "auto"` (the default) both are read when present. The JSONL
source keeps its existing cursor and simply finds nothing new once OpenClaw
stops writing JSONL. Every agent directory under the state directory is
discovered; restrict it with `agents = ["main"]`.

Sessions that were shipped from JSONL and later migrated into SQLite are read
again by the SQLite source. They map to the same session id, timestamp, event
type and content, so ingest collapses them on its dedup key.

## How the SQLite store is read

The OpenClaw gateway is the single writer of these databases, so the collector
is built to stay out of its way:

- The database is opened with a `mode=ro` URI and `PRAGMA query_only = ON`.
  The collector cannot create, modify or checkpoint it.
- The connection is in autocommit mode. Every statement is small, uses the
  primary key, and is fully fetched before the next one, so no read
  transaction outlives a single statement.
- Rows are read `batch_size` at a time and a run stops after
  `max_events_per_run` events.
- A locked database is retried twice with a short backoff and then skipped
  until the next run. Events already read in that run are kept.

Only these tables are read, and only the listed columns:

| Table | Columns |
| --- | --- |
| `transcript_events` | `session_id`, `seq`, `created_at`, `event_json`, `event_zstd` |
| `session_windows` | `session_key`, `channel`, `chat_type`, `model_provider`, `model`, `primary_conversation_id`, `parent_session_key`, `spawned_by`, `display_name` |
| `session_nodes` | `label`, `display_name`, `parent_session_key`, `spawned_by`, `created_via` |
| `session_conversations` | `conversation_id`, `role` |
| `conversations` | `channel`, `kind` |
| `schema_meta` | `app_version` |

Auth profiles, dispatch tokens, account ids, peer ids, delivery targets,
session entry JSON, caches, memory indexes and transcript archives are never
selected. Transcript content itself is imported unchanged, as it is from JSONL.

The schema is discovered at run time. `transcript_events` with `session_id`,
`seq`, `created_at` and at least one payload column is required; every other
table and column is optional and used when present. A database without that
shape is skipped with a warning that names what is missing, and other agents'
databases are still collected.

## Watermark

The watermark is the primary key of `transcript_events`: the last `seq` read
per session, per agent, stored in the `openclaw_sqlite` cursor file under the
collector's `state_dir`. SQLite's `rowid` is not used because the table has no
integer primary key, so `rowid` values can be reused or renumbered.

The cursor advances only after a run's events have been staged and shipped. A
restart, a failed ship or a skipped database resumes from the stored position.
`watermark_iso` in that cursor is the newest event time seen and is shown by
`drover-collect status`; it does not decide what is read.

If a session's stored transcript ends before its watermark, OpenClaw rewrote
that transcript. The session is read again from its first row and ingest
dedupes the unchanged events. Watermarks of sessions that OpenClaw deleted are
dropped.

## Event mapping

Each transcript record goes through the same mapper as a JSONL line:

| Drover field | Source |
| --- | --- |
| `session_id` | `transcript_events.session_id` |
| `id` | record `id`; otherwise `openclaw:<agent>:<session>:<seq>` |
| `timestamp` | record `timestamp`; otherwise `created_at` |
| `agent_id` | `openclaw` (the OpenClaw agent id is `raw_data.agent_id`) |
| `event_type` | same vocabulary as the JSONL source |
| `message` | record `message.role` and `message.content` |
| `tool_calls` | `toolCall` (or `tool_use`) content blocks: name and arguments |
| `raw_data.session_key`, `channel`, `parent_session_key`, `topic` | session tables |
| `raw_data.harness_version` | `schema_meta.app_version` |
| `raw_data.openclaw_store` | `seq`, chat type, model, provider, spawn origin |

The `session` header record sets session-level fields such as `cwd` and emits
no event, as with JSONL. Tool call extraction also applies to the JSONL source.

## Troubleshooting

Warnings are logged as `[openclaw_sqlite] ...` by `drover-collect run`.

| Message | Meaning |
| --- | --- |
| `no OpenClaw agent database matches ...` | `store = "sqlite"` but nothing is under `state_dir`. Check `state_dir`. |
| `unknown OpenClaw schema, skipped` | The database has no usable `transcript_events`. The message lists what is missing. |
| `database busy after 2 retries` | The gateway held the database. Nothing is lost; the next run continues. |
| `paused at zstd-compressed events` | A compressed row needs a zstd decoder: run the collector on Python 3.14 or newer, or install `zstandard`. The session resumes at that row afterwards. |
| `skipped N transcript row(s)` | Rows that were not JSON objects or could not be mapped. They are not retried. |
| `rewritten by OpenClaw and re-read` | See [Watermark](#watermark). |

## Limits

- Transcripts that OpenClaw moved to cold archive files or archive blobs are
  not read.
- A transcript that is rewritten and then grows past the old watermark before
  the next run is not detected as rewritten.
- zstd payloads compressed with a custom dictionary are not supported.
