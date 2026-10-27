"""Shared additive DDL for the Drover observer inbox, never Factory state."""

# The first two tables shipped together.  Receipts were added later, so import
# code accepts a snapshot that predates them and creates an empty receipt set
# rather than treating a valid continuity checkpoint as corrupt.
CONTINUITY_BASE_TABLES = ("factory_observer_runs", "factory_observer_inbox")
CONTINUITY_RECEIPT_TABLE = "factory_observer_completion_receipts"
CONTINUITY_TABLES = (*CONTINUITY_BASE_TABLES, CONTINUITY_RECEIPT_TABLE)

# Native owner execution records are control-plane state, not harness-local
# state.  A turn claim is intentionally separate from the observer delivery:
# a lost hub process may retry the same client turn id, but cannot overlap a
# second turn on its linked owner session.
NATIVE_OWNER_DDL = (
    """CREATE TABLE IF NOT EXISTS continuity_owner_sessions (
      goal_id TEXT NOT NULL, owner_id TEXT NOT NULL,
      session_id TEXT, client_session_id TEXT NOT NULL,
      generation INTEGER NOT NULL DEFAULT 1,
      updated_at TIMESTAMPTZ NOT NULL,
      PRIMARY KEY (goal_id, owner_id), UNIQUE (client_session_id)
    )""",
    """CREATE TABLE IF NOT EXISTS continuity_owner_turn_claims (
      wake_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, owner_id TEXT NOT NULL,
      run_id TEXT NOT NULL, session_id TEXT NOT NULL,
      client_turn_id TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN
        ('claimed', 'sent', 'completed')),
      claim_until TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS continuity_owner_turn_live ON continuity_owner_turn_claims (goal_id, owner_id, state, claim_until)",
)

NATIVE_OWNER_RECEIPT_DDL = (
    """CREATE TABLE IF NOT EXISTS continuity_owner_action_intents (
      wake_id TEXT NOT NULL, action_fingerprint TEXT NOT NULL, run_id TEXT NOT NULL,
      owner_id TEXT NOT NULL, owner_epoch BIGINT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('intent', 'complete')),
      artifact TEXT, created_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ,
      PRIMARY KEY (wake_id, action_fingerprint)
    )""",
    "CREATE INDEX IF NOT EXISTS continuity_owner_action_intents_run ON continuity_owner_action_intents (run_id, wake_id)",
)

CONTINUITY_DDL = (
    """
    CREATE TABLE IF NOT EXISTS factory_observer_runs (
      run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      expected_revision BIGINT NOT NULL, objective TEXT NOT NULL,
      checkpoint TEXT NOT NULL, authority_scope TEXT NOT NULL CHECK
        (authority_scope IN ('implementation', 'integration', 'deployment')),
      authorized_owner_id TEXT,
      handoff_owner_id TEXT, handoff_token_sha256 TEXT,
      handoff_issued_by TEXT, handoff_issued_at TIMESTAMPTZ,
      handoff_redeemed_at TIMESTAMPTZ,
      owner_id TEXT, owner_epoch BIGINT NOT NULL DEFAULT 0,
      lease_until TIMESTAMPTZ, worker_state TEXT NOT NULL DEFAULT 'unknown',
      next_action_json TEXT, updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS factory_observer_inbox (
      event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
      source TEXT NOT NULL, subject TEXT NOT NULL, sequence BIGINT NOT NULL,
      payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending' CHECK
        (state IN ('pending', 'delivered', 'acknowledged', 'exhausted')),
      action_json TEXT, attempts INTEGER NOT NULL DEFAULT 0,
      next_delivery_at TIMESTAMPTZ, acknowledged_at TIMESTAMPTZ,
      received_at TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS factory_observer_inbox_pending "
    "ON factory_observer_inbox (run_id, state, received_at, event_id)",
    "CREATE INDEX IF NOT EXISTS factory_observer_inbox_subject "
    "ON factory_observer_inbox (run_id, source, subject, sequence)",
    """
    CREATE TABLE IF NOT EXISTS factory_observer_completion_receipts (
      harness_event_id TEXT PRIMARY KEY, run_id TEXT,
      session_id TEXT NOT NULL, sequence BIGINT,
      state TEXT NOT NULL CHECK (state IN
        ('projected', 'pending_authorization', 'rejected')),
      reason TEXT, continuity_event_id TEXT,
      recorded_at TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS factory_observer_completion_receipts_run "
    "ON factory_observer_completion_receipts (run_id, state, recorded_at, harness_event_id)",
)

# PostgreSQL migration 10 was released with this exact tuple. New continuity
# columns/tables belong in later numbered migrations, not in its hash.
POSTGRES_V10_CONTINUITY_DDL = (
    """
    CREATE TABLE IF NOT EXISTS factory_observer_runs (
      run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      expected_revision BIGINT NOT NULL, objective TEXT NOT NULL,
      checkpoint TEXT NOT NULL, authority_scope TEXT NOT NULL CHECK
        (authority_scope IN ('implementation', 'integration', 'deployment')),
      owner_id TEXT, owner_epoch BIGINT NOT NULL DEFAULT 0,
      lease_until TIMESTAMPTZ, worker_state TEXT NOT NULL DEFAULT 'unknown',
      next_action_json TEXT, updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS factory_observer_inbox (
      event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
      source TEXT NOT NULL, subject TEXT NOT NULL, sequence BIGINT NOT NULL,
      payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending' CHECK
        (state IN ('pending', 'delivered', 'acknowledged', 'exhausted')),
      action_json TEXT, attempts INTEGER NOT NULL DEFAULT 0,
      next_delivery_at TIMESTAMPTZ, acknowledged_at TIMESTAMPTZ,
      received_at TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS factory_observer_inbox_pending "
    "ON factory_observer_inbox (run_id, state, received_at, event_id)",
    "CREATE INDEX IF NOT EXISTS factory_observer_inbox_subject "
    "ON factory_observer_inbox (run_id, source, subject, sequence)",
)
