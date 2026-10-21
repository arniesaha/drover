"""Workspace-scoped execution links and append-only metadata events."""

import json
import re
import secrets
from contextlib import contextmanager
from uuid import uuid4

from drover.server.control_outbox import is_postgres_connection
from drover.server.control_store import postgres_control_store
from drover.server.work_queue.work_item_events import (
    _filter_kinds,
    append_timeline_event,
    event_payload,
)


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
        append_timeline_event(
            con,
            workspace_id=self.workspace_id,
            item_id=item_id,
            kind="launched",
            stage="launched",
            gate="worker_done",
            detail=title,
            from_stage=None,
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
        timeline_kind = (
            "pr_linked"
            if "pr" in supplied
            else ("stage_changed" if "stage" in supplied else "updated")
        )
        columns = [f"{key} = ?" for key in supplied]
        params = [*supplied.values(), self.workspace_id, item_id]
        with self.connection() as con:
            previous = con.execute(
                "SELECT stage FROM work_items WHERE workspace_id = ? AND id = ?",
                [self.workspace_id, item_id],
            ).fetchone()
            if previous is None:
                raise KeyError(item_id)
            row = con.execute(
                "UPDATE work_items SET " + ", ".join(columns) + ", updated_at = now() "
                "WHERE workspace_id = ? AND id = ? RETURNING id, stage, gate",
                params,
            ).fetchone()
            if row is None:
                raise KeyError(item_id)
            append_timeline_event(
                con,
                workspace_id=self.workspace_id,
                item_id=item_id,
                kind=timeline_kind,
                stage=row[1],
                gate=row[2],
                detail=", ".join(supplied),
                from_stage=previous[0],
            )
        return self.hub_item(item_id)

    def create_work_item_subscription(
        self, credential_id, *, url, event_kinds, repo=None
    ):
        kinds = _filter_kinds(event_kinds)
        if repo is not None and (
            not isinstance(repo, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
        ):
            raise ValueError("repo must be owner/name")
        subscription_id = str(uuid4())
        secret = secrets.token_urlsafe(32)
        with self.connection() as con:
            con.execute(
                """INSERT INTO work_item_subscriptions
                   (id, workspace_id, credential_id, url, event_kinds_json, repo, hmac_secret)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    subscription_id,
                    self.workspace_id,
                    credential_id,
                    url,
                    json.dumps(kinds),
                    repo,
                    secret,
                ],
            )
        return {
            "id": subscription_id,
            "url": url,
            "event_kinds": kinds,
            "repo": repo,
            "secret": secret,
        }

    def work_item_subscriptions(self, credential_id):
        with self.connection() as con:
            rows = self.rows(
                con.execute(
                    """SELECT id, url, event_kinds_json, repo, created_at, updated_at
                       FROM work_item_subscriptions WHERE workspace_id = ? AND credential_id = ?
                       ORDER BY created_at DESC, id""",
                    [self.workspace_id, credential_id],
                )
            )
        for row in rows:
            row["event_kinds"] = json.loads(row.pop("event_kinds_json"))
        return rows

    def delete_work_item_subscription(self, credential_id, subscription_id):
        with self.connection() as con:
            row = con.execute(
                """DELETE FROM work_item_subscriptions
                   WHERE workspace_id = ? AND credential_id = ? AND id = ? RETURNING id""",
                [self.workspace_id, credential_id, subscription_id],
            ).fetchone()
        return row is not None

    def work_item_events(self, *, since, limit):
        with self.connection() as con:
            rows = self.rows(
                con.execute(
                    """SELECT event_id, cursor, item_id, kind, from_stage, to_stage, gate,
                              next_action, session_id, pr, occurred_at
                       FROM work_item_events WHERE workspace_id = ? AND cursor > ?
                       ORDER BY cursor LIMIT ?""",
                    [self.workspace_id, since, limit],
                )
            )
        next_cursor = rows[-1]["cursor"] if rows else since
        return {
            "events": [event_payload(row) for row in rows],
            "next_cursor": next_cursor,
        }

    def claim_work_item_deliveries(self, limit):
        """Lease at most ``limit`` rows; expired leases make retries durable."""
        with self.connection() as con:
            rows = self.rows(
                con.execute(
                    """SELECT d.subscription_id, d.event_id, d.attempts, s.url, s.hmac_secret,
                              e.item_id, e.kind, e.from_stage, e.to_stage, e.gate,
                              e.next_action, e.session_id, e.pr, e.occurred_at
                       FROM work_item_event_deliveries d
                       JOIN work_item_subscriptions s ON s.id = d.subscription_id
                       JOIN work_item_events e ON e.event_id = d.event_id
                       WHERE (d.state = 'pending' AND d.next_attempt_at <= now())
                          OR (d.state = 'leased' AND d.lease_until <= now())
                       ORDER BY d.next_attempt_at, d.event_id LIMIT ? FOR UPDATE SKIP LOCKED""",
                    [limit],
                )
            )
            for row in rows:
                row["attempts"] += 1
                con.execute(
                    """UPDATE work_item_event_deliveries SET attempts = ?, state = 'leased',
                       lease_until = now() + interval '35 seconds' WHERE subscription_id = ? AND event_id = ?""",
                    [row["attempts"], row["subscription_id"], row["event_id"]],
                )
        return rows

    def complete_work_item_delivery(self, row, *, status, delivered, max_attempts):
        with self.connection() as con:
            if delivered:
                con.execute(
                    """UPDATE work_item_event_deliveries SET state = 'sent', last_status = ?,
                       delivered_at = now(), lease_until = NULL WHERE subscription_id = ? AND event_id = ?
                       AND state = 'leased' AND attempts = ?""",
                    [status, row["subscription_id"], row["event_id"], row["attempts"]],
                )
                return "sent"
            if row["attempts"] >= max_attempts:
                con.execute(
                    """UPDATE work_item_event_deliveries SET state = 'dead_letter', last_status = ?,
                       dead_lettered_at = now(), lease_until = NULL WHERE subscription_id = ? AND event_id = ?
                       AND state = 'leased' AND attempts = ?""",
                    [
                        status or None,
                        row["subscription_id"],
                        row["event_id"],
                        row["attempts"],
                    ],
                )
                return "dead_lettered"
            delay = min(3600, 2 ** (row["attempts"] - 1))
            con.execute(
                """UPDATE work_item_event_deliveries SET state = 'pending', last_status = ?,
                   next_attempt_at = now() + (? * interval '1 second'), lease_until = NULL
                   WHERE subscription_id = ? AND event_id = ? AND state = 'leased' AND attempts = ?""",
                [
                    status or None,
                    delay,
                    row["subscription_id"],
                    row["event_id"],
                    row["attempts"],
                ],
            )
            return "failed"

    def work_item_delivery_health(self):
        with self.connection() as con:
            row = con.execute(
                """SELECT count(*) FILTER (WHERE state = 'pending') AS pending,
                          count(*) FILTER (WHERE state = 'dead_letter') AS dead_lettered,
                          EXTRACT(EPOCH FROM (now() - min(next_attempt_at)
                            FILTER (WHERE state = 'pending'))) AS oldest_lag_seconds
                   FROM work_item_event_deliveries"""
            ).fetchone()
        return {
            "pending": int(row[0] or 0),
            "dead_lettered_total": int(row[1] or 0),
            "oldest_lag_seconds": float(row[2] or 0),
        }

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
    # Hub-owned work items are part of the PostgreSQL control plane. Local
    # harness daemons retain their historical DuckDB event path unchanged.
    if not is_postgres_connection(con):
        return False
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
        stage, gate, kind, detail = "blocked", "worker_done", "blocked", None
    elif (
        event_type == "session.terminated" or data.get("session_status") == "terminated"
    ):
        stage, gate, kind, detail = "closed", None, "closed", None
    else:
        return False
    row = con.execute(
        "SELECT id, stage FROM work_items WHERE session_id = ? FOR UPDATE", [session_id]
    ).fetchone()
    if row is None:
        return False
    # An explicit done state is a terminal success, and a later termination is
    # just cleanup rather than a closure of the item.
    if kind == "closed" and row[1] == "done":
        return False
    timeline_id = str(uuid4())
    inserted = con.execute(
        """INSERT INTO work_item_timeline
           (id, work_item_id, source_event_id, kind, stage, gate, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (source_event_id) DO NOTHING
           RETURNING id""",
        [timeline_id, row[0], event_id, kind, stage, gate, detail],
    ).fetchone()
    if inserted is None:
        return False
    con.execute(
        "UPDATE work_items SET stage = ?, gate = ?, updated_at = now() WHERE id = ?",
        [stage, gate, row[0]],
    )
    # The lifecycle timeline row has already been inserted so preserve its
    # stable id while creating the event/delivery facts.  The helper normally
    # inserts its own timeline row; inline this one keeps source-event replay
    # semantics intact.
    event_row = con.execute(
        "SELECT created_at FROM work_item_timeline WHERE id = ?", [timeline_id]
    ).fetchone()
    item = con.execute(
        "SELECT workspace_id, repo, session_id, pr, next_action FROM work_items WHERE id = ?",
        [row[0]],
    ).fetchone()
    cursor = con.execute(
        """INSERT INTO work_item_event_cursors (workspace_id, cursor) VALUES (?, 1)
           ON CONFLICT (workspace_id) DO UPDATE SET cursor = work_item_event_cursors.cursor + 1
           RETURNING cursor""",
        [item[0]],
    ).fetchone()[0]
    con.execute(
        """INSERT INTO work_item_events
           (event_id, workspace_id, cursor, item_id, kind, from_stage, to_stage,
            gate, next_action, session_id, pr, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            timeline_id,
            item[0],
            cursor,
            row[0],
            kind,
            row[1],
            stage,
            gate,
            item[4],
            item[2],
            item[3],
            event_row[0],
        ],
    )
    subscriptions = con.execute(
        "SELECT id, event_kinds_json, repo FROM work_item_subscriptions WHERE workspace_id = ?",
        [item[0]],
    ).fetchall()
    for subscription_id, kinds_json, repo_filter in subscriptions:
        try:
            kinds = json.loads(kinds_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if (kinds and kind not in kinds) or (repo_filter and repo_filter != item[1]):
            continue
        con.execute(
            """INSERT INTO work_item_event_deliveries (subscription_id, event_id)
               VALUES (?, ?) ON CONFLICT DO NOTHING""",
            [subscription_id, timeline_id],
        )
    return True
