--
-- PostgreSQL database dump
--


-- Dumped from database version 17.11 (Homebrew)
-- Dumped by pg_dump version 17.11 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: drover_control; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA drover_control;


--
-- Name: drover_refresh_session_search(text); Type: FUNCTION; Schema: drover_control; Owner: -
--

CREATE FUNCTION drover_control.drover_refresh_session_search(sid text) RETURNS void
    LANGUAGE plpgsql
    SET search_path TO 'drover_control'
    AS $$
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
            $$;


--
-- Name: drover_session_search_from_memory(); Type: FUNCTION; Schema: drover_control; Owner: -
--

CREATE FUNCTION drover_control.drover_session_search_from_memory() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'drover_control'
    AS $$
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
            $$;


--
-- Name: drover_session_search_from_preview(); Type: FUNCTION; Schema: drover_control; Owner: -
--

CREATE FUNCTION drover_control.drover_session_search_from_preview() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'drover_control'
    AS $$
            BEGIN
              PERFORM drover_refresh_session_search(
                CASE WHEN TG_OP = 'DELETE' THEN OLD.session_id ELSE NEW.session_id END);
              RETURN NULL;
            END
            $$;


--
-- Name: drover_session_search_from_session(); Type: FUNCTION; Schema: drover_control; Owner: -
--

CREATE FUNCTION drover_control.drover_session_search_from_session() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'drover_control'
    AS $$
            BEGIN
              IF TG_OP = 'DELETE' THEN
                DELETE FROM session_search WHERE session_id = OLD.session_id;
                RETURN OLD;
              END IF;
              PERFORM drover_refresh_session_search(NEW.session_id);
              RETURN NEW;
            END
            $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: advisory_findings; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.advisory_findings (
    finding_id text NOT NULL,
    fingerprint text NOT NULL,
    analyzer_id text NOT NULL,
    rule_id text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    analyzer_class text NOT NULL,
    severity text NOT NULL,
    confidence text NOT NULL,
    title text NOT NULL,
    impact text NOT NULL,
    remediation_json text NOT NULL,
    state text NOT NULL,
    dismissal_reason text,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    resolved_at timestamp with time zone,
    dismissed_at timestamp with time zone,
    regressed_at timestamp with time zone,
    evaluated_content_hash text,
    regression_count integer DEFAULT 0 NOT NULL,
    latest_run_id text NOT NULL
);


--
-- Name: advisory_occurrences; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.advisory_occurrences (
    occurrence_id text NOT NULL,
    finding_id text NOT NULL,
    run_id text NOT NULL,
    outcome text NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    source_ref text,
    evidence_json text,
    excerpt text,
    evidence_hash text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: control_content_consent; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.control_content_consent (
    singleton boolean DEFAULT true NOT NULL,
    enabled boolean NOT NULL,
    epoch bigint NOT NULL,
    backend text NOT NULL,
    external_disclosure_accepted boolean NOT NULL,
    migrated_from_legacy boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT control_content_consent_epoch_check CHECK ((epoch >= 0)),
    CONSTRAINT control_content_consent_singleton_check CHECK (singleton)
);


--
-- Name: control_credentials; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.control_credentials (
    credential_id text NOT NULL,
    scope text NOT NULL,
    label text NOT NULL,
    verifier text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    host_id text,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone,
    apns_token text,
    apns_environment text,
    apns_failure_reason text,
    apns_failed_at timestamp with time zone,
    apns_failed_fingerprint text
);


--
-- Name: control_outbox_batch_events; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.control_outbox_batch_events (
    batch_id text NOT NULL,
    event_id text NOT NULL,
    ordinal integer NOT NULL
);


--
-- Name: control_outbox_batches; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.control_outbox_batches (
    batch_id text NOT NULL,
    state text NOT NULL,
    lease_owner text,
    lease_until timestamp with time zone,
    member_count integer NOT NULL,
    content_sha256 text,
    archive_path text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    published_at timestamp with time zone,
    acknowledged_at timestamp with time zone
);


--
-- Name: control_outbox_events; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.control_outbox_events (
    event_id text NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    batch_id text,
    lease_owner text,
    lease_until timestamp with time zone,
    committed_at timestamp with time zone DEFAULT now() NOT NULL,
    published_at timestamp with time zone,
    acknowledged_at timestamp with time zone
);


--
-- Name: control_schema_migrations; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.control_schema_migrations (
    version integer NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: control_server_identity; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.control_server_identity (
    identity_key text NOT NULL,
    identity_value text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: control_store_initialization; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.control_store_initialization (
    singleton boolean DEFAULT true NOT NULL,
    state text NOT NULL,
    mode text NOT NULL,
    source_fingerprint text,
    source_timezone text,
    verified_at timestamp with time zone,
    details_json text DEFAULT '{}'::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT control_store_initialization_singleton_check CHECK (singleton)
);


--
-- Name: factory_observer_inbox; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.factory_observer_inbox (
    event_id text NOT NULL,
    run_id text NOT NULL,
    source text NOT NULL,
    subject text NOT NULL,
    sequence bigint NOT NULL,
    payload_json text NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    action_json text,
    attempts integer DEFAULT 0 NOT NULL,
    next_delivery_at timestamp with time zone,
    acknowledged_at timestamp with time zone,
    received_at timestamp with time zone NOT NULL,
    CONSTRAINT factory_observer_inbox_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'delivered'::text, 'acknowledged'::text, 'exhausted'::text])))
);


--
-- Name: factory_observer_runs; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.factory_observer_runs (
    run_id text NOT NULL,
    session_id text NOT NULL,
    expected_revision bigint NOT NULL,
    objective text NOT NULL,
    checkpoint text NOT NULL,
    authority_scope text NOT NULL,
    owner_id text,
    owner_epoch bigint DEFAULT 0 NOT NULL,
    lease_until timestamp with time zone,
    worker_state text DEFAULT 'unknown'::text NOT NULL,
    next_action_json text,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT factory_observer_runs_authority_scope_check CHECK ((authority_scope = ANY (ARRAY['implementation'::text, 'integration'::text, 'deployment'::text])))
);


--
-- Name: harness_event_archives; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.harness_event_archives (
    event_id text NOT NULL,
    batch_id text NOT NULL,
    payload_sha256 text NOT NULL,
    verified_at timestamp with time zone NOT NULL,
    payload_pruned_at timestamp with time zone
);


--
-- Name: harness_event_payloads; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.harness_event_payloads (
    event_id text NOT NULL,
    payload_json text NOT NULL,
    payload_sha256 text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: harness_events; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.harness_events (
    event_id text NOT NULL,
    session_id text NOT NULL,
    event_type text NOT NULL,
    normalized_type text,
    normalized_source text,
    content_preview text,
    payload_json text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    seq integer,
    dedup_key text
);


--
-- Name: harness_hosts; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.harness_hosts (
    host_id text NOT NULL,
    display_name text NOT NULL,
    kind text NOT NULL,
    local_url text,
    tailscale_url text,
    connection_kind text,
    status text NOT NULL,
    capabilities_json text NOT NULL,
    model_catalogs_json text DEFAULT '{}'::text NOT NULL,
    agent_version text,
    update_json text,
    last_seen_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    retired_at timestamp with time zone,
    retired_reason text
);


--
-- Name: harness_session_previews; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.harness_session_previews (
    session_id text NOT NULL,
    event_id text NOT NULL,
    content_preview text NOT NULL,
    event_type text NOT NULL,
    event_priority integer NOT NULL,
    seq integer,
    event_created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: harness_sessions; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.harness_sessions (
    session_id text NOT NULL,
    host_id text NOT NULL,
    harness text NOT NULL,
    repo_owner text,
    repo_name text,
    branch text,
    cwd text,
    command text NOT NULL,
    status text NOT NULL,
    started_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    last_error text,
    summary_session_id text,
    native_session_id text,
    native_resume_label text,
    source_session_id text,
    handoff_mode text,
    mode text,
    awaiting text,
    last_activity timestamp with time zone,
    permission_mode text,
    model text,
    thinking_effort text,
    recap_reconcile_needed boolean DEFAULT false,
    client_session_id text,
    parent_session_id text
);


--
-- Name: lake_export_batches; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.lake_export_batches (
    batch_id text NOT NULL,
    catalog_id text NOT NULL,
    input_json text NOT NULL,
    input_sha256 text NOT NULL,
    receipt_sha256 text,
    acknowledged_at timestamp with time zone
);


--
-- Name: live_recap_jobs; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.live_recap_jobs (
    session_id text NOT NULL,
    desired_source_seq integer NOT NULL,
    status text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text,
    enqueued_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    next_run_at timestamp with time zone,
    stream_publish_needed boolean DEFAULT false NOT NULL
);


--
-- Name: live_session_recaps; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.live_session_recaps (
    session_id text NOT NULL,
    recap_text text NOT NULL,
    source_seq integer NOT NULL,
    generator_model text,
    generated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: native_usage_partition_totals; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.native_usage_partition_totals (
    native_usage_partition_id text NOT NULL,
    session_id text NOT NULL,
    partition_date text NOT NULL,
    input_tokens bigint,
    output_tokens bigint,
    cache_read_tokens bigint,
    cache_write_tokens bigint,
    reasoning_tokens bigint,
    turn_count integer NOT NULL,
    event_count integer NOT NULL,
    exact boolean NOT NULL,
    observed_at timestamp with time zone NOT NULL
);


--
-- Name: native_usage_partition_watermarks; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.native_usage_partition_watermarks (
    partition_date text NOT NULL,
    source_activity_at timestamp with time zone NOT NULL,
    rolled_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: pipeline_job_attempts; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.pipeline_job_attempts (
    attempt_id bigint NOT NULL,
    job_id text NOT NULL,
    attempt_no integer NOT NULL,
    worker_id text,
    lease_token text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    result text,
    error_category text,
    error_message text,
    metrics_json text,
    CONSTRAINT pipeline_job_attempts_result_check CHECK ((result = ANY (ARRAY['succeeded'::text, 'retryable_failed'::text, 'terminal_failed'::text, 'lease_expired'::text, 'released'::text, 'superseded'::text])))
);


--
-- Name: pipeline_job_attempts_attempt_id_seq; Type: SEQUENCE; Schema: drover_control; Owner: -
--

ALTER TABLE drover_control.pipeline_job_attempts ALTER COLUMN attempt_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME drover_control.pipeline_job_attempts_attempt_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pipeline_jobs; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.pipeline_jobs (
    job_id text NOT NULL,
    job_kind text NOT NULL,
    subject_key text NOT NULL,
    source_version text DEFAULT ''::text NOT NULL,
    payload_json text DEFAULT '{}'::text NOT NULL,
    status text NOT NULL,
    priority integer DEFAULT 0 NOT NULL,
    claims integer DEFAULT 0 NOT NULL,
    failures integer DEFAULT 0 NOT NULL,
    max_attempts integer NOT NULL,
    next_run_at timestamp with time zone DEFAULT now() NOT NULL,
    lease_owner text,
    lease_token text,
    lease_expires_at timestamp with time zone,
    last_error text,
    error_category text,
    disposition_reason text,
    enqueued_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pipeline_jobs_check CHECK (((status = 'running'::text) = (lease_token IS NOT NULL))),
    CONSTRAINT pipeline_jobs_check1 CHECK (((status = 'running'::text) OR (lease_expires_at IS NULL))),
    CONSTRAINT pipeline_jobs_check2 CHECK (((status <> ALL (ARRAY['dead_lettered'::text, 'quarantined'::text, 'superseded'::text])) OR (disposition_reason IS NOT NULL))),
    CONSTRAINT pipeline_jobs_claims_check CHECK ((claims >= 0)),
    CONSTRAINT pipeline_jobs_failures_check CHECK ((failures >= 0)),
    CONSTRAINT pipeline_jobs_job_kind_check CHECK ((job_kind = ANY (ARRAY['summarize_session'::text, 'embed_session'::text, 'brief_project'::text, 'recap_session'::text]))),
    CONSTRAINT pipeline_jobs_max_attempts_check CHECK ((max_attempts > 0)),
    CONSTRAINT pipeline_jobs_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'running'::text, 'retry_wait'::text, 'succeeded'::text, 'dead_lettered'::text, 'quarantined'::text, 'superseded'::text])))
);


--
-- Name: project_briefs; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.project_briefs (
    project_key text NOT NULL,
    repo_owner text NOT NULL,
    repo_name text NOT NULL,
    brief_md text NOT NULL,
    recent_themes_md text,
    key_files text[] DEFAULT '{}'::text[] NOT NULL,
    open_questions text[] DEFAULT '{}'::text[] NOT NULL,
    next_steps_md text,
    session_count integer DEFAULT 0 NOT NULL,
    last_activity_at timestamp with time zone,
    source_session_id text,
    source_version text,
    generator_model text,
    generated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: session_embeddings; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.session_embeddings (
    session_id text NOT NULL,
    embedding public.vector(768) NOT NULL,
    model text NOT NULL,
    dim integer NOT NULL,
    source_version text DEFAULT ''::text NOT NULL,
    embedded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT session_embeddings_dim_check CHECK ((dim = 768))
);


--
-- Name: session_memory; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.session_memory (
    session_id text NOT NULL,
    phase text NOT NULL,
    task_id text,
    agent_id text,
    project_key text,
    ended_at timestamp with time zone,
    summary_md text,
    next_steps_md text,
    files_touched text[] DEFAULT '{}'::text[] NOT NULL,
    tools_used jsonb DEFAULT '{}'::jsonb NOT NULL,
    open_questions text[] DEFAULT '{}'::text[] NOT NULL,
    last_user_prompt text,
    last_assistant text,
    summary_status text,
    summary_source_version text,
    summary_model text,
    summary_generated_at timestamp with time zone,
    recap_text text,
    recap_source_seq integer,
    recap_model text,
    recap_generated_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT session_memory_check CHECK (((phase = 'final'::text) = (summary_generated_at IS NOT NULL))),
    CONSTRAINT session_memory_check1 CHECK (((recap_text IS NULL) = (recap_source_seq IS NULL))),
    CONSTRAINT session_memory_phase_check CHECK ((phase = ANY (ARRAY['live'::text, 'final'::text])))
);


--
-- Name: session_search; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.session_search (
    session_id text NOT NULL,
    document tsvector NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: session_usage; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.session_usage (
    session_id text NOT NULL,
    host_id text,
    harness text,
    input_tokens bigint,
    output_tokens bigint,
    cache_read_tokens bigint,
    cache_write_tokens bigint,
    reasoning_tokens bigint,
    turn_count integer DEFAULT 0 NOT NULL,
    exact boolean DEFAULT true NOT NULL,
    source text NOT NULL,
    source_seq integer NOT NULL,
    source_event_count integer NOT NULL,
    observed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: session_usage_sources; Type: TABLE; Schema: drover_control; Owner: -
--

CREATE TABLE drover_control.session_usage_sources (
    source_usage_id text NOT NULL,
    session_id text NOT NULL,
    source text NOT NULL,
    host_id text,
    harness text,
    input_tokens bigint,
    output_tokens bigint,
    cache_read_tokens bigint,
    cache_write_tokens bigint,
    reasoning_tokens bigint,
    turn_count integer DEFAULT 0 NOT NULL,
    exact boolean DEFAULT true NOT NULL,
    usage_observed boolean DEFAULT false NOT NULL,
    source_seq integer NOT NULL,
    source_event_count integer NOT NULL,
    observed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: advisory_findings advisory_findings_fingerprint_key; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.advisory_findings
    ADD CONSTRAINT advisory_findings_fingerprint_key UNIQUE (fingerprint);


--
-- Name: advisory_findings advisory_findings_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.advisory_findings
    ADD CONSTRAINT advisory_findings_pkey PRIMARY KEY (finding_id);


--
-- Name: advisory_occurrences advisory_occurrences_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.advisory_occurrences
    ADD CONSTRAINT advisory_occurrences_pkey PRIMARY KEY (occurrence_id);


--
-- Name: control_content_consent control_content_consent_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.control_content_consent
    ADD CONSTRAINT control_content_consent_pkey PRIMARY KEY (singleton);


--
-- Name: control_credentials control_credentials_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.control_credentials
    ADD CONSTRAINT control_credentials_pkey PRIMARY KEY (credential_id);


--
-- Name: control_credentials control_credentials_verifier_key; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.control_credentials
    ADD CONSTRAINT control_credentials_verifier_key UNIQUE (verifier);


--
-- Name: control_outbox_batch_events control_outbox_batch_events_batch_id_ordinal_key; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.control_outbox_batch_events
    ADD CONSTRAINT control_outbox_batch_events_batch_id_ordinal_key UNIQUE (batch_id, ordinal);


--
-- Name: control_outbox_batch_events control_outbox_batch_events_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.control_outbox_batch_events
    ADD CONSTRAINT control_outbox_batch_events_pkey PRIMARY KEY (batch_id, event_id);


--
-- Name: control_outbox_batches control_outbox_batches_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.control_outbox_batches
    ADD CONSTRAINT control_outbox_batches_pkey PRIMARY KEY (batch_id);


--
-- Name: control_outbox_events control_outbox_events_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.control_outbox_events
    ADD CONSTRAINT control_outbox_events_pkey PRIMARY KEY (event_id);


--
-- Name: control_schema_migrations control_schema_migrations_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.control_schema_migrations
    ADD CONSTRAINT control_schema_migrations_pkey PRIMARY KEY (version);


--
-- Name: control_server_identity control_server_identity_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.control_server_identity
    ADD CONSTRAINT control_server_identity_pkey PRIMARY KEY (identity_key);


--
-- Name: control_store_initialization control_store_initialization_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.control_store_initialization
    ADD CONSTRAINT control_store_initialization_pkey PRIMARY KEY (singleton);


--
-- Name: factory_observer_inbox factory_observer_inbox_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.factory_observer_inbox
    ADD CONSTRAINT factory_observer_inbox_pkey PRIMARY KEY (event_id);


--
-- Name: factory_observer_runs factory_observer_runs_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.factory_observer_runs
    ADD CONSTRAINT factory_observer_runs_pkey PRIMARY KEY (run_id);


--
-- Name: harness_event_archives harness_event_archives_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.harness_event_archives
    ADD CONSTRAINT harness_event_archives_pkey PRIMARY KEY (event_id);


--
-- Name: harness_event_payloads harness_event_payloads_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.harness_event_payloads
    ADD CONSTRAINT harness_event_payloads_pkey PRIMARY KEY (event_id);


--
-- Name: harness_events harness_events_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.harness_events
    ADD CONSTRAINT harness_events_pkey PRIMARY KEY (event_id);


--
-- Name: harness_hosts harness_hosts_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.harness_hosts
    ADD CONSTRAINT harness_hosts_pkey PRIMARY KEY (host_id);


--
-- Name: harness_session_previews harness_session_previews_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.harness_session_previews
    ADD CONSTRAINT harness_session_previews_pkey PRIMARY KEY (session_id);


--
-- Name: harness_sessions harness_sessions_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.harness_sessions
    ADD CONSTRAINT harness_sessions_pkey PRIMARY KEY (session_id);


--
-- Name: lake_export_batches lake_export_batches_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.lake_export_batches
    ADD CONSTRAINT lake_export_batches_pkey PRIMARY KEY (batch_id);


--
-- Name: live_recap_jobs live_recap_jobs_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.live_recap_jobs
    ADD CONSTRAINT live_recap_jobs_pkey PRIMARY KEY (session_id);


--
-- Name: live_session_recaps live_session_recaps_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.live_session_recaps
    ADD CONSTRAINT live_session_recaps_pkey PRIMARY KEY (session_id);


--
-- Name: native_usage_partition_totals native_usage_partition_totals_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.native_usage_partition_totals
    ADD CONSTRAINT native_usage_partition_totals_pkey PRIMARY KEY (native_usage_partition_id);


--
-- Name: native_usage_partition_totals native_usage_partition_totals_session_id_partition_date_key; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.native_usage_partition_totals
    ADD CONSTRAINT native_usage_partition_totals_session_id_partition_date_key UNIQUE (session_id, partition_date);


--
-- Name: native_usage_partition_watermarks native_usage_partition_watermarks_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.native_usage_partition_watermarks
    ADD CONSTRAINT native_usage_partition_watermarks_pkey PRIMARY KEY (partition_date);


--
-- Name: pipeline_job_attempts pipeline_job_attempts_job_id_attempt_no_key; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.pipeline_job_attempts
    ADD CONSTRAINT pipeline_job_attempts_job_id_attempt_no_key UNIQUE (job_id, attempt_no);


--
-- Name: pipeline_job_attempts pipeline_job_attempts_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.pipeline_job_attempts
    ADD CONSTRAINT pipeline_job_attempts_pkey PRIMARY KEY (attempt_id);


--
-- Name: pipeline_jobs pipeline_jobs_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.pipeline_jobs
    ADD CONSTRAINT pipeline_jobs_pkey PRIMARY KEY (job_id);


--
-- Name: project_briefs project_briefs_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.project_briefs
    ADD CONSTRAINT project_briefs_pkey PRIMARY KEY (project_key);


--
-- Name: session_embeddings session_embeddings_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.session_embeddings
    ADD CONSTRAINT session_embeddings_pkey PRIMARY KEY (session_id);


--
-- Name: session_memory session_memory_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.session_memory
    ADD CONSTRAINT session_memory_pkey PRIMARY KEY (session_id);


--
-- Name: session_search session_search_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.session_search
    ADD CONSTRAINT session_search_pkey PRIMARY KEY (session_id);


--
-- Name: session_usage session_usage_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.session_usage
    ADD CONSTRAINT session_usage_pkey PRIMARY KEY (session_id);


--
-- Name: session_usage_sources session_usage_sources_pkey; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.session_usage_sources
    ADD CONSTRAINT session_usage_sources_pkey PRIMARY KEY (source_usage_id);


--
-- Name: session_usage_sources session_usage_sources_session_id_source_key; Type: CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.session_usage_sources
    ADD CONSTRAINT session_usage_sources_session_id_source_key UNIQUE (session_id, source);


--
-- Name: control_credentials_active_verifier; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX control_credentials_active_verifier ON drover_control.control_credentials USING btree (verifier) WHERE (revoked_at IS NULL);


--
-- Name: control_outbox_batches_visibility; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX control_outbox_batches_visibility ON drover_control.control_outbox_batches USING btree (state, published_at, batch_id);


--
-- Name: control_outbox_events_claim; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX control_outbox_events_claim ON drover_control.control_outbox_events USING btree (state, lease_until, committed_at, event_id);


--
-- Name: factory_observer_inbox_pending; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX factory_observer_inbox_pending ON drover_control.factory_observer_inbox USING btree (run_id, state, received_at, event_id);


--
-- Name: factory_observer_inbox_subject; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX factory_observer_inbox_subject ON drover_control.factory_observer_inbox USING btree (run_id, source, subject, sequence);


--
-- Name: harness_events_dedup_key; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE UNIQUE INDEX harness_events_dedup_key ON drover_control.harness_events USING btree (dedup_key) WHERE (dedup_key IS NOT NULL);


--
-- Name: harness_events_session_order; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX harness_events_session_order ON drover_control.harness_events USING btree (session_id, seq, created_at, event_id);


--
-- Name: harness_session_previews_order; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX harness_session_previews_order ON drover_control.harness_session_previews USING btree (session_id, event_priority, seq, event_created_at, event_id);


--
-- Name: harness_sessions_client_key; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE UNIQUE INDEX harness_sessions_client_key ON drover_control.harness_sessions USING btree (client_session_id);


--
-- Name: harness_sessions_history; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX harness_sessions_history ON drover_control.harness_sessions USING btree (COALESCE(last_activity, updated_at), session_id);


--
-- Name: harness_sessions_history_host; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX harness_sessions_history_host ON drover_control.harness_sessions USING btree (host_id, COALESCE(last_activity, updated_at), session_id);


--
-- Name: harness_sessions_native; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX harness_sessions_native ON drover_control.harness_sessions USING btree (native_session_id) WHERE (native_session_id IS NOT NULL);


--
-- Name: harness_sessions_parent; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX harness_sessions_parent ON drover_control.harness_sessions USING btree (parent_session_id) WHERE (parent_session_id IS NOT NULL);


--
-- Name: idx_advisory_findings_list; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX idx_advisory_findings_list ON drover_control.advisory_findings USING btree (state, severity, last_seen_at);


--
-- Name: idx_advisory_occurrences_finding; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX idx_advisory_occurrences_finding ON drover_control.advisory_occurrences USING btree (finding_id, outcome, recorded_at);


--
-- Name: live_recap_jobs_claim; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX live_recap_jobs_claim ON drover_control.live_recap_jobs USING btree (status, next_run_at, enqueued_at);


--
-- Name: pipeline_job_attempts_open; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX pipeline_job_attempts_open ON drover_control.pipeline_job_attempts USING btree (job_id, lease_token) WHERE (finished_at IS NULL);


--
-- Name: pipeline_jobs_due; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX pipeline_jobs_due ON drover_control.pipeline_jobs USING btree (job_kind, priority DESC, next_run_at, enqueued_at) WHERE (status = ANY (ARRAY['pending'::text, 'retry_wait'::text]));


--
-- Name: pipeline_jobs_failed; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX pipeline_jobs_failed ON drover_control.pipeline_jobs USING btree (job_kind, updated_at DESC) WHERE (status = ANY (ARRAY['retry_wait'::text, 'dead_lettered'::text, 'quarantined'::text]));


--
-- Name: pipeline_jobs_leases; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX pipeline_jobs_leases ON drover_control.pipeline_jobs USING btree (job_kind, lease_expires_at) WHERE (status = 'running'::text);


--
-- Name: pipeline_jobs_one_live; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE UNIQUE INDEX pipeline_jobs_one_live ON drover_control.pipeline_jobs USING btree (job_kind, subject_key) WHERE (status = ANY (ARRAY['pending'::text, 'running'::text, 'retry_wait'::text]));


--
-- Name: pipeline_jobs_subject; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX pipeline_jobs_subject ON drover_control.pipeline_jobs USING btree (job_kind, subject_key, enqueued_at DESC);


--
-- Name: pipeline_jobs_succeeded; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX pipeline_jobs_succeeded ON drover_control.pipeline_jobs USING btree (job_kind, finished_at DESC) WHERE (status = 'succeeded'::text);


--
-- Name: session_embeddings_model; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX session_embeddings_model ON drover_control.session_embeddings USING btree (model);


--
-- Name: session_memory_final_recent; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX session_memory_final_recent ON drover_control.session_memory USING btree (ended_at DESC NULLS LAST) WHERE (phase = 'final'::text);


--
-- Name: session_memory_project; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX session_memory_project ON drover_control.session_memory USING btree (project_key, ended_at DESC NULLS LAST);


--
-- Name: session_memory_task; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX session_memory_task ON drover_control.session_memory USING btree (task_id);


--
-- Name: session_search_document; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX session_search_document ON drover_control.session_search USING gin (document);


--
-- Name: session_usage_sources_session; Type: INDEX; Schema: drover_control; Owner: -
--

CREATE INDEX session_usage_sources_session ON drover_control.session_usage_sources USING btree (session_id, source);


--
-- Name: session_memory session_search_memory_write; Type: TRIGGER; Schema: drover_control; Owner: -
--

CREATE TRIGGER session_search_memory_write AFTER INSERT OR DELETE OR UPDATE OF summary_md, recap_text ON drover_control.session_memory FOR EACH ROW EXECUTE FUNCTION drover_control.drover_session_search_from_memory();


--
-- Name: harness_session_previews session_search_preview_write; Type: TRIGGER; Schema: drover_control; Owner: -
--

CREATE TRIGGER session_search_preview_write AFTER INSERT OR DELETE OR UPDATE OF content_preview ON drover_control.harness_session_previews FOR EACH ROW EXECUTE FUNCTION drover_control.drover_session_search_from_preview();


--
-- Name: harness_sessions session_search_session_delete; Type: TRIGGER; Schema: drover_control; Owner: -
--

CREATE TRIGGER session_search_session_delete AFTER DELETE ON drover_control.harness_sessions FOR EACH ROW EXECUTE FUNCTION drover_control.drover_session_search_from_session();


--
-- Name: harness_sessions session_search_session_write; Type: TRIGGER; Schema: drover_control; Owner: -
--

CREATE TRIGGER session_search_session_write AFTER INSERT OR UPDATE OF repo_owner, repo_name, branch, harness, native_session_id ON drover_control.harness_sessions FOR EACH ROW EXECUTE FUNCTION drover_control.drover_session_search_from_session();


--
-- Name: lake_export_batches lake_export_batches_batch_id_fkey; Type: FK CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.lake_export_batches
    ADD CONSTRAINT lake_export_batches_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES drover_control.control_outbox_batches(batch_id);


--
-- Name: pipeline_job_attempts pipeline_job_attempts_job_id_fkey; Type: FK CONSTRAINT; Schema: drover_control; Owner: -
--

ALTER TABLE ONLY drover_control.pipeline_job_attempts
    ADD CONSTRAINT pipeline_job_attempts_job_id_fkey FOREIGN KEY (job_id) REFERENCES drover_control.pipeline_jobs(job_id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--
