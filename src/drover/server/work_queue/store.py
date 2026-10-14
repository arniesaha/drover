"""Workspace-scoped execution links and append-only metadata events."""

from contextlib import contextmanager
from uuid import uuid4

from drover.server.control_store import postgres_control_store


class WorkStore:
    def __init__(self, path, workspace_id):
        self.path = path
        self.workspace_id = workspace_id

    @contextmanager
    def connection(self):
        with postgres_control_store(self.path).connection() as con:
            yield con

    @staticmethod
    def rows(cursor):
        return [
            dict(zip((c[0] for c in cursor.description), row))
            for row in cursor.fetchall()
        ]

    def links(self, *, run_key=None, goal_id=None, number=None):
        where, params = ["workspace_id = ?"], [self.workspace_id]
        for name, value in (
            ("run_key", run_key),
            ("goal_id", goal_id),
            ("item_number", number),
        ):
            if value is not None:
                where.append(name + " = ?")
                params.append(value)
        with self.connection() as con:
            return self.rows(
                con.execute(
                    "SELECT * FROM execution_links WHERE "
                    + " AND ".join(where)
                    + " ORDER BY created_at, id",
                    params,
                )
            )

    def intent(self, actor, ref, goal, run_key, role, failures, goal_ref):
        with self.connection() as con:
            con.execute(
                """INSERT INTO execution_links
                (id, workspace_id, actor_principal, provider, repo, item_number,
                 item_url, goal_id, run_key, run_role, failure_count, goal_ref)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (workspace_id, run_key) DO NOTHING""",
                [
                    str(uuid4()),
                    self.workspace_id,
                    actor,
                    ref.provider,
                    ref.repo,
                    ref.number,
                    ref.url,
                    goal,
                    run_key,
                    role,
                    failures,
                    goal_ref,
                ],
            )
        return self.links(run_key=run_key)[0]

    def associate(self, run_key, session_id, pr_ref=None):
        with self.connection() as con:
            con.execute(
                """UPDATE execution_links SET session_id = ?, pr_ref = COALESCE(?, pr_ref),
                updated_at = now(), last_progress_at = now()
                WHERE workspace_id = ? AND run_key = ?
                AND (session_id IS NULL OR session_id = ?)""",
                [session_id, pr_ref, self.workspace_id, run_key, session_id],
            )
        row = self.links(run_key=run_key)[0]
        if row["session_id"] != session_id:
            raise ValueError("session association conflict")
        return row

    def events(self, request_key):
        with self.connection() as con:
            return self.rows(
                con.execute(
                    "SELECT * FROM decision_events WHERE workspace_id = ? AND request_key = ? ORDER BY created_at, id",
                    [self.workspace_id, request_key],
                )
            )

    def decisions(self, ref):
        with self.connection() as con:
            return self.rows(
                con.execute(
                    """SELECT q.request_key AS decision_id, q.action_category, q.options,
                q.created_at, q.actor_principal,
                EXISTS (SELECT 1 FROM decision_events o WHERE o.workspace_id = q.workspace_id
                  AND o.request_key = q.request_key AND o.event_kind = 'outcome') AS resolved,
                EXISTS (SELECT 1 FROM decision_events i WHERE i.workspace_id = q.workspace_id
                  AND i.request_key = q.request_key AND i.event_kind = 'intent') AS intent_recorded
                FROM decision_events q WHERE q.workspace_id = ? AND q.provider = ?
                  AND q.repo = ? AND q.item_number = ? AND q.event_kind = 'question'
                ORDER BY q.created_at DESC LIMIT 100""",
                    [self.workspace_id, ref.provider, ref.repo, ref.number],
                )
            )

    def event(
        self,
        actor,
        ref,
        key,
        kind,
        category,
        options=(),
        outcome=None,
        operation_id=None,
        link_id=None,
    ):
        with self.connection() as con:
            con.execute(
                """INSERT INTO decision_events
                (id, workspace_id, actor_principal, provider, repo, item_number,
                 request_key, event_kind, action_category, options, outcome, provider_operation_id, execution_link_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    str(uuid4()),
                    self.workspace_id,
                    actor,
                    ref.provider,
                    ref.repo,
                    ref.number,
                    key,
                    kind,
                    category,
                    list(options),
                    outcome,
                    operation_id,
                    link_id,
                ],
            )

    def failure(self, run_key, failures):
        with self.connection() as con:
            con.execute(
                "UPDATE execution_links SET failure_count = ?, updated_at = now() WHERE workspace_id = ? AND run_key = ?",
                [failures, self.workspace_id, run_key],
            )
