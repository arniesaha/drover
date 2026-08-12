"""DuckDB schema bootstrap for the Drover lakehouse.

Idempotent: every CREATE uses IF NOT EXISTS or OR REPLACE.

Layout:
  parquet_dir/
    agent_events/date=YYYY-MM-DD/agent_id=<id>/part-*.parquet
    spans/date=YYYY-MM-DD/part-*.parquet
    pr_events/part-*.parquet
    routing/part-*.parquet
  drover.duckdb
    Tables: tasks, session_summaries, summarize_jobs, decisions
    Views:  agent_events, spans, spans_enriched, session_links, pr_events, routing, sessions, active_sessions
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from drover.agent_aliases import canonicalize_sql
from drover.event_identity import canonical_agent_events_cte
from drover.server.db import (
    CONTROL_PLANE_PRIMARY_KEYS,
    CONTROL_PLANE_TABLES,
    control_plane_connection,
    control_plane_path,
    sql_path_literal,
)
from drover.server.harness.schema import bootstrap_harness_tables

log = logging.getLogger("drover.schema")

PARQUET_SUBDIRS = (
    "agent_events",
    "spans",
    "pr_events",
    "routing",
    "provider_usage_snapshots",
)

EXPECTED_TABLES = (
    "tasks",
    "session_summaries",
    "summarize_jobs",
    "project_briefs",
    "brief_jobs",
    "session_embeddings",
    "embed_jobs",
    "span_embeddings",
    "span_embed_jobs",
    "active_session_briefs",
    "decisions",
    "context_containers",
    "curated_context_records",
    "curated_context_provenance",
    "pipeline_receipts",
    "pipeline_jobs",
    "pipeline_job_attempts",
    "pipeline_artifacts",
    "provider_connections",
    "advisory_findings",
    "advisory_occurrences",
    "span_partition_activity",
    # `harness_*`, `live_recap_jobs` and `live_session_recaps` are deliberately
    # absent: issue #95 moved them to the control-plane store, where they get a
    # DuckDB instance the parquet scans cannot saturate. This tuple is what
    # `drover-server status` counts against the lakehouse, so leaving them here
    # would report an error on every healthy hub. `db.CONTROL_PLANE_TABLES` is
    # the list for the other file.
)
EXPECTED_VIEWS = (
    "agent_events",
    "spans",
    "spans_enriched",
    "session_links",
    "openclaw_span_links",
    "pr_events",
    "routing",
    "provider_usage_snapshots",
    "sessions",
    "active_sessions",
)


_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id           VARCHAR PRIMARY KEY,
  repo_owner        VARCHAR,
  repo_name         VARCHAR,
  branch            VARCHAR,
  explicit_task_id  VARCHAR,
  principal_id      VARCHAR,
  status            VARCHAR,
  title             VARCHAR,
  created_at        TIMESTAMP,
  last_activity_at  TIMESTAMP,
  session_count     INTEGER,
  total_cost_usd    DOUBLE
);
"""

_SPAN_PARTITION_ACTIVITY_DDL = """
CREATE TABLE IF NOT EXISTS span_partition_activity (
  date VARCHAR PRIMARY KEY,
  latest_activity_at TIMESTAMPTZ NOT NULL
);
"""

_SESSION_SUMMARIES_DDL = """
CREATE TABLE IF NOT EXISTS session_summaries (
  session_id        VARCHAR PRIMARY KEY,
  task_id           VARCHAR,
  agent_id          VARCHAR,
  ended_at          TIMESTAMP,
  summary_md        VARCHAR,
  files_touched     VARCHAR[],
  tools_used        MAP(VARCHAR, INTEGER),
  last_user_prompt  VARCHAR,
  last_assistant    VARCHAR,
  next_steps_md     VARCHAR,
  open_questions    VARCHAR[],
  status            VARCHAR,
  generator_model   VARCHAR,
  generated_at      TIMESTAMP
);
"""

_SUMMARIZE_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS summarize_jobs (
  session_id       VARCHAR PRIMARY KEY,
  status           VARCHAR,
  attempts         INTEGER DEFAULT 0,
  last_error       VARCHAR,
  enqueued_at      TIMESTAMP DEFAULT now(),
  updated_at       TIMESTAMP,
  source_version   VARCHAR,
  max_attempts     INTEGER DEFAULT 5,
  next_run_at      TIMESTAMP,
  dead_lettered_at TIMESTAMP,
  stream_publish_needed BOOLEAN DEFAULT FALSE,
  dead_letter_streak INTEGER DEFAULT 0
);
"""

_SUMMARIZE_JOBS_COLUMNS = {
    "source_version": "VARCHAR",
    "max_attempts": "INTEGER DEFAULT 5",
    "next_run_at": "TIMESTAMP",
    "dead_lettered_at": "TIMESTAMP",
    "stream_publish_needed": "BOOLEAN DEFAULT FALSE",
    # Survives the per-generation attempt reset, so a session that keeps
    # producing events can no longer buy an unbounded number of fresh
    # retry budgets for a summary that never succeeds.
    "dead_letter_streak": "INTEGER DEFAULT 0",
}

_LIVE_SESSION_RECAPS_DDL = """
CREATE TABLE IF NOT EXISTS live_session_recaps (
  session_id       VARCHAR PRIMARY KEY,
  recap_text       VARCHAR NOT NULL,
  source_seq       INTEGER NOT NULL,
  generator_model  VARCHAR,
  generated_at     TIMESTAMP NOT NULL DEFAULT now()
);
"""

_LIVE_RECAP_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS live_recap_jobs (
  session_id            VARCHAR PRIMARY KEY,
  desired_source_seq    INTEGER NOT NULL,
  status                VARCHAR NOT NULL,
  attempts              INTEGER NOT NULL DEFAULT 0,
  last_error            VARCHAR,
  enqueued_at           TIMESTAMP NOT NULL DEFAULT now(),
  updated_at            TIMESTAMP NOT NULL DEFAULT now(),
  next_run_at           TIMESTAMP,
  stream_publish_needed BOOLEAN NOT NULL DEFAULT FALSE
);
"""

# Project-level rollup keyed by `<repo_owner>/<repo_name>`. One row per
# project; regenerated from session_summaries when activity warrants.
_PROJECT_BRIEFS_DDL = """
CREATE TABLE IF NOT EXISTS project_briefs (
  project_key       VARCHAR PRIMARY KEY,   -- '<repo_owner>/<repo_name>'
  repo_owner        VARCHAR,
  repo_name         VARCHAR,
  brief_md          VARCHAR,                -- "what is this project, current state"
  recent_themes_md  VARCHAR,                -- last 7-day themes / decisions
  key_files         VARCHAR[],              -- files touched most often
  open_questions    VARCHAR[],
  next_steps_md     VARCHAR,
  session_count     INTEGER,
  last_activity_at  TIMESTAMP,
  generator_model   VARCHAR,
  generated_at      TIMESTAMP
);
"""

_BRIEF_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS brief_jobs (
  project_key VARCHAR PRIMARY KEY,
  status      VARCHAR,
  attempts    INTEGER DEFAULT 0,
  last_error  VARCHAR,
  enqueued_at TIMESTAMP DEFAULT now(),
  updated_at  TIMESTAMP,
  source_session_id VARCHAR,
  source_version VARCHAR
);
"""

_BRIEF_JOBS_COLUMNS = {
    "source_session_id": "VARCHAR",
    "source_version": "VARCHAR",
}

# Embeddings of session_summaries.summary_md, keyed by session_id.
# Stored as FLOAT[] so DuckDB's array_cosine_similarity works directly.
_SESSION_EMBEDDINGS_DDL = """
CREATE TABLE IF NOT EXISTS session_embeddings (
  session_id  VARCHAR PRIMARY KEY,
  embedding   FLOAT[],
  model       VARCHAR,
  dim         INTEGER,
  embedded_at TIMESTAMP
);
"""

_EMBED_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS embed_jobs (
  session_id  VARCHAR PRIMARY KEY,
  status      VARCHAR,
  attempts    INTEGER DEFAULT 0,
  last_error  VARCHAR,
  enqueued_at TIMESTAMP DEFAULT now(),
  updated_at  TIMESTAMP,
  source_version VARCHAR
);
"""

_EMBED_JOBS_COLUMNS = {"source_version": "VARCHAR"}

# Span-derived embeddings are intentionally separate from session-summary
# embeddings: span_id is the source identity, and source_text/source_fields record
# the exact redacted/truncated material embedded from the span row.
_SPAN_EMBEDDINGS_DDL = """
CREATE TABLE IF NOT EXISTS span_embeddings (
  span_id       VARCHAR PRIMARY KEY,
  trace_id      VARCHAR,
  session_id    VARCHAR,
  task_id       VARCHAR,
  agent_id      VARCHAR,
  repo_owner    VARCHAR,
  repo_name     VARCHAR,
  branch        VARCHAR,
  source_text   VARCHAR,
  source_fields VARCHAR[],
  embedding     FLOAT[],
  model         VARCHAR,
  dim           INTEGER,
  embedded_at   TIMESTAMP
);
"""

_SPAN_EMBED_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS span_embed_jobs (
  span_id     VARCHAR PRIMARY KEY,
  status      VARCHAR,
  attempts    INTEGER DEFAULT 0,
  last_error  VARCHAR,
  enqueued_at TIMESTAMP DEFAULT now(),
  updated_at  TIMESTAMP
);
"""

_SPAN_EMBEDDINGS_COLUMNS = {
    "repo_owner": "VARCHAR",
    "repo_name": "VARCHAR",
    "branch": "VARCHAR",
}

# Compact rolling brief for an OPEN session, so another agent can pick up
# the work mid-task without waiting for SessionEnd to fire. Cached with a
# short TTL — refreshed lazily by the MCP tool that reads it.
_ACTIVE_SESSION_BRIEFS_DDL = """
CREATE TABLE IF NOT EXISTS active_session_briefs (
  session_id        VARCHAR PRIMARY KEY,
  brief_md          VARCHAR,
  last_user_req     VARCHAR,
  current_objective VARCHAR,
  files_touched     VARCHAR[],
  open_blockers     VARCHAR,
  suggested_next    VARCHAR,
  events_seen       INTEGER,
  freshness_ts      TIMESTAMP,
  generator_model   VARCHAR
);
"""

_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS decisions (
  decision_id        VARCHAR PRIMARY KEY,
  trace_id           VARCHAR,
  root_span_id       VARCHAR,
  source_span_id     VARCHAR,
  decision_statement VARCHAR,
  rationale          VARCHAR,
  alternatives       VARCHAR[],
  selected_action    VARCHAR,
  session_id         VARCHAR,
  task_id            VARCHAR,
  agent_id           VARCHAR,
  repo_owner         VARCHAR,
  repo_name          VARCHAR,
  branch             VARCHAR,
  decided_at         TIMESTAMPTZ,
  extracted_at       TIMESTAMPTZ,
  extractor          VARCHAR
);
"""

# Confidence-aware context containers let Drover represent resumable work that is
# not necessarily a source-code repository. Repo columns remain nullable and are
# evidence, not identity; the stable key is context_id.
_CONTEXT_CONTAINERS_DDL = """
CREATE TABLE IF NOT EXISTS context_containers (
  context_id       VARCHAR PRIMARY KEY,
  container_type   VARCHAR,  -- code_project | operational_project | personal_project | research_thread | open_floor_conversation | general_activity
  label            VARCHAR,
  source_harness   VARCHAR,
  confidence       DOUBLE,
  evidence         VARCHAR,
  last_touched_at  TIMESTAMPTZ,
  next_action      VARCHAR,
  open_loop        VARCHAR,
  session_ids      VARCHAR[],
  task_ids         VARCHAR[],
  repo_owner       VARCHAR,
  repo_name        VARCHAR,
  branch           VARCHAR,
  summary_md       VARCHAR,
  redaction_policy VARCHAR DEFAULT 'session-summary-redacted',
  created_at       TIMESTAMPTZ DEFAULT now(),
  updated_at       TIMESTAMPTZ DEFAULT now()
);
"""

_CURATED_CONTEXT_RECORDS_DDL = """
CREATE TABLE IF NOT EXISTS curated_context_records (
  record_id       VARCHAR PRIMARY KEY,
  kind            VARCHAR,
  title           VARCHAR,
  content_md      VARCHAR,
  refs            VARCHAR[],
  metadata_json   VARCHAR,
  source_stage    VARCHAR,
  source_path     VARCHAR,
  content_hash    VARCHAR,
  normalized_json VARCHAR,
  created_at      TIMESTAMPTZ DEFAULT now(),
  updated_at      TIMESTAMPTZ DEFAULT now(),
  imported_at     TIMESTAMPTZ DEFAULT now()
);
"""

_CURATED_CONTEXT_PROVENANCE_DDL = """
CREATE TABLE IF NOT EXISTS curated_context_provenance (
  event_id      VARCHAR PRIMARY KEY,
  record_id     VARCHAR,
  event_kind    VARCHAR, -- generated | edited | imported
  source_path   VARCHAR,
  content_hash  VARCHAR,
  details_json  VARCHAR,
  recorded_at   TIMESTAMPTZ DEFAULT now()
);
"""


# ---------------------------------------------------------------------------
# Durable pipeline ledger (AGE-31 / AGE-42)
#
# Four append-aware tables make ingestion and derived-job execution auditable,
# replayable, and crash-recoverable from DuckDB alone, without promoting Redis
# into the source of truth. The lakehouse (parquet + DuckDB) stays the durable
# system of record; Redis (see ``drover.server.jobs``) is optional execution
# coordination only. State-transition rules and the helper API live in
# ``drover.server.ledger``; this module only owns the storage shape.
#
# Operators can answer four questions from these tables alone:
#   receipts  -> what source unit was seen, and was it applied/duplicate/quarantined
#   jobs      -> what logical work item it created and its current state
#   attempts  -> how many times it ran and how each run ended
#   artifacts -> which durable output/version won
# ---------------------------------------------------------------------------

# One durable row per accepted source unit. Fences duplicate upstream work
# before it can create duplicate downstream jobs.
_PIPELINE_RECEIPTS_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_receipts (
  receipt_id      VARCHAR PRIMARY KEY,
  source_kind     VARCHAR NOT NULL,   -- agent_event_batch | otlp_span | session_close | brief_refresh_request | embed_request | ...
  source_key      VARCHAR NOT NULL,   -- natural durable identity of the source unit
  source_version  VARCHAR,            -- optional upstream version/offset/hash discriminator
  subject_kind    VARCHAR,            -- session | span | project | task | batch
  subject_key     VARCHAR,
  payload_hash    VARCHAR,
  status          VARCHAR NOT NULL,   -- observed | applied | duplicate | quarantined | failed
  first_seen_at   TIMESTAMPTZ DEFAULT now(),
  applied_at      TIMESTAMPTZ,
  last_error      VARCHAR,
  metadata_json   VARCHAR,
  -- Idempotency fence: a source unit re-arriving is a lookup, not a new job.
  UNIQUE (source_kind, source_key, source_version)
);
"""

# One durable logical work item per subject + job kind. Represents intent and
# current state independently from any queue backend, and holds the retry cursor.
_PIPELINE_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_jobs (
  job_id            VARCHAR PRIMARY KEY,
  job_kind          VARCHAR NOT NULL,  -- ingest_agent_batch | ingest_span | summarize_session | regenerate_project_brief | embed_session | embed_span | extract_decisions
  subject_kind      VARCHAR,
  subject_key       VARCHAR NOT NULL,
  caused_by_receipt_id VARCHAR,
  status            VARCHAR NOT NULL,  -- pending | leased | succeeded | retry_wait | terminal_failed | dead_lettered | cancelled | superseded
  priority          INTEGER DEFAULT 0,
  attempt_count     INTEGER DEFAULT 0,
  max_attempts      INTEGER DEFAULT 5,
  next_run_at       TIMESTAMPTZ,
  lease_owner       VARCHAR,
  lease_expires_at  TIMESTAMPTZ,
  latest_attempt_id VARCHAR,
  latest_artifact_id VARCHAR,
  created_at        TIMESTAMPTZ DEFAULT now(),
  updated_at        TIMESTAMPTZ DEFAULT now(),
  succeeded_at      TIMESTAMPTZ,
  dead_lettered_at  TIMESTAMPTZ
);
"""

# Append-only execution history for each job. Every retry appends one row; prior
# failures are never overwritten.
_PIPELINE_JOB_ATTEMPTS_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_job_attempts (
  attempt_id        VARCHAR PRIMARY KEY,
  job_id            VARCHAR NOT NULL,
  attempt_no        INTEGER NOT NULL,
  worker_id         VARCHAR,
  started_at        TIMESTAMPTZ DEFAULT now(),
  finished_at       TIMESTAMPTZ,
  result            VARCHAR,           -- succeeded | retryable_failed | terminal_failed | cancelled | superseded (null while running)
  error_category    VARCHAR,
  error_message     VARCHAR,
  retry_at          TIMESTAMPTZ,
  replay_of_attempt_id VARCHAR,
  metrics_json      VARCHAR,
  UNIQUE (job_id, attempt_no)
);
"""

# Append-only durable outputs (or output pointers) produced by a successful
# attempt. Supersession is explicit so singleton projections have one current
# version while history is preserved.
_PIPELINE_ARTIFACTS_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_artifacts (
  artifact_id       VARCHAR PRIMARY KEY,
  job_id            VARCHAR NOT NULL,
  attempt_id        VARCHAR,
  artifact_kind     VARCHAR NOT NULL,  -- agent_events_parquet_part | spans_parquet_part | session_summary | project_brief | session_embedding | span_embedding | decision_batch
  subject_key       VARCHAR,
  storage_uri       VARCHAR,           -- storage URI or durable row key
  content_hash      VARCHAR,
  version_token     VARCHAR,
  supersedes_artifact_id VARCHAR,
  is_current        BOOLEAN DEFAULT TRUE,
  created_at        TIMESTAMPTZ DEFAULT now(),
  metadata_json     VARCHAR
);
"""

_PROVIDER_CONNECTIONS_DDL = """
CREATE TABLE IF NOT EXISTS provider_connections (
  provider                     VARCHAR NOT NULL,
  account_label                VARCHAR NOT NULL,
  host_id                      VARCHAR NOT NULL,
  enabled                      BOOLEAN NOT NULL DEFAULT TRUE,
  supports_usage               BOOLEAN NOT NULL DEFAULT FALSE,
  supports_limits              BOOLEAN NOT NULL DEFAULT FALSE,
  supports_account_discovery   BOOLEAN NOT NULL DEFAULT FALSE,
  supports_refresh             BOOLEAN NOT NULL DEFAULT FALSE,
  capabilities_json            VARCHAR,
  last_attempt_at              TIMESTAMPTZ,
  last_success_at              TIMESTAMPTZ,
  error_category               VARCHAR,
  credential_reference         VARCHAR,
  updated_at                   TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (provider, account_label, host_id)
);
"""

_ADVISORY_FINDINGS_DDL = """
CREATE TABLE IF NOT EXISTS advisory_findings (
  finding_id              VARCHAR PRIMARY KEY,
  fingerprint             VARCHAR NOT NULL UNIQUE,
  analyzer_id             VARCHAR NOT NULL,
  rule_id                 VARCHAR NOT NULL,
  target_type             VARCHAR NOT NULL,
  target_id               VARCHAR NOT NULL,
  analyzer_class          VARCHAR NOT NULL,
  severity                VARCHAR NOT NULL,
  confidence              VARCHAR NOT NULL,
  title                   VARCHAR NOT NULL,
  impact                  VARCHAR NOT NULL,
  remediation_json        VARCHAR NOT NULL,
  state                   VARCHAR NOT NULL,
  dismissal_reason        VARCHAR,
  first_seen_at           TIMESTAMPTZ NOT NULL,
  last_seen_at            TIMESTAMPTZ NOT NULL,
  resolved_at             TIMESTAMPTZ,
  dismissed_at            TIMESTAMPTZ,
  regressed_at            TIMESTAMPTZ,
  evaluated_content_hash  VARCHAR,
  latest_run_id           VARCHAR NOT NULL
);
"""

_ADVISORY_OCCURRENCES_DDL = """
CREATE TABLE IF NOT EXISTS advisory_occurrences (
  occurrence_id   VARCHAR PRIMARY KEY,
  finding_id      VARCHAR NOT NULL,
  run_id          VARCHAR NOT NULL,
  outcome         VARCHAR NOT NULL,
  observed_at     TIMESTAMPTZ NOT NULL,
  source_ref      VARCHAR,
  evidence_json   VARCHAR,
  excerpt         VARCHAR,
  evidence_hash   VARCHAR,
  recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _agent_events_view(parquet_dir: Path) -> str:
    return f"""
CREATE OR REPLACE VIEW agent_events AS
WITH raw_agent_events AS (
  SELECT * FROM read_parquet(
    '{parquet_dir}/agent_events/**/*.parquet',
    hive_partitioning=true,
    union_by_name=true
  )
),
normalized_agent_events AS (
  SELECT *,
         COALESCE(
           CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.cwd') END,
           CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.currentWorkingDirectory') END,
           CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.working_directory') END,
           CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$.workspaceDir') END
         ) AS inferred_cwd,
         CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$._repo_owner') END AS raw_repo_owner,
         CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data, '$._repo_name') END AS raw_repo_name
  FROM raw_agent_events
)
SELECT * EXCLUDE (
         repo_owner, repo_name, inferred_cwd, raw_repo_owner, raw_repo_name
       ),
       COALESCE(repo_owner, raw_repo_owner) AS repo_owner,
       COALESCE(repo_name, raw_repo_name) AS repo_name
FROM normalized_agent_events;
"""


def _spans_view(parquet_dir: Path) -> str:
    """Partition-safe raw spans view plus opt-in attribution enrichment.

    ``spans`` intentionally reads only the spans parquet tree and casts span
    timestamps. It must stay safe for broad freshness/count analytics and must
    not join ``agent_events`` implicitly.

    ``spans_enriched`` preserves the older two-stage repo attribution fallback
    for callers that explicitly need it. Spans from AgentWeave use a different
    ``session_id`` / ``task_id`` namespace than Claude Code agent_events, so
    most spans never match the session-level join. The second join rolls up
    ``agent_events`` to (agent_id, date) → most-frequent repo and lets spans
    inherit attribution from "what this agent was working on that day", which
    is usually unique. See #52 and #69.
    """
    span_attr_select = _span_attr_select()
    return f"""
CREATE OR REPLACE VIEW spans AS
WITH raw_spans AS (
  SELECT *
  FROM read_parquet(
    '{parquet_dir}/spans/**/*.parquet',
    hive_partitioning=true,
    union_by_name=true
  )
)
{span_attr_select}

CREATE OR REPLACE VIEW spans_enriched AS
WITH span_session_days AS (
  SELECT DISTINCT session_id, date
  FROM spans
  WHERE session_id IS NOT NULL
    AND date IS NOT NULL
    AND date <> '_seed'
),
span_agent_days AS (
  SELECT DISTINCT agent_id, date
  FROM spans
  WHERE agent_id IS NOT NULL
    AND date IS NOT NULL
    AND date <> '_seed'
),
session_candidate_events AS (
  SELECT ae.*
    FROM span_session_days sd
    JOIN agent_events ae
      ON ae.session_id = sd.session_id
     AND ae.date BETWEEN strftime(
           TRY_CAST(sd.date AS DATE) - INTERVAL '1 day',
           '%Y-%m-%d'
         )
         AND strftime(
           TRY_CAST(sd.date AS DATE) + INTERVAL '1 day',
           '%Y-%m-%d'
         )
   WHERE ae.repo_owner IS NOT NULL
),
agent_candidate_events AS (
  SELECT ae.*
    FROM span_agent_days sad
    JOIN agent_events ae
      ON ae.agent_id = sad.agent_id
     AND ae.date = sad.date
   WHERE ae.repo_owner IS NOT NULL
),
candidate_agent_events AS (
  SELECT * FROM session_candidate_events
  UNION ALL
  SELECT * FROM agent_candidate_events
),
{canonical_agent_events_cte(source="candidate_agent_events")},
session_repos AS (
  SELECT session_id, date, repo_owner, repo_name, branch
  FROM (
    SELECT sd.session_id,
           sd.date,
           ae.repo_owner,
           ae.repo_name,
           mode(ae.branch) AS branch,
           row_number() OVER (
             PARTITION BY sd.session_id, sd.date
             ORDER BY count(*) DESC, ae.repo_owner, ae.repo_name
           ) AS rn
      FROM span_session_days sd
      JOIN canonical_agent_events ae
        ON ae.session_id = sd.session_id
       AND ae.date BETWEEN strftime(
             TRY_CAST(sd.date AS DATE) - INTERVAL '1 day',
             '%Y-%m-%d'
           )
           AND strftime(
             TRY_CAST(sd.date AS DATE) + INTERVAL '1 day',
             '%Y-%m-%d'
           )
     WHERE ae.repo_owner IS NOT NULL
     GROUP BY sd.session_id, sd.date, ae.repo_owner, ae.repo_name
  )
  WHERE rn = 1
),
agent_day_repos AS (
  SELECT agent_id, date, repo_owner, repo_name, branch
  FROM (
    SELECT sad.agent_id,
           sad.date,
           ae.repo_owner,
           ae.repo_name,
           mode(ae.branch) AS branch,
           row_number() OVER (
             PARTITION BY sad.agent_id, sad.date
             ORDER BY count(*) DESC, ae.repo_owner, ae.repo_name
           ) AS rn
      FROM span_agent_days sad
      JOIN canonical_agent_events ae
        ON ae.agent_id = sad.agent_id
       AND ae.date = sad.date
     WHERE ae.repo_owner IS NOT NULL
     GROUP BY sad.agent_id, sad.date, ae.repo_owner, ae.repo_name
  )
  WHERE rn = 1
)
SELECT
  s.* EXCLUDE (repo_owner, repo_name, branch),
  COALESCE(s.repo_owner, sr.repo_owner, adr.repo_owner) AS repo_owner,
  COALESCE(s.repo_name,  sr.repo_name,  adr.repo_name)  AS repo_name,
  COALESCE(s.branch,     sr.branch,     adr.branch)     AS branch
FROM spans s
LEFT JOIN session_repos sr
  ON s.session_id = sr.session_id
 AND sr.date       = s.date
LEFT JOIN agent_day_repos adr
  ON adr.agent_id = s.agent_id
 AND adr.date     = s.date;
"""


def _json_attr(alias: str, key: str, *, null_empty: bool = True) -> str:
    extract = f"json_extract_string({alias}.attributes_json, '$.\"{key}\"')"
    if null_empty:
        extract = f"NULLIF({extract}, '')"
    return f"CASE WHEN json_valid({alias}.attributes_json) THEN {extract} ELSE NULL END"


def _json_first_attr(alias: str, *keys: str) -> str:
    return "COALESCE(" + ", ".join(_json_attr(alias, key) for key in keys) + ")"


def _span_attr_select() -> str:
    """Return the SELECT that exposes clear AgentWeave attrs as durable columns.

    Some live AgentWeave/OpenClaw rows landed before the OTLP parser persisted
    all provenance fields into first-class Parquet columns. The attrs are still
    present in ``attributes_json``; coalescing them at view time preserves the
    conservative no-backfill behavior while making analytics columns complete.
    ``prov.session.key`` deliberately never fills ``session_id``.
    """
    agent_id = canonicalize_sql(
        f"COALESCE(s.agent_id, {_json_first_attr('s', 'prov.agent.id')})"
    )
    project = _json_first_attr(
        "s", "prov.project", "project", "agentweave.project", "x-agentweave-project"
    )
    cwd = _json_first_attr("s", "prov.cwd", "cwd")
    repository = _json_first_attr("s", "prov.repository", "repository")
    # Repository identity must come from producer metadata or collector-side
    # attribution. A central schema cannot safely infer it from one operator's
    # filesystem layout.
    repo_owner = f"COALESCE(s.repo_owner, {_json_first_attr('s', 'prov.repo.owner', '_repo_owner')})"
    repo_name = f"COALESCE(s.repo_name, {_json_first_attr('s', 'prov.repo.name', '_repo_name')})"
    return f"""
SELECT
  s.* EXCLUDE (
    start_time, end_time, harness, session_id, session_key, task_id, agent_id,
    agent_type, agent_model, associated_with, activity_type, parent_session_id,
    project, cwd, repository, task_label, llm_provider, llm_model, stop_reason,
    repo_owner, repo_name, branch, routing_provider, routing_model,
    routing_reason, redaction_level, sensitivity, cost_usd, prompt_tokens,
    completion_tokens, total_tokens, cache_read_tokens, cache_write_tokens
  ),
  TRY_CAST(s.start_time AS TIMESTAMPTZ) AS start_time,
  TRY_CAST(s.end_time   AS TIMESTAMPTZ) AS end_time,
  COALESCE(s.harness, {_json_first_attr('s', 'prov.harness', 'harness')}) AS harness,
  COALESCE(s.session_id, {_json_first_attr('s', 'session.id', 'prov.session.id')}) AS session_id,
  COALESCE(s.session_key, {_json_first_attr('s', 'prov.session.key')}) AS session_key,
  s.task_id AS task_id,
  {agent_id} AS agent_id,
  COALESCE(s.agent_type, {_json_first_attr('s', 'prov.agent.type')}) AS agent_type,
  COALESCE(s.agent_model, {_json_first_attr('s', 'prov.agent.model')}) AS agent_model,
  COALESCE(s.associated_with, {_json_first_attr('s', 'prov.wasAssociatedWith')}) AS associated_with,
  COALESCE(s.activity_type, {_json_first_attr('s', 'prov.activity.type')}) AS activity_type,
  COALESCE(s.parent_session_id, {_json_first_attr('s', 'prov.parent.session.id')}) AS parent_session_id,
  COALESCE(s.project, {project}, {repo_name}) AS project,
  COALESCE(s.cwd, {cwd}) AS cwd,
  COALESCE(s.repository, {repository}) AS repository,
  COALESCE(s.task_label, {_json_first_attr('s', 'prov.task.label')}) AS task_label,
  COALESCE(s.llm_provider, {_json_first_attr('s', 'prov.llm.provider')}) AS llm_provider,
  COALESCE(s.llm_model, {_json_first_attr('s', 'prov.llm.model')}) AS llm_model,
  COALESCE(s.stop_reason, {_json_first_attr('s', 'prov.llm.stop_reason')}) AS stop_reason,
  {repo_owner} AS repo_owner,
  {repo_name} AS repo_name,
  COALESCE(s.branch, {_json_first_attr('s', 'prov.git.branch')}) AS branch,
  COALESCE(s.routing_provider, {_json_first_attr('s', 'prov.routing.provider', 'mux.provider', 'mux.selected_provider')}) AS routing_provider,
  COALESCE(s.routing_model, {_json_first_attr('s', 'prov.routing.model', 'mux.model', 'mux.selected_model')}) AS routing_model,
  COALESCE(s.routing_reason, {_json_first_attr('s', 'prov.routing.reason', 'mux.reason', 'mux.fallback_reason')}) AS routing_reason,
  COALESCE(s.redaction_level, {_json_first_attr('s', 'redaction.level')}) AS redaction_level,
  COALESCE(s.sensitivity, {_json_first_attr('s', 'sensitivity', 'redaction.sensitivity')}) AS sensitivity,
  COALESCE(s.cost_usd, TRY_CAST({_json_first_attr('s', 'cost.usd')} AS DOUBLE)) AS cost_usd,
  COALESCE(s.prompt_tokens, TRY_CAST({_json_first_attr('s', 'prov.llm.prompt_tokens')} AS BIGINT)) AS prompt_tokens,
  COALESCE(s.completion_tokens, TRY_CAST({_json_first_attr('s', 'prov.llm.completion_tokens')} AS BIGINT)) AS completion_tokens,
  COALESCE(s.total_tokens, TRY_CAST({_json_first_attr('s', 'prov.llm.total_tokens')} AS BIGINT)) AS total_tokens,
  COALESCE(s.cache_read_tokens, TRY_CAST({_json_first_attr('s', 'tokens.cache_read')} AS BIGINT)) AS cache_read_tokens,
  COALESCE(s.cache_write_tokens, TRY_CAST({_json_first_attr('s', 'tokens.cache_write')} AS BIGINT)) AS cache_write_tokens
FROM raw_spans s;
"""


def _span_query_macros(parquet_dir: Path) -> str:
    """Bounded span query macros for ad hoc analytics.

    DuckDB binds every file behind a broad parquet glob before relation-level
    filters can help. These macros force callers to choose one span partition
    date up front, so enrichment reads only that span date and matching
    ``agent_events`` date instead of the full historical tree.
    """
    span_attr_select = _span_attr_select()
    return f"""
CREATE OR REPLACE VIEW span_partitions AS
SELECT DISTINCT regexp_extract(file, '/date=([^/]+)/', 1) AS date
FROM glob('{parquet_dir}/spans/date=*/*.parquet');

CREATE OR REPLACE VIEW agent_event_partitions AS
SELECT DISTINCT regexp_extract(file, '/date=([^/]+)/', 1) AS date
FROM glob('{parquet_dir}/agent_events/date=*/agent_id=*/*.parquet');

CREATE OR REPLACE MACRO spans_for_date(partition_date) AS TABLE
WITH raw_spans AS (
  SELECT *
  FROM read_parquet(
    [
      '{parquet_dir}/spans/date=_seed/*.parquet',
      '{parquet_dir}/spans/date=' || partition_date || '/*.parquet'
    ],
    hive_partitioning=true,
    union_by_name=true
  )
  WHERE date <> '_seed'
)
{span_attr_select}

CREATE OR REPLACE MACRO agent_events_for_date(partition_date) AS TABLE
SELECT *
FROM read_parquet(
  [
    '{parquet_dir}/agent_events/date=_seed/agent_id=_seed/*.parquet',
    '{parquet_dir}/agent_events/date=' || partition_date || '/agent_id=*/*.parquet'
  ],
  hive_partitioning=true,
  union_by_name=true
)
WHERE date <> '_seed';

CREATE OR REPLACE MACRO spans_enriched_for_date(partition_date) AS TABLE
WITH date_agent_events AS (
  SELECT * FROM agent_events_for_date(partition_date)
),
{canonical_agent_events_cte(source="date_agent_events")},
session_repos AS (
  SELECT session_id, repo_owner, repo_name, branch
  FROM (
    SELECT session_id,
           repo_owner,
           repo_name,
           mode(branch) AS branch,
           row_number() OVER (
             PARTITION BY session_id
             ORDER BY count(*) DESC, repo_owner, repo_name
           ) AS rn
      FROM canonical_agent_events
     WHERE repo_owner IS NOT NULL
       AND session_id IS NOT NULL
     GROUP BY session_id, repo_owner, repo_name
  )
  WHERE rn = 1
),
agent_day_repos AS (
  SELECT agent_id, date, repo_owner, repo_name, branch
  FROM (
    SELECT agent_id,
           date,
           repo_owner,
           repo_name,
           mode(branch) AS branch,
           row_number() OVER (
             PARTITION BY agent_id, date
             ORDER BY count(*) DESC, repo_owner, repo_name
           ) AS rn
      FROM canonical_agent_events
     WHERE repo_owner IS NOT NULL
       AND agent_id IS NOT NULL
       AND date IS NOT NULL
     GROUP BY agent_id, date, repo_owner, repo_name
  )
  WHERE rn = 1
)
SELECT
  s.* EXCLUDE (repo_owner, repo_name, branch),
  COALESCE(s.repo_owner, sr.repo_owner, adr.repo_owner) AS repo_owner,
  COALESCE(s.repo_name,  sr.repo_name,  adr.repo_name)  AS repo_name,
  COALESCE(s.branch,     sr.branch,     adr.branch)     AS branch
FROM spans_for_date(partition_date) s
LEFT JOIN session_repos sr
  ON s.session_id = sr.session_id
LEFT JOIN agent_day_repos adr
  ON adr.agent_id = s.agent_id
 AND adr.date     = s.date;
"""


def _refresh_span_partition_activity(con: duckdb.DuckDBPyConnection) -> None:
    """Refresh the small request-time index of span partition activity."""
    con.execute("""
        DELETE FROM span_partition_activity
        WHERE date NOT IN (
          SELECT date FROM span_partitions WHERE date <> '_seed'
        )
        """)
    con.execute("""
        INSERT INTO span_partition_activity BY NAME
        SELECT
          date,
          max(COALESCE(GREATEST(end_time, start_time), end_time, start_time))
            AS latest_activity_at
        FROM spans
        WHERE date IS NOT NULL AND date <> '_seed'
        GROUP BY date
        ON CONFLICT (date) DO UPDATE SET
          latest_activity_at = EXCLUDED.latest_activity_at
        """)


def _pr_events_view(parquet_dir: Path) -> str:
    return f"""
CREATE OR REPLACE VIEW pr_events AS
SELECT * FROM read_parquet(
  '{parquet_dir}/pr_events/**/*.parquet',
  union_by_name=true
);
"""


def _routing_view(parquet_dir: Path) -> str:
    return f"""
CREATE OR REPLACE VIEW routing AS
SELECT * FROM read_parquet(
  '{parquet_dir}/routing/**/*.parquet',
  union_by_name=true
);
"""


def _provider_usage_snapshots_view(parquet_dir: Path) -> str:
    return f"""
CREATE OR REPLACE VIEW provider_usage_snapshots AS
SELECT * FROM read_parquet(
  '{parquet_dir}/provider_usage_snapshots/**/*.parquet',
  union_by_name=true
);
"""


_SESSIONS_VIEW = f"""
CREATE OR REPLACE VIEW sessions AS
WITH {canonical_agent_events_cte()}
SELECT
  e.session_id,
  any_value(e.agent_id) AS agent_id,
  any_value(e.task_id)  AS task_id,
  min(TRY_CAST(e.timestamp AS TIMESTAMP WITH TIME ZONE)) AS started_at,
  max(TRY_CAST(e.timestamp AS TIMESTAMP WITH TIME ZONE)) AS ended_at,
  count(*)              AS event_count,
  ss.summary_md,
  ss.next_steps_md
FROM canonical_agent_events e
LEFT JOIN session_summaries ss USING (session_id)
GROUP BY e.session_id, ss.summary_md, ss.next_steps_md;
"""


_ACTIVE_SESSIONS_VIEW = f"""
CREATE OR REPLACE VIEW active_sessions AS
WITH {canonical_agent_events_cte()}
SELECT
  e.session_id,
  any_value(e.agent_id) AS agent_id,
  any_value(e.task_id)  AS task_id,
  any_value(COALESCE(e.repo_owner, t.repo_owner)) AS repo_owner,
  any_value(COALESCE(e.repo_name, t.repo_name))   AS repo_name,
  any_value(COALESCE(e.branch, t.branch))         AS branch,
  min(TRY_CAST(e.timestamp AS TIMESTAMP WITH TIME ZONE)) AS started_at,
  max(TRY_CAST(e.timestamp AS TIMESTAMP WITH TIME ZONE)) AS last_event_at,
  count(*)                AS event_count
FROM canonical_agent_events e
LEFT JOIN tasks t USING (task_id)
WHERE NOT EXISTS (SELECT 1 FROM session_summaries ss WHERE ss.session_id = e.session_id)
  AND e.session_id <> 'unknown_openclaw'
  AND TRY_CAST(e.timestamp AS TIMESTAMP WITH TIME ZONE) > now() - INTERVAL 30 MINUTE
GROUP BY e.session_id;
"""


_SESSION_LINKS_VIEW = f"""
CREATE OR REPLACE VIEW session_links AS
WITH {canonical_agent_events_cte()},
agent_sessions AS (
  SELECT
    session_id AS source_session_id,
    any_value(agent_id) AS source_agent_id,
    mode(repo_owner) AS repo_owner,
    mode(repo_name) AS repo_name,
    mode(branch) AS branch,
    min(TRY_CAST(timestamp AS TIMESTAMP WITH TIME ZONE)) AS started_at,
    max(TRY_CAST(timestamp AS TIMESTAMP WITH TIME ZONE)) AS ended_at
  FROM canonical_agent_events
  WHERE session_id IS NOT NULL
  GROUP BY session_id
),
span_rows AS (
  SELECT
    session_id AS target_session_id,
    parent_session_id,
    agent_id AS target_agent_id,
    repo_owner,
    repo_name,
    branch,
    start_time
  FROM spans_enriched
  WHERE session_id IS NOT NULL
    AND start_time IS NOT NULL
),
direct_parent_links AS (
  SELECT
    a.source_session_id,
    s.target_session_id,
    a.source_agent_id,
    any_value(s.target_agent_id) AS target_agent_id,
    1.0::DOUBLE AS confidence,
    'parent_session_id' AS reason,
    min(s.start_time) AS first_seen_at,
    max(s.start_time) AS last_seen_at
  FROM agent_sessions a
  JOIN span_rows s
    ON s.parent_session_id = a.source_session_id
  GROUP BY a.source_session_id, s.target_session_id, a.source_agent_id
),
direct_session_links AS (
  SELECT
    a.source_session_id,
    s.target_session_id,
    a.source_agent_id,
    any_value(s.target_agent_id) AS target_agent_id,
    0.95::DOUBLE AS confidence,
    'session_id' AS reason,
    min(s.start_time) AS first_seen_at,
    max(s.start_time) AS last_seen_at
  FROM agent_sessions a
  JOIN span_rows s
    ON s.target_session_id = a.source_session_id
  WHERE s.parent_session_id IS NULL
  GROUP BY a.source_session_id, s.target_session_id, a.source_agent_id
),
explicit_links AS (
  SELECT * FROM direct_parent_links
  UNION ALL
  SELECT * FROM direct_session_links
),
time_candidates AS (
  SELECT
    a.source_session_id,
    s.target_session_id,
    a.source_agent_id,
    any_value(s.target_agent_id) AS target_agent_id,
    0.75::DOUBLE AS confidence,
    'agent_repo_time_window' AS reason,
    min(s.start_time) AS first_seen_at,
    max(s.start_time) AS last_seen_at,
    count(DISTINCT s.target_session_id) OVER (
      PARTITION BY a.source_session_id
    ) AS candidate_targets
  FROM agent_sessions a
  JOIN span_rows s
    ON s.target_agent_id = a.source_agent_id
   AND s.repo_owner = a.repo_owner
   AND s.repo_name = a.repo_name
   AND (
        s.branch = a.branch
        OR s.branch IS NULL
        OR a.branch IS NULL
       )
   AND s.start_time BETWEEN
       a.started_at - INTERVAL 5 MINUTE
       AND a.ended_at + INTERVAL 5 MINUTE
  WHERE a.source_agent_id IS NOT NULL
    AND a.repo_owner IS NOT NULL
    AND a.repo_name IS NOT NULL
    AND s.parent_session_id IS NULL
    AND s.target_session_id != a.source_session_id
    AND NOT EXISTS (
      SELECT 1
      FROM explicit_links el
      WHERE el.source_session_id = a.source_session_id
    )
  GROUP BY a.source_session_id, s.target_session_id, a.source_agent_id
),
time_links AS (
  SELECT
    source_session_id,
    target_session_id,
    source_agent_id,
    target_agent_id,
    confidence,
    reason,
    first_seen_at,
    last_seen_at
  FROM time_candidates
  WHERE candidate_targets = 1
)
SELECT
  source_session_id,
  target_session_id,
  source_agent_id,
  target_agent_id,
  confidence,
  reason,
  first_seen_at,
  last_seen_at
FROM explicit_links
UNION ALL
SELECT
  source_session_id,
  target_session_id,
  source_agent_id,
  target_agent_id,
  confidence,
  reason,
  first_seen_at,
  last_seen_at
FROM time_links;
"""


_OPENCLAW_SPAN_LINKS_VIEW = f"""
CREATE OR REPLACE VIEW openclaw_span_links AS
WITH {canonical_agent_events_cte()},
event_sessions AS (
  SELECT
    session_id AS event_session_id,
    mode(
      CASE
        WHEN json_valid(raw_data)
        THEN NULLIF(json_extract_string(raw_data, '$.session_key'), '')
        ELSE NULL
      END
    ) AS event_session_key,
    any_value(agent_id) AS event_agent_id,
    mode(repo_owner) AS repo_owner,
    mode(repo_name) AS repo_name,
    min(TRY_CAST(timestamp AS TIMESTAMPTZ)) AS first_event_at,
    max(TRY_CAST(timestamp AS TIMESTAMPTZ)) AS last_event_at
  FROM canonical_agent_events
  WHERE session_id IS NOT NULL
    AND CASE
          WHEN json_valid(raw_data)
          THEN COALESCE(json_extract_string(raw_data, '$.harness'), '') = 'openclaw'
          ELSE false
        END
  GROUP BY session_id
),
unique_event_session_keys AS (
  SELECT event_session_key
  FROM event_sessions
  WHERE event_session_key IS NOT NULL
  GROUP BY event_session_key
  HAVING count(DISTINCT event_session_id) = 1
),
span_rows AS (
  SELECT
    trace_id,
    span_id,
    session_id AS span_session_id,
    session_key AS span_session_key,
    parent_session_id,
    agent_id,
    harness,
    repo_owner,
    repo_name,
    start_time
  FROM spans
  WHERE span_id IS NOT NULL
    AND COALESCE(harness, '') = 'openclaw'
),
candidates AS (
  SELECT
    s.trace_id,
    s.span_id,
    s.span_session_id,
    s.span_session_key,
    e.event_session_id,
    e.event_session_key,
    s.agent_id,
    s.harness,
    'canonical_session_id' AS link_method,
    'exact' AS link_confidence,
    1 AS link_rank
  FROM span_rows s
  JOIN event_sessions e
    ON s.span_session_id = e.event_session_id
  WHERE s.span_session_id IS NOT NULL

  UNION ALL

  SELECT
    s.trace_id,
    s.span_id,
    s.span_session_id,
    s.span_session_key,
    e.event_session_id,
    e.event_session_key,
    s.agent_id,
    s.harness,
    'session_key' AS link_method,
    'strong' AS link_confidence,
    2 AS link_rank
  FROM span_rows s
  JOIN event_sessions e
    ON s.span_session_key = e.event_session_key
  JOIN unique_event_session_keys k
    ON k.event_session_key = e.event_session_key
  WHERE s.span_session_key IS NOT NULL
    AND e.event_session_key IS NOT NULL

  UNION ALL

  SELECT
    s.trace_id,
    s.span_id,
    s.span_session_id,
    s.span_session_key,
    e.event_session_id,
    e.event_session_key,
    s.agent_id,
    s.harness,
    'parent_session_id' AS link_method,
    'strong' AS link_confidence,
    3 AS link_rank
  FROM span_rows s
  JOIN event_sessions e
    ON s.parent_session_id = e.event_session_id
  WHERE s.parent_session_id IS NOT NULL

  UNION ALL

  SELECT * EXCLUDE (fallback_candidate_count)
  FROM (
    SELECT
      s.trace_id,
      s.span_id,
      s.span_session_id,
      s.span_session_key,
      e.event_session_id,
      e.event_session_key,
      s.agent_id,
      s.harness,
      'agent_day_project' AS link_method,
      'weak' AS link_confidence,
      4 AS link_rank,
      count(DISTINCT e.event_session_id) OVER (
        PARTITION BY s.trace_id, s.span_id
      ) AS fallback_candidate_count
    FROM span_rows s
    JOIN event_sessions e
      ON s.agent_id = e.event_agent_id
     AND s.repo_owner = e.repo_owner
     AND s.repo_name = e.repo_name
     AND s.start_time BETWEEN e.first_event_at - INTERVAL 1 DAY
                          AND e.last_event_at + INTERVAL 1 DAY
    WHERE s.agent_id IS NOT NULL
      AND s.repo_owner IS NOT NULL
      AND s.repo_name IS NOT NULL
  )
  WHERE fallback_candidate_count = 1
),
best_candidates AS (
  SELECT * EXCLUDE (link_rank, rn)
  FROM (
    SELECT
      candidates.*,
      row_number() OVER (
        PARTITION BY trace_id, span_id
        ORDER BY link_rank, event_session_id
      ) AS rn
    FROM candidates
  )
  WHERE rn = 1
)
SELECT * FROM best_candidates
UNION ALL
SELECT
  s.trace_id,
  s.span_id,
  s.span_session_id,
  s.span_session_key,
  NULL AS event_session_id,
  NULL AS event_session_key,
  s.agent_id,
  s.harness,
  'unmatched' AS link_method,
  'none' AS link_confidence
FROM span_rows s
WHERE NOT EXISTS (
  SELECT 1
  FROM best_candidates b
  WHERE b.trace_id IS NOT DISTINCT FROM s.trace_id
    AND b.span_id = s.span_id
);
"""


def _ensure_seed_parquet(parquet_dir: Path) -> None:
    """DuckDB's read_parquet errors on an empty glob.  Drop an empty
    parquet file in each subdir so views can be created at bootstrap time.

    The seed schema includes the columns referenced by the sessions and
    active_sessions views so DuckDB can resolve those columns at view-creation
    time (DuckDB 1.x validates column references against the Parquet schema
    when the view is created, not at query time).

    agent_events uses hive partitioning (date=/agent_id=), so its seed must
    live inside a hive-partitioned path to avoid a BinderException when real
    hive-partitioned files appear alongside a flat seed file.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    # agent_events seed: placed in a hive-partitioned directory so DuckDB
    # doesn't raise a partition mismatch when real data arrives.  Also
    # includes dedup_key so _existing_dedup_keys() can query it immediately,
    # and id so queries like WHERE id = '...' bind correctly at view-creation.
    ae_seed_schema = pa.schema(
        [
            ("id", pa.string()),
            ("session_id", pa.string()),
            ("agent_id", pa.string()),
            ("task_id", pa.string()),
            ("timestamp", pa.timestamp("us", tz="UTC")),
            ("event_type", pa.string()),
            ("role", pa.string()),
            ("content", pa.string()),
            ("repo_owner", pa.string()),
            ("repo_name", pa.string()),
            ("branch", pa.string()),
            ("principal_id", pa.string()),
            ("dedup_key", pa.string()),
            ("raw_data", pa.string()),
        ]
    )
    ae_empty = pa.table(
        {
            "id": pa.array([], type=pa.string()),
            "session_id": pa.array([], type=pa.string()),
            "agent_id": pa.array([], type=pa.string()),
            "task_id": pa.array([], type=pa.string()),
            "timestamp": pa.array([], type=pa.timestamp("us", tz="UTC")),
            "event_type": pa.array([], type=pa.string()),
            "role": pa.array([], type=pa.string()),
            "content": pa.array([], type=pa.string()),
            "repo_owner": pa.array([], type=pa.string()),
            "repo_name": pa.array([], type=pa.string()),
            "branch": pa.array([], type=pa.string()),
            "principal_id": pa.array([], type=pa.string()),
            "dedup_key": pa.array([], type=pa.string()),
            "raw_data": pa.array([], type=pa.string()),
        },
        schema=ae_seed_schema,
    )
    # Use a hive-compatible path so the glob picks it up without partition conflict.
    ae_seed_dir = parquet_dir / "agent_events" / "date=_seed" / "agent_id=_seed"
    ae_seed_dir.mkdir(parents=True, exist_ok=True)
    ae_seed_file = ae_seed_dir / "empty.parquet"
    if not ae_seed_file.exists():
        pq.write_table(ae_empty, ae_seed_file)

    # Spans seed — hive-partitioned (date=) with the columns the OTLP ingest writes.
    spans_seed_schema = pa.schema(
        [
            ("trace_id", pa.string()),
            ("span_id", pa.string()),
            ("parent_span_id", pa.string()),
            ("name", pa.string()),
            ("service_name", pa.string()),
            ("start_time", pa.timestamp("us", tz="UTC")),
            ("end_time", pa.timestamp("us", tz="UTC")),
            ("duration_ms", pa.float64()),
            ("harness", pa.string()),
            ("session_id", pa.string()),
            ("session_key", pa.string()),
            ("task_id", pa.string()),
            ("agent_id", pa.string()),
            ("agent_type", pa.string()),
            ("agent_model", pa.string()),
            ("associated_with", pa.string()),
            ("activity_type", pa.string()),
            ("parent_session_id", pa.string()),
            ("project", pa.string()),
            ("cwd", pa.string()),
            ("repository", pa.string()),
            ("task_label", pa.string()),
            ("llm_provider", pa.string()),
            ("llm_model", pa.string()),
            ("stop_reason", pa.string()),
            ("repo_owner", pa.string()),
            ("repo_name", pa.string()),
            ("branch", pa.string()),
            ("principal_id", pa.string()),
            ("routing_provider", pa.string()),
            ("routing_model", pa.string()),
            ("routing_reason", pa.string()),
            ("redaction_level", pa.string()),
            ("sensitivity", pa.string()),
            ("prompt_preview", pa.string()),
            ("response_preview", pa.string()),
            ("preview_truncated", pa.bool_()),
            ("preview_bytes", pa.int64()),
            ("cost_usd", pa.float64()),
            ("prompt_tokens", pa.int64()),
            ("completion_tokens", pa.int64()),
            ("total_tokens", pa.int64()),
            ("cache_read_tokens", pa.int64()),
            ("cache_write_tokens", pa.int64()),
            ("attributes_json", pa.string()),
            ("raw_object_uri", pa.string()),
            ("dedup_key", pa.string()),
        ]
    )
    spans_empty = pa.table(
        {field.name: pa.array([], type=field.type) for field in spans_seed_schema},
        schema=spans_seed_schema,
    )
    spans_seed_dir = parquet_dir / "spans" / "date=_seed"
    spans_seed_dir.mkdir(parents=True, exist_ok=True)
    spans_seed_file = spans_seed_dir / "empty.parquet"
    # Always refresh the empty seed so newly added nullable span columns are
    # visible even before a real partition containing them has been written.
    pq.write_table(spans_empty, spans_seed_file)

    # Minimal shared schema for the remaining flat (non-hive) subdirs.
    flat_seed_schema = pa.schema(
        [
            ("session_id", pa.string()),
            ("agent_id", pa.string()),
            ("task_id", pa.string()),
            ("timestamp", pa.timestamp("us", tz="UTC")),
        ]
    )
    flat_empty = pa.table(
        {
            "session_id": pa.array([], type=pa.string()),
            "agent_id": pa.array([], type=pa.string()),
            "task_id": pa.array([], type=pa.string()),
            "timestamp": pa.array([], type=pa.timestamp("us", tz="UTC")),
        },
        schema=flat_seed_schema,
    )
    for sub in ("pr_events", "routing"):
        seed_dir = parquet_dir / sub / "_seed"
        seed_dir.mkdir(parents=True, exist_ok=True)
        seed_file = seed_dir / "empty.parquet"
        if not seed_file.exists():
            pq.write_table(flat_empty, seed_file)

    # Provider usage snapshots are flattened to one row per reported window.
    # A typed seed lets a fresh lakehouse expose the view before the first
    # provider refresh writes an observation.
    from drover.server.providers.types import provider_snapshot_schema

    provider_seed_dir = parquet_dir / "provider_usage_snapshots" / "_seed"
    provider_seed_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([], schema=provider_snapshot_schema()),
        provider_seed_dir / "empty.parquet",
    )


def _ensure_table_columns(
    con: duckdb.DuckDBPyConnection, table: str, columns: dict[str, str]
) -> None:
    """Add newly introduced nullable columns to existing DuckDB tables."""
    existing = {
        row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()
    }
    for name, ddl_type in columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")


def bootstrap_control_plane_store(duckdb_path: Path) -> Path:
    """Create the control plane's own database and its tables. Idempotent.

    Issue #95. These tables used to be created inside ``drover.duckdb``, which
    is what put every ``/harness*`` read on the same DuckDB instance -- one
    scheduler, one buffer manager, one ``memory_limit`` -- as the parquet scans
    that repeatedly wedged the hub.
    """
    registry_path = control_plane_path(duckdb_path)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with control_plane_connection(registry_path) as con:
        con.execute(_LIVE_SESSION_RECAPS_DDL)
        con.execute(_LIVE_RECAP_JOBS_DDL)
        bootstrap_harness_tables(con)
    return registry_path


def migrate_control_plane_tables(
    con: duckdb.DuckDBPyConnection, duckdb_path: Path
) -> dict[str, int]:
    """Copy pre-split control-plane rows into the control-plane store.

    Runs on every start against a live fleet, so:

    * **Idempotent.** Rows are inserted only where the primary key is absent
      from the destination, so a second run copies nothing.
    * **Never destructive.** The destination is authoritative once the first
      run has completed -- an existing row is left exactly as it is, so a
      restart cannot resurrect a stale ``status='running'`` over a session the
      control plane has since completed.
    * **Verified.** Every source key must exist in the destination afterwards
      or the migration raises, rather than leaving a hub serving a fleet that
      silently lost rows.
    * **Non-lossy.** The pre-split tables are left in ``drover.duckdb``
      untouched. They cost disk and nothing else, and they are what makes a
      rollback of this change a restart instead of a data-recovery exercise --
      #104 shipped for this issue and did not hold, so going backwards has to
      stay cheap. ``attached_control_plane_snapshot`` shadows them so no reader
      can answer from them in the meantime. A later cleanup drops them.

    Copies from ``con`` (already open on the analytical store) into an attached
    control-plane store, rather than the other way round: DuckDB will not let a
    second instance in this process open a file this one already holds, and at
    bootstrap time ``con`` is the only open handle.
    """
    registry_path = control_plane_path(duckdb_path)
    legacy = {
        str(row[0])
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_type = 'BASE TABLE'"
        ).fetchall()
    } & set(CONTROL_PLANE_TABLES)
    if not legacy:
        return {}

    alias = "drover_control_plane_migration"
    try:
        con.execute(f"ATTACH {sql_path_literal(registry_path)} AS {alias}")
    except duckdb.Error as exc:
        # Another process holds the control-plane store. Startup carries on --
        # `bootstrap_harnessd_schema` sets the same precedent -- and the next
        # start retries, because this is idempotent.
        log.warning(
            "control-plane migration deferred; %s is not attachable now (%s)",
            registry_path,
            exc,
        )
        return {}

    copied: dict[str, int] = {}
    try:
        for table in CONTROL_PLANE_TABLES:
            if table not in legacy:
                continue
            key = CONTROL_PLANE_PRIMARY_KEYS[table]
            missing_sql = (
                f"SELECT count(*) FROM main.{table} src "
                f"WHERE NOT EXISTS (SELECT 1 FROM {alias}.{table} dst "
                f"WHERE dst.{key} = src.{key})"
            )
            pending = int(con.execute(missing_sql).fetchone()[0])
            if pending:
                con.execute(
                    f"INSERT INTO {alias}.{table} BY NAME "
                    f"SELECT src.* FROM main.{table} src "
                    f"WHERE NOT EXISTS (SELECT 1 FROM {alias}.{table} dst "
                    f"WHERE dst.{key} = src.{key})"
                )
                remaining = int(con.execute(missing_sql).fetchone()[0])
                if remaining:
                    raise RuntimeError(
                        f"control-plane migration left {remaining} {table} row(s) "
                        f"behind in {duckdb_path}"
                    )
            copied[table] = pending
    finally:
        con.execute(f"DETACH {alias}")
    if any(copied.values()):
        log.info("migrated control-plane rows into %s: %s", registry_path, copied)
    return copied


def bootstrap(*, parquet_dir: Path, duckdb_path: Path) -> None:
    """Create directories, tables, and views.  Idempotent."""
    parquet_dir = Path(parquet_dir)
    duckdb_path = Path(duckdb_path)
    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    parquet_dir.mkdir(parents=True, exist_ok=True)
    for sub in PARQUET_SUBDIRS:
        (parquet_dir / sub).mkdir(parents=True, exist_ok=True)

    _ensure_seed_parquet(parquet_dir)

    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(_TASKS_DDL)
        con.execute(_SPAN_PARTITION_ACTIVITY_DDL)
        con.execute(_SESSION_SUMMARIES_DDL)
        con.execute(_SUMMARIZE_JOBS_DDL)
        _ensure_table_columns(con, "summarize_jobs", _SUMMARIZE_JOBS_COLUMNS)
        con.execute(_PROJECT_BRIEFS_DDL)
        con.execute(_BRIEF_JOBS_DDL)
        _ensure_table_columns(con, "brief_jobs", _BRIEF_JOBS_COLUMNS)
        con.execute(_SESSION_EMBEDDINGS_DDL)
        con.execute(_EMBED_JOBS_DDL)
        _ensure_table_columns(con, "embed_jobs", _EMBED_JOBS_COLUMNS)
        con.execute(_SPAN_EMBEDDINGS_DDL)
        _ensure_table_columns(con, "span_embeddings", _SPAN_EMBEDDINGS_COLUMNS)
        con.execute(_SPAN_EMBED_JOBS_DDL)
        con.execute(_ACTIVE_SESSION_BRIEFS_DDL)
        con.execute(_DECISIONS_DDL)
        con.execute(_CONTEXT_CONTAINERS_DDL)
        con.execute(_CURATED_CONTEXT_RECORDS_DDL)
        con.execute(_CURATED_CONTEXT_PROVENANCE_DDL)
        con.execute(_PIPELINE_RECEIPTS_DDL)
        con.execute(_PIPELINE_JOBS_DDL)
        con.execute(_PIPELINE_JOB_ATTEMPTS_DDL)
        con.execute(_PIPELINE_ARTIFACTS_DDL)
        con.execute(_PROVIDER_CONNECTIONS_DDL)
        con.execute(_ADVISORY_FINDINGS_DDL)
        con.execute(_ADVISORY_OCCURRENCES_DDL)
        bootstrap_control_plane_store(duckdb_path)
        migrate_control_plane_tables(con, duckdb_path)
        con.execute(_agent_events_view(parquet_dir))
        con.execute(_spans_view(parquet_dir))
        con.execute(_span_query_macros(parquet_dir))
        _refresh_span_partition_activity(con)
        con.execute(_SESSION_LINKS_VIEW)
        con.execute(_OPENCLAW_SPAN_LINKS_VIEW)
        con.execute(_pr_events_view(parquet_dir))
        con.execute(_routing_view(parquet_dir))
        con.execute(_provider_usage_snapshots_view(parquet_dir))
        con.execute(_SESSIONS_VIEW)
        con.execute(_ACTIVE_SESSIONS_VIEW)
    finally:
        con.close()
