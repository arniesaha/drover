"""Versioned PostgreSQL DDL for the central serving store."""

from __future__ import annotations

from typing import Any

_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE IF NOT EXISTS harness_hosts (
              host_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, kind TEXT NOT NULL,
              local_url TEXT, tailscale_url TEXT, connection_kind TEXT,
              status TEXT NOT NULL, capabilities_json TEXT NOT NULL,
              model_catalogs_json TEXT NOT NULL DEFAULT '{}', agent_version TEXT,
              update_json TEXT, last_seen_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS harness_sessions (
              session_id TEXT PRIMARY KEY, host_id TEXT NOT NULL, harness TEXT NOT NULL,
              repo_owner TEXT, repo_name TEXT, branch TEXT, cwd TEXT, command TEXT NOT NULL,
              status TEXT NOT NULL, started_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              ended_at TIMESTAMPTZ, last_error TEXT, summary_session_id TEXT,
              native_session_id TEXT, native_resume_label TEXT, source_session_id TEXT,
              handoff_mode TEXT, mode TEXT, awaiting TEXT, last_activity TIMESTAMPTZ,
              permission_mode TEXT, model TEXT, thinking_effort TEXT,
              recap_reconcile_needed BOOLEAN DEFAULT FALSE, client_session_id TEXT
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS harness_sessions_client_key ON harness_sessions (client_session_id)",
            """
            CREATE TABLE IF NOT EXISTS harness_events (
              event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, event_type TEXT NOT NULL,
              normalized_type TEXT, normalized_source TEXT, content_preview TEXT,
              payload_json TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              seq INTEGER, dedup_key TEXT
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS harness_events_dedup_key ON harness_events (dedup_key) WHERE dedup_key IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS harness_events_session_order ON harness_events (session_id, seq, created_at, event_id)",
            """
            CREATE TABLE IF NOT EXISTS live_session_recaps (
              session_id TEXT PRIMARY KEY, recap_text TEXT NOT NULL, source_seq INTEGER NOT NULL,
              generator_model TEXT, generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS live_recap_jobs (
              session_id TEXT PRIMARY KEY, desired_source_seq INTEGER NOT NULL, status TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
              enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              next_run_at TIMESTAMPTZ, stream_publish_needed BOOLEAN NOT NULL DEFAULT FALSE
            )
            """,
            "CREATE INDEX IF NOT EXISTS live_recap_jobs_claim ON live_recap_jobs (status, next_run_at, enqueued_at)",
            """
            CREATE TABLE IF NOT EXISTS advisory_findings (
              finding_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, analyzer_id TEXT NOT NULL,
              rule_id TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
              analyzer_class TEXT NOT NULL, severity TEXT NOT NULL, confidence TEXT NOT NULL,
              title TEXT NOT NULL, impact TEXT NOT NULL, remediation_json TEXT NOT NULL,
              state TEXT NOT NULL, dismissal_reason TEXT, first_seen_at TIMESTAMPTZ NOT NULL,
              last_seen_at TIMESTAMPTZ NOT NULL, resolved_at TIMESTAMPTZ, dismissed_at TIMESTAMPTZ,
              regressed_at TIMESTAMPTZ, evaluated_content_hash TEXT,
              regression_count INTEGER NOT NULL DEFAULT 0, latest_run_id TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_advisory_findings_list ON advisory_findings (state, severity, last_seen_at)",
            """
            CREATE TABLE IF NOT EXISTS advisory_occurrences (
              occurrence_id TEXT PRIMARY KEY, finding_id TEXT NOT NULL, run_id TEXT NOT NULL,
              outcome TEXT NOT NULL, observed_at TIMESTAMPTZ NOT NULL, source_ref TEXT,
              evidence_json TEXT, excerpt TEXT, evidence_hash TEXT,
              recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_advisory_occurrences_finding ON advisory_occurrences (finding_id, outcome, recorded_at)",
            """
            CREATE TABLE IF NOT EXISTS session_usage (
              session_id TEXT PRIMARY KEY, host_id TEXT, harness TEXT, input_tokens BIGINT,
              output_tokens BIGINT, cache_read_tokens BIGINT, cache_write_tokens BIGINT,
              reasoning_tokens BIGINT, turn_count INTEGER NOT NULL DEFAULT 0,
              exact BOOLEAN NOT NULL DEFAULT TRUE, source TEXT NOT NULL, source_seq INTEGER NOT NULL,
              source_event_count INTEGER NOT NULL, observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS session_usage_sources (
              source_usage_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, source TEXT NOT NULL,
              host_id TEXT, harness TEXT, input_tokens BIGINT, output_tokens BIGINT,
              cache_read_tokens BIGINT, cache_write_tokens BIGINT, reasoning_tokens BIGINT,
              turn_count INTEGER NOT NULL DEFAULT 0, exact BOOLEAN NOT NULL DEFAULT TRUE,
              usage_observed BOOLEAN NOT NULL DEFAULT FALSE, source_seq INTEGER NOT NULL,
              source_event_count INTEGER NOT NULL, observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              UNIQUE (session_id, source)
            )
            """,
            "CREATE INDEX IF NOT EXISTS session_usage_sources_session ON session_usage_sources (session_id, source)",
            """
            CREATE TABLE IF NOT EXISTS native_usage_partition_totals (
              native_usage_partition_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
              partition_date TEXT NOT NULL, input_tokens BIGINT, output_tokens BIGINT,
              cache_read_tokens BIGINT, cache_write_tokens BIGINT, reasoning_tokens BIGINT,
              turn_count INTEGER NOT NULL, event_count INTEGER NOT NULL, exact BOOLEAN NOT NULL,
              observed_at TIMESTAMPTZ NOT NULL, UNIQUE (session_id, partition_date)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS native_usage_partition_watermarks (
              partition_date TEXT PRIMARY KEY, source_activity_at TIMESTAMPTZ NOT NULL,
              rolled_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS control_server_identity (
              identity_key TEXT PRIMARY KEY, identity_value TEXT NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS control_credentials (
              credential_id TEXT PRIMARY KEY, scope TEXT NOT NULL, label TEXT NOT NULL,
              verifier TEXT NOT NULL UNIQUE, created_at TIMESTAMPTZ NOT NULL,
              host_id TEXT, last_used_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ,
              apns_token TEXT, apns_environment TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS control_credentials_active_verifier ON control_credentials (verifier) WHERE revoked_at IS NULL",
        ),
    ),
    (
        2,
        (
            # Keep the legacy column readable for a manually restored v1 row,
            # but new PostgreSQL writes put the full envelope in the side
            # table below.  That lets the hot event index stay narrow without
            # breaking an interrupted upgrade half way through its first boot.
            "ALTER TABLE harness_events ALTER COLUMN payload_json DROP NOT NULL",
            """
            CREATE TABLE IF NOT EXISTS harness_event_payloads (
              event_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL,
              payload_sha256 TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            INSERT INTO harness_event_payloads (event_id, payload_json)
            SELECT event_id, payload_json FROM harness_events
             WHERE payload_json IS NOT NULL
            ON CONFLICT (event_id) DO NOTHING
            """,
            """
            UPDATE harness_events
               SET payload_json = NULL
             WHERE payload_json IS NOT NULL
               AND EXISTS (
                 SELECT 1 FROM harness_event_payloads p
                  WHERE p.event_id = harness_events.event_id
               )
            """,
            """
            CREATE TABLE IF NOT EXISTS harness_session_previews (
              session_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
              content_preview TEXT NOT NULL, event_type TEXT NOT NULL,
              event_priority INTEGER NOT NULL, seq INTEGER,
              event_created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS harness_session_previews_order ON harness_session_previews (session_id, event_priority, seq, event_created_at, event_id)",
            """
            CREATE TABLE IF NOT EXISTS control_outbox_events (
              event_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'pending',
              batch_id TEXT, lease_owner TEXT, lease_until TIMESTAMPTZ,
              committed_at TIMESTAMPTZ NOT NULL DEFAULT now(), published_at TIMESTAMPTZ,
              acknowledged_at TIMESTAMPTZ
            )
            """,
            "CREATE INDEX IF NOT EXISTS control_outbox_events_claim ON control_outbox_events (state, lease_until, committed_at, event_id)",
            """
            CREATE TABLE IF NOT EXISTS control_outbox_batches (
              batch_id TEXT PRIMARY KEY, state TEXT NOT NULL,
              lease_owner TEXT, lease_until TIMESTAMPTZ, member_count INTEGER NOT NULL,
              content_sha256 TEXT, archive_path TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              published_at TIMESTAMPTZ, acknowledged_at TIMESTAMPTZ
            )
            """,
            "CREATE INDEX IF NOT EXISTS control_outbox_batches_visibility ON control_outbox_batches (state, published_at, batch_id)",
            """
            CREATE TABLE IF NOT EXISTS control_outbox_batch_events (
              batch_id TEXT NOT NULL, event_id TEXT NOT NULL,
              ordinal INTEGER NOT NULL, PRIMARY KEY (batch_id, event_id),
              UNIQUE (batch_id, ordinal)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS harness_event_archives (
              event_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
              payload_sha256 TEXT NOT NULL, verified_at TIMESTAMPTZ NOT NULL,
              payload_pruned_at TIMESTAMPTZ
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS control_store_initialization (
              singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
              state TEXT NOT NULL, mode TEXT NOT NULL, source_fingerprint TEXT,
              source_timezone TEXT, verified_at TIMESTAMPTZ, details_json TEXT NOT NULL DEFAULT '{}',
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
        ),
    ),
    (
        3,
        (
            # This state is deliberately central rather than a shared config
            # sidecar: API heartbeats and worker mutations can run on different
            # machines, while hosts must always see the current epoch.
            """
            CREATE TABLE IF NOT EXISTS control_content_consent (
              singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
              enabled BOOLEAN NOT NULL, epoch BIGINT NOT NULL CHECK (epoch >= 0),
              backend TEXT NOT NULL, external_disclosure_accepted BOOLEAN NOT NULL,
              migrated_from_legacy BOOLEAN NOT NULL DEFAULT FALSE,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
        ),
    ),
    (
        4,
        (
            # Apple's permanent rejection of a device registration, so the
            # registration route can keep refusing the same dead token.
            "ALTER TABLE control_credentials ADD COLUMN IF NOT EXISTS apns_failure_reason TEXT",
            "ALTER TABLE control_credentials ADD COLUMN IF NOT EXISTS apns_failed_at TIMESTAMPTZ",
            "ALTER TABLE control_credentials ADD COLUMN IF NOT EXISTS apns_failed_fingerprint TEXT",
        ),
    ),
    (
        5,
        (
            # Optional delegation link recorded at launch, so the session
            # graph never has to infer parentage from traces (#473).
            "ALTER TABLE harness_sessions ADD COLUMN IF NOT EXISTS parent_session_id TEXT",
            "CREATE INDEX IF NOT EXISTS harness_sessions_parent ON harness_sessions (parent_session_id) WHERE parent_session_id IS NOT NULL",
        ),
    ),
    (
        6,
        (
            # Self-contained host lifecycle migration; safe to renumber.
            "ALTER TABLE harness_hosts ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ",
            "ALTER TABLE harness_hosts ADD COLUMN IF NOT EXISTS retired_reason TEXT",
        ),
    ),
    (
        7,
        (
            # #480: the one job ledger and the derived memory it produces.
            # Rebuild, don't migrate derived rows. Preserve legacy tables;
            # removal is an explicit, separately authorized maintenance step.
            """
            CREATE TABLE pipeline_jobs (
              job_id TEXT PRIMARY KEY,
              job_kind TEXT NOT NULL CHECK (job_kind IN
                ('summarize_session', 'embed_session', 'brief_project', 'recap_session')),
              subject_key TEXT NOT NULL,
              source_version TEXT NOT NULL DEFAULT '',
              payload_json TEXT NOT NULL DEFAULT '{}',
              status TEXT NOT NULL CHECK (status IN
                ('pending', 'running', 'retry_wait', 'succeeded',
                 'dead_lettered', 'quarantined', 'superseded')),
              priority INTEGER NOT NULL DEFAULT 0,
              claims INTEGER NOT NULL DEFAULT 0 CHECK (claims >= 0),
              failures INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
              max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
              next_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              lease_owner TEXT, lease_token TEXT, lease_expires_at TIMESTAMPTZ,
              last_error TEXT, error_category TEXT, disposition_reason TEXT,
              enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              CHECK ((status = 'running') = (lease_token IS NOT NULL)),
              CHECK (status = 'running' OR lease_expires_at IS NULL),
              CHECK (status NOT IN ('dead_lettered', 'quarantined', 'superseded')
                     OR disposition_reason IS NOT NULL)
            )
            """,
            # At most one live job per subject; enqueue conflicts on this.
            """
            CREATE UNIQUE INDEX pipeline_jobs_one_live ON pipeline_jobs (job_kind, subject_key)
             WHERE status IN ('pending', 'running', 'retry_wait')
            """,
            # The due-work claim: SELECT ... FOR UPDATE SKIP LOCKED walks this.
            """
            CREATE INDEX pipeline_jobs_due ON pipeline_jobs
              (job_kind, priority DESC, next_run_at, enqueued_at)
             WHERE status IN ('pending', 'retry_wait')
            """,
            """
            CREATE INDEX pipeline_jobs_leases ON pipeline_jobs (job_kind, lease_expires_at)
             WHERE status = 'running'
            """,
            """
            CREATE INDEX pipeline_jobs_failed ON pipeline_jobs (job_kind, updated_at DESC)
             WHERE status IN ('retry_wait', 'dead_lettered', 'quarantined')
            """,
            """
            CREATE INDEX pipeline_jobs_succeeded ON pipeline_jobs (job_kind, finished_at DESC)
             WHERE status = 'succeeded'
            """,
            "CREATE INDEX pipeline_jobs_subject ON pipeline_jobs (job_kind, subject_key, enqueued_at DESC)",
            """
            CREATE TABLE pipeline_job_attempts (
              attempt_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
              job_id TEXT NOT NULL REFERENCES pipeline_jobs (job_id) ON DELETE CASCADE,
              attempt_no INTEGER NOT NULL, worker_id TEXT, lease_token TEXT NOT NULL,
              started_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ,
              result TEXT CHECK (result IN
                ('succeeded', 'retryable_failed', 'terminal_failed', 'lease_expired',
                 'released', 'superseded')),
              error_category TEXT, error_message TEXT, metrics_json TEXT,
              UNIQUE (job_id, attempt_no)
            )
            """,
            "CREATE INDEX pipeline_job_attempts_open ON pipeline_job_attempts (job_id, lease_token) WHERE finished_at IS NULL",
            # One memory row per session. The live recap and the final summary
            # are two phases of it; `phase` says which the latest reader shows.
            """
            CREATE TABLE session_memory (
              session_id TEXT PRIMARY KEY,
              phase TEXT NOT NULL CHECK (phase IN ('live', 'final')),
              task_id TEXT, agent_id TEXT, project_key TEXT, ended_at TIMESTAMPTZ,
              summary_md TEXT, next_steps_md TEXT,
              files_touched TEXT[] NOT NULL DEFAULT '{}',
              tools_used JSONB NOT NULL DEFAULT '{}'::jsonb,
              open_questions TEXT[] NOT NULL DEFAULT '{}',
              last_user_prompt TEXT, last_assistant TEXT, summary_status TEXT,
              summary_source_version TEXT, summary_model TEXT,
              summary_generated_at TIMESTAMPTZ,
              recap_text TEXT, recap_source_seq INTEGER, recap_model TEXT,
              recap_generated_at TIMESTAMPTZ,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              CHECK ((phase = 'final') = (summary_generated_at IS NOT NULL)),
              CHECK ((recap_text IS NULL) = (recap_source_seq IS NULL))
            )
            """,
            """
            CREATE INDEX session_memory_final_recent ON session_memory (ended_at DESC NULLS LAST)
             WHERE phase = 'final'
            """,
            "CREATE INDEX session_memory_project ON session_memory (project_key, ended_at DESC NULLS LAST)",
            "CREATE INDEX session_memory_task ON session_memory (task_id)",
            """
            CREATE TABLE project_briefs (
              project_key TEXT PRIMARY KEY, repo_owner TEXT NOT NULL, repo_name TEXT NOT NULL,
              brief_md TEXT NOT NULL, recent_themes_md TEXT,
              key_files TEXT[] NOT NULL DEFAULT '{}',
              open_questions TEXT[] NOT NULL DEFAULT '{}',
              next_steps_md TEXT, session_count INTEGER NOT NULL DEFAULT 0,
              last_activity_at TIMESTAMPTZ, source_session_id TEXT,
              source_version TEXT, generator_model TEXT,
              generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
        ),
    ),
    (
        9,
        (
            # Session history (docs/design/session-history.md). The keyset
            # order is (activity_at DESC, session_id DESC), where activity_at
            # is COALESCE(last_activity, updated_at): updated_at is NOT NULL,
            # so every row has a sort key. Queries must repeat the expression
            # verbatim for the planner to use these indexes.
            """
            CREATE INDEX IF NOT EXISTS harness_sessions_history
              ON harness_sessions ((COALESCE(last_activity, updated_at)), session_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS harness_sessions_history_host
              ON harness_sessions (host_id, (COALESCE(last_activity, updated_at)), session_id)
            """,
            # session_memory may be keyed by the harness id or by its native id.
            """
            CREATE INDEX IF NOT EXISTS harness_sessions_native
              ON harness_sessions (native_session_id) WHERE native_session_id IS NOT NULL
            """,
            # Title, repo and summary live in three tables, so one GIN index
            # needs one denormalized document per session. Triggers keep it in
            # the writer's transaction; nothing has to remember to refresh it.
            """
            CREATE TABLE IF NOT EXISTS session_search (
              session_id TEXT PRIMARY KEY,
              document TSVECTOR NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS session_search_document ON session_search USING GIN (document)",
            # SET search_path FROM CURRENT pins the bootstrap schema, so a
            # trigger fired from any session resolves this schema's tables.
            """
            CREATE OR REPLACE FUNCTION drover_refresh_session_search(sid TEXT)
            RETURNS VOID LANGUAGE plpgsql SET search_path FROM CURRENT AS $fn$
            BEGIN
              INSERT INTO session_search (session_id, document, updated_at)
              SELECT s.session_id,
                     setweight(to_tsvector('simple', left(coalesce(p.content_preview, ''), 1000)), 'A')
                  || setweight(to_tsvector('simple', concat_ws(' ',
                       s.repo_owner, s.repo_name, replace(coalesce(s.repo_name, ''), '-', ' '),
                       s.branch, s.harness)), 'B')
                  || setweight(to_tsvector('simple', left(coalesce(
                       (SELECT coalesce(m.summary_md, m.recap_text)
                          FROM session_memory m
                         WHERE m.session_id IN (s.session_id, s.native_session_id)
                         ORDER BY (m.summary_md IS NOT NULL) DESC,
                                  (m.session_id = s.session_id) DESC
                         LIMIT 1), ''), 8000)), 'C'),
                     now()
                FROM harness_sessions s
                LEFT JOIN harness_session_previews p ON p.session_id = s.session_id
               WHERE s.session_id = sid
              ON CONFLICT (session_id) DO UPDATE
                SET document = excluded.document, updated_at = excluded.updated_at;
            EXCEPTION WHEN OTHERS THEN
              -- A stale search document is recoverable; a failed session or
              -- event write is not. Never let the index veto the writer.
              RAISE WARNING 'session_search refresh failed for %: %', sid, SQLERRM;
            END
            $fn$
            """,
            """
            CREATE OR REPLACE FUNCTION drover_session_search_from_session()
            RETURNS TRIGGER LANGUAGE plpgsql SET search_path FROM CURRENT AS $fn$
            BEGIN
              IF TG_OP = 'DELETE' THEN
                DELETE FROM session_search WHERE session_id = OLD.session_id;
                RETURN OLD;
              END IF;
              PERFORM drover_refresh_session_search(NEW.session_id);
              RETURN NEW;
            END
            $fn$
            """,
            """
            CREATE OR REPLACE FUNCTION drover_session_search_from_preview()
            RETURNS TRIGGER LANGUAGE plpgsql SET search_path FROM CURRENT AS $fn$
            BEGIN
              PERFORM drover_refresh_session_search(
                CASE WHEN TG_OP = 'DELETE' THEN OLD.session_id ELSE NEW.session_id END);
              RETURN NULL;
            END
            $fn$
            """,
            # A memory row may be keyed by the native id; refresh every harness
            # session it can belong to.
            """
            CREATE OR REPLACE FUNCTION drover_session_search_from_memory()
            RETURNS TRIGGER LANGUAGE plpgsql SET search_path FROM CURRENT AS $fn$
            DECLARE
              key TEXT := CASE WHEN TG_OP = 'DELETE' THEN OLD.session_id ELSE NEW.session_id END;
              harness_id TEXT;
            BEGIN
              FOR harness_id IN
                SELECT session_id FROM harness_sessions WHERE session_id = key
                UNION
                SELECT session_id FROM harness_sessions WHERE native_session_id = key
              LOOP
                PERFORM drover_refresh_session_search(harness_id);
              END LOOP;
              RETURN NULL;
            END
            $fn$
            """,
            # Only the indexed columns: last_activity moves on every event and
            # must not rewrite the search document each time.
            "DROP TRIGGER IF EXISTS session_search_session_write ON harness_sessions",
            """
            CREATE TRIGGER session_search_session_write
              AFTER INSERT OR UPDATE OF repo_owner, repo_name, branch, harness, native_session_id
              ON harness_sessions FOR EACH ROW
              EXECUTE FUNCTION drover_session_search_from_session()
            """,
            "DROP TRIGGER IF EXISTS session_search_session_delete ON harness_sessions",
            """
            CREATE TRIGGER session_search_session_delete
              AFTER DELETE ON harness_sessions FOR EACH ROW
              EXECUTE FUNCTION drover_session_search_from_session()
            """,
            "DROP TRIGGER IF EXISTS session_search_preview_write ON harness_session_previews",
            """
            CREATE TRIGGER session_search_preview_write
              AFTER INSERT OR UPDATE OF content_preview OR DELETE
              ON harness_session_previews FOR EACH ROW
              EXECUTE FUNCTION drover_session_search_from_preview()
            """,
            "DROP TRIGGER IF EXISTS session_search_memory_write ON session_memory",
            """
            CREATE TRIGGER session_search_memory_write
              AFTER INSERT OR UPDATE OF summary_md, recap_text OR DELETE
              ON session_memory FOR EACH ROW
              EXECUTE FUNCTION drover_session_search_from_memory()
            """,
            # Backfill what already exists (279 sessions on the live hub).
            "SELECT drover_refresh_session_search(session_id) FROM harness_sessions",
        ),
    ),
)

#: Session embeddings need pgvector, which is a server-side extension the
#: control store may not have. They are a separate, conditional migration so a
#: server without pgvector still bootstraps everything else -- and readiness
#: then fails loudly instead of the embedding path silently falling back.
VECTOR_MIGRATION = 8
EMBEDDING_DIM = 768


def _vector_migration_statements(extension_schema: str) -> tuple[str, ...]:
    from psycopg import sql

    vector_type = sql.SQL("{}.vector({})").format(
        sql.Identifier(extension_schema), sql.Literal(EMBEDDING_DIM)
    )
    return (
        sql.SQL("""
            CREATE TABLE IF NOT EXISTS session_embeddings (
              session_id TEXT PRIMARY KEY,
              embedding {} NOT NULL,
              model TEXT NOT NULL,
              dim INTEGER NOT NULL CHECK (dim = {}),
              source_version TEXT NOT NULL DEFAULT '',
              embedded_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """).format(vector_type, sql.Literal(EMBEDDING_DIM)),
        sql.SQL(
            "CREATE INDEX IF NOT EXISTS session_embeddings_model ON session_embeddings (model)"
        ),
    )


def vector_extension_schema(con: Any) -> str | None:
    """Schema pgvector is installed in for this database, or None."""
    row = con.execute(
        "SELECT extnamespace::regnamespace::text FROM pg_extension WHERE extname = 'vector'"
    ).fetchone()
    return None if row is None else str(row[0]).strip('"')


def _apply_vector_migration(con: Any, raw: Any) -> None:
    """Create session_embeddings when pgvector can be enabled; otherwise skip.

    Runs inside the bootstrap transaction under a savepoint, so a missing or
    unprivileged extension rolls back only this step. It is retried on every
    bootstrap until it succeeds. The extension is pinned to ``public`` so a
    per-schema drop (tests, a second hub) can never remove it from under
    another schema.
    """
    applied = con.execute(
        "SELECT 1 FROM control_schema_migrations WHERE version = ?",
        [VECTOR_MIGRATION],
    ).fetchone()
    if applied is not None:
        return
    available = con.execute(
        "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
    ).fetchone()
    if available is None:
        return
    con.execute("SAVEPOINT drover_vector")
    try:
        if vector_extension_schema(con) is None:
            con.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        extension_schema = vector_extension_schema(con) or "public"
        for statement in _vector_migration_statements(extension_schema):
            raw.execute(statement)
        con.execute(
            "INSERT INTO control_schema_migrations (version) VALUES (?)",
            [VECTOR_MIGRATION],
        )
    except Exception:
        con.execute("ROLLBACK TO SAVEPOINT drover_vector")
        import logging

        logging.getLogger("drover.postgres_schema").warning(
            "pgvector is available but could not be enabled; session embeddings "
            "stay unavailable until it can be",
            exc_info=True,
        )
    finally:
        con.execute("RELEASE SAVEPOINT drover_vector")


def bootstrap_postgres_control_store(store: Any) -> None:
    """Apply idempotent, ordered PostgreSQL central-store migrations."""
    from psycopg import sql

    schema = store.config.schema
    with store.connection() as con:
        raw = con._connection
        con.execute("BEGIN")
        try:
            # API and worker can cold-start at the same time. The lock covers
            # schema creation as well as the version recheck, so catalog DDL
            # is never concurrent and a waiter sees the winner's migration.
            con.execute(
                "SELECT pg_advisory_xact_lock(hashtext(?))",
                [f"drover-control-schema:{schema}"],
            )
            raw.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
            )
            raw.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema))
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS control_schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            for version, statements in _MIGRATIONS:
                applied = con.execute(
                    "SELECT version FROM control_schema_migrations WHERE version = ?",
                    [version],
                ).fetchone()
                if applied is not None:
                    continue
                for statement in statements:
                    con.execute(statement)
                con.execute(
                    "INSERT INTO control_schema_migrations (version) VALUES (?)",
                    [version],
                )
            _apply_vector_migration(con, raw)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
