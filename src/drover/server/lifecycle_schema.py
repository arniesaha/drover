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

PUBLICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS session_publications (
  publication_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES harness_sessions(session_id),
  repo TEXT NOT NULL,
  pushed_branch TEXT NOT NULL,
  pushed_sha TEXT NOT NULL,
  session_head TEXT NOT NULL,
  base_sha TEXT NOT NULL,
  pr_number INTEGER,
  source TEXT NOT NULL CHECK (source IN ('orchestrator', 'push_capture', 'operator')),
  actor TEXT NOT NULL,
  report_hash TEXT NOT NULL,
  pr_state TEXT NOT NULL DEFAULT 'unknown',
  pr_verified_at TIMESTAMPTZ,
  evidence_json TEXT,
  reported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (session_id, report_hash)
)
"""
WORKTREES_DDL = """
CREATE TABLE IF NOT EXISTS session_worktrees (
  host_id TEXT NOT NULL,
  path TEXT NOT NULL,
  session_id TEXT REFERENCES harness_sessions(session_id),
  repo TEXT,
  branch TEXT,
  base_sha TEXT,
  observed_head TEXT,
  ownership TEXT NOT NULL,
  gc_state TEXT NOT NULL DEFAULT 'retained',
  reasons_json TEXT NOT NULL DEFAULT '[]',
  observation_json TEXT NOT NULL DEFAULT '{}',
  observed_at TIMESTAMPTZ NOT NULL,
  archive_receipt_json TEXT,
  collected_at TIMESTAMPTZ,
  PRIMARY KEY (host_id, path)
)
"""
PUBLICATION_MIGRATION = (
    PUBLICATIONS_DDL,
    WORKTREES_DDL,
    "CREATE INDEX IF NOT EXISTS publication_pr ON session_publications (repo, pr_number)",
    "CREATE INDEX IF NOT EXISTS worktree_session ON session_worktrees (session_id)",
)
