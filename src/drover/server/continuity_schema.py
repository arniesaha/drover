"""Shared additive DDL for the Drover observer inbox, never Factory state."""

CONTINUITY_TABLES = ("factory_observer_runs", "factory_observer_inbox")

CONTINUITY_DDL = (
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
