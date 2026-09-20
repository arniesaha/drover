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
)


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
            raw.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
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
                    "INSERT INTO control_schema_migrations (version) VALUES (?)", [version]
                )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
