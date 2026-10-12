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
| `transcript_rewrite_watermarks` | `session_id`, `generation` |
| `session_transcript_archives` | `session_id`, `generation`, `encoding`, `archive_blob`, `archive_sha256`, `created_at` |
| `session_transcript_cold_archives` | `session_id`, `generation`, `archive_name`, `archive_sha256`, `storage`, `archive_blob`, `archive_bytes` |

Auth profiles, dispatch tokens, account ids, peer ids, delivery targets,
session entry JSON, caches and memory indexes are never selected. Transcript content itself is imported unchanged, as it is from JSONL.

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

The cursor also records OpenClaw's rewrite generation and a hash of the last
read row. A generation change or changed/missing row triggers a replay, even
when a shortened transcript has already grown past the old sequence number.
Ingest dedupes unchanged events. An older cursor replays once to establish its
identity evidence. Stores without generation markers use the row hash, which
cannot detect changes confined to earlier rows when the checkpoint row is
unchanged. Watermarks of sessions that OpenClaw deleted are dropped.

## Archived transcripts

Registered reset/deleted generations in `session_transcript_archives` are read
from identity or zstd blobs. Cold transcripts in
`session_transcript_cold_archives` are read from SQLite blobs or the registered
`sessions/cold/<hash>.jsonl.zst` file. The collector verifies stored hashes,
file sizes and archive header identity before importing transcript records.
Cold storage indexes and identity records are not imported.

Each archive generation has its own resume position and shares the configured
event budget with live rows. Moving a previously collected session to cold
storage may replay its events; the existing ingest dedup key collapses repeats.
Unavailable, corrupt or undecodable archives retain their cursor for retry.
Archive checkpoint entries are removed after their registry entry disappears.
No archive is restored, published, rewritten or materialized by Drover.

## Collector health

Successful non-dry-run collection and event shipping update
`last_successful_collect_at`, including idle runs with no new events. The
collector ships a separate `.collector-<source>.json` heartbeat containing only
progress metadata. Failed source reads and failed shipments preserve the last
successful time. Dry runs do not claim successful delivery.

`drover doctor`, runtime audit and quality freshness checks report per-host,
per-source collector lag in seconds since the last successful collect. This is
separate from the latest event timestamp. Prometheus quality output includes
`drover_collector_lag_seconds` and
`drover_collector_last_success_timestamp_seconds`. A collector reaching its event budget
reports that it is still catching up. Lag of 10 minutes warns; one hour is
critical. Missing or invalid metadata is explicitly unknown, and older
collectors without heartbeats remain unknown until upgraded.

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

- Only registered archives are read; unregistered legacy archive files are
  outside this SQLite source.
- Archive bytes and decoded content are capped at 64 MiB per generation, with
  an 8 MiB record limit. Larger archives are reported unavailable for retry.
- zstd payloads compressed with a custom dictionary are not supported.
