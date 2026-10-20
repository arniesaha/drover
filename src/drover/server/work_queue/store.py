"""Workspace-scoped execution links and append-only metadata events."""

import re
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

    # Hub-owned continuity is deliberately stored beside the existing work
    # queue links.  The provider queue remains the policy/automation layer;
    # these rows are the durable, local record of every launched session.
    def register_hub_launch(
        self,
        con,
        *,
        session_id,
        title,
        repo,
        branch,
        owner,
        work_item_id=None,
        acceptance=None,
    ):
        """Create or link a work item using the caller's session transaction."""
        item_id = str(work_item_id or uuid4())
        if work_item_id:
            linked = con.execute(
                """UPDATE work_items SET session_id = ?, updated_at = now()
                   WHERE workspace_id = ? AND id = ?
                     AND (session_id IS NULL OR session_id = ?)
                   RETURNING id""",
                [session_id, self.workspace_id, item_id, session_id],
            ).fetchone()
            if linked is None:
                raise ValueError("unknown or already-linked work item")
        else:
            con.execute(
                """INSERT INTO work_items
                   (id, workspace_id, title, repo, branch, session_id, owner,
                    stage, gate, acceptance)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'launched', 'worker_done', ?)""",
                [
                    item_id,
                    self.workspace_id,
                    title,
                    repo,
                    branch,
                    session_id,
                    owner,
                    acceptance,
                ],
            )
        con.execute(
            """INSERT INTO work_item_timeline
               (id, work_item_id, kind, stage, gate, detail)
               VALUES (?, ?, 'launched', 'launched', 'worker_done', ?)""",
            [str(uuid4()), item_id, title],
        )
        return item_id

    def hub_items(self, *, state=None):
        if state not in {None, "open"}:
            raise ValueError("invalid work item state")
        where = "workspace_id = ?"
        params = [self.workspace_id]
        if state == "open":
            where += " AND stage NOT IN ('done', 'closed')"
        with self.connection() as con:
            return self.rows(
                con.execute(
                    "SELECT * FROM work_items WHERE "
                    + where
                    + " ORDER BY updated_at DESC, id",
                    params,
                )
            )

    def hub_item(self, item_id):
        with self.connection() as con:
            rows = self.rows(
                con.execute(
                    "SELECT * FROM work_items WHERE workspace_id = ? AND id = ?",
                    [self.workspace_id, item_id],
                )
            )
            if not rows:
                return None
            rows[0]["timeline"] = self.rows(
                con.execute(
                    "SELECT * FROM work_item_timeline WHERE work_item_id = ? ORDER BY created_at, id",
                    [item_id],
                )
            )
            return rows[0]

    def patch_hub_item(
        self, item_id, *, stage=None, next_action=None, pr=None, acceptance=None
    ):
        values = {
            "stage": stage,
            "next_action": next_action,
            "pr": pr,
            "acceptance": acceptance,
        }
        supplied = {key: value for key, value in values.items() if value is not None}
        if not supplied or any(
            not isinstance(value, str) or not value.strip()
            for value in supplied.values()
        ):
            raise ValueError("non-empty work item fields required")
        columns = [f"{key} = ?" for key in supplied]
        params = [*supplied.values(), self.workspace_id, item_id]
        with self.connection() as con:
            row = con.execute(
                "UPDATE work_items SET " + ", ".join(columns) + ", updated_at = now() "
                "WHERE workspace_id = ? AND id = ? RETURNING id, stage, gate",
                params,
            ).fetchone()
            if row is None:
                raise KeyError(item_id)
            con.execute(
                """INSERT INTO work_item_timeline (id, work_item_id, kind, stage, gate, detail)
                   VALUES (?, ?, 'patched', ?, ?, ?)""",
                [str(uuid4()), item_id, row[1], row[2], ", ".join(supplied)],
            )
        return self.hub_item(item_id)

    def hub_export(self):
        items = self.hub_items()
        return {
            "items": [
                {
                    "id": row["id"],
                    "stage": row["stage"],
                    "pr": row["pr"],
                    "branch": row["branch"],
                    "sid": row["session_id"],
                    "next": row["next_action"],
                    "after": row["updated_at"],
                }
                for row in items
            ]
        }


def _event_payload(payload):
    inner = payload.get("payload") if isinstance(payload, dict) else None
    return (
        inner
        if isinstance(inner, dict)
        else (payload if isinstance(payload, dict) else {})
    )


def _reported_commit(payload):
    values = _event_payload(payload)
    for key in ("commit_sha", "commit", "sha"):
        candidate = values.get(key)
        if isinstance(candidate, str) and re.fullmatch(r"[0-9a-fA-F]{7,64}", candidate):
            return candidate
    return None


def apply_hub_lifecycle_event(con, *, event_id, session_id, event_type, payload):
    """Project one persisted harness event into its linked work item.

    Called only after the outbox event insert won, in that same transaction.
    The source-event unique key makes a replay a no-op even if a caller later
    replays a historical event through another ingest path.
    """
    data = _event_payload(payload)
    if data.get("turn_complete") is True:
        stage, gate, kind, detail = (
            "worker_done",
            "ci_green",
            "worker_done",
            _reported_commit(payload),
        )
    elif data.get("awaiting") in {"input", "approval"}:
        stage, gate, kind, detail = (
            "needs_input",
            "worker_done",
            "needs_input",
            data.get("awaiting"),
        )
    elif (
        event_type in {"session.error", "error"}
        or event_type.endswith(".error")
        or data.get("session_status") == "errored"
        or (event_type == "session.exited" and data.get("exited") not in {None, 0})
    ):
        stage, gate, kind, detail = "blocked", "worker_done", "errored", None
    elif (
        event_type == "session.terminated" or data.get("session_status") == "terminated"
    ):
        stage, gate, kind, detail = "closed", None, "terminated", None
    else:
        return False
    row = con.execute(
        "SELECT id, stage FROM work_items WHERE session_id = ? FOR UPDATE", [session_id]
    ).fetchone()
    if row is None:
        return False
    # An explicit done state is a terminal success, and a later termination is
    # just cleanup rather than a closure of the item.
    if kind == "terminated" and row[1] == "done":
        return False
    inserted = con.execute(
        """INSERT INTO work_item_timeline
           (id, work_item_id, source_event_id, kind, stage, gate, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (source_event_id) DO NOTHING
           RETURNING id""",
        [str(uuid4()), row[0], event_id, kind, stage, gate, detail],
    ).fetchone()
    if inserted is None:
        return False
    con.execute(
        "UPDATE work_items SET stage = ?, gate = ?, updated_at = now() WHERE id = ?",
        [stage, gate, row[0]],
    )
    return True
