"""Additive lifecycle records shared by central and local control stores."""

SESSION_COLUMNS = {
    "end_reason": "TEXT",
    "archived_at": "TIMESTAMPTZ",
    "retention_policy": "TEXT DEFAULT 'auto'",
    "retention_reason": "TEXT",
    "retention_actor": "TEXT",
    "retention_updated_at": "TIMESTAMPTZ",
    "lifecycle_generation": "INTEGER DEFAULT 1",
}

OPERATIONS_DDL = """
CREATE TABLE IF NOT EXISTS session_lifecycle_operations (
  operation_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES harness_sessions(session_id),
  host_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  action TEXT NOT NULL,
  reason TEXT NOT NULL,
  actor TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL DEFAULT 'pending',
  preconditions_json TEXT NOT NULL DEFAULT '{}',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  lease_until TIMESTAMPTZ,
  result_json TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
STOP_MIGRATION = tuple(
    f"ALTER TABLE harness_sessions ADD COLUMN IF NOT EXISTS {name} {ddl}"
    for name, ddl in SESSION_COLUMNS.items()
) + (
    OPERATIONS_DDL,
    "CREATE INDEX IF NOT EXISTS lifecycle_pending ON session_lifecycle_operations (state, host_id)",
)
