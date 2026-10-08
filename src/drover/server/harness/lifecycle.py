"""Durable explicit stop intent. Lifecycle policy never dispatches a stop."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from drover.server.control_outbox import is_postgres_connection
from drover.server.harness.models import TERMINAL_SESSION_STATUSES
from drover.server.harness.registry import HarnessRegistry


class LifecycleStore:
    def __init__(self, path):
        self.registry = HarnessRegistry(path)

    def request_stop(self, session_id):
        with self.registry._connect() as con:
            row = con.execute(
                "SELECT host_id, lifecycle_generation FROM harness_sessions WHERE session_id = ?",
                [session_id],
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            host, generation = row
            key = f"stop:{session_id}:{host}:{generation}"
            operation_id = hashlib.sha256(key.encode()).hexdigest()
            con.execute(
                "INSERT INTO session_lifecycle_operations "
                "(operation_id, session_id, host_id, generation, action, reason, actor, idempotency_key) "
                "VALUES (?, ?, ?, ?, 'stop', 'user', 'authenticated_user', ?) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                [operation_id, session_id, host, generation, key],
            )
        return operation_id

    def pending(self, host_id=None, session_id=None):
        with self.registry._connect() as con:
            result = con.execute(
                "SELECT o.operation_id, o.session_id, o.host_id, o.generation "
                "FROM session_lifecycle_operations o JOIN harness_sessions s "
                "ON s.session_id = o.session_id AND s.host_id = o.host_id "
                "AND s.lifecycle_generation = o.generation "
                "WHERE o.state = 'pending' AND o.action = 'stop' AND o.reason = 'user' AND (CAST(? AS TEXT) IS NULL OR o.host_id = ?) "
                "AND (CAST(? AS TEXT) IS NULL OR o.session_id = ?)",
                [host_id, host_id, session_id, session_id],
            ).fetchall()
        return result

    def attempted(self, operation_id, error=None):
        with self.registry._connect() as con:
            con.execute(
                "UPDATE session_lifecycle_operations SET attempts = attempts + 1, "
                "last_error = ?, updated_at = ? WHERE operation_id = ? AND state = 'pending'",
                [error, datetime.now(timezone.utc), operation_id],
            )

    def confirm(self, operation_id, payload):
        # A 202 or an empty/invalid response is not an acknowledgement.
        status = payload.get("status")
        if status not in TERMINAL_SESSION_STATUSES:
            return False
        if payload.get("terminated") is False:
            return False
        now = datetime.now(timezone.utc)
        with self.registry._connect() as con:
            con.execute("BEGIN")
            try:
                row = con.execute(
                    "SELECT o.session_id, o.host_id FROM session_lifecycle_operations o "
                    "JOIN harness_sessions s ON s.session_id = o.session_id "
                    "AND s.host_id = o.host_id AND s.lifecycle_generation = o.generation "
                    "WHERE o.operation_id = ? AND o.state = 'pending'"
                    + (" FOR UPDATE OF s, o" if is_postgres_connection(con) else ""),
                    [operation_id],
                ).fetchone()
                if (
                    row is None
                    or payload.get("session_id") != row[0]
                    or (
                        payload.get("host_id") is not None
                        and payload["host_id"] != row[1]
                    )
                ):
                    con.execute("ROLLBACK")
                    return False
                con.execute(
                    "UPDATE harness_sessions SET status = ?, ended_at = COALESCE(ended_at, ?), "
                    "end_reason = COALESCE(end_reason, 'user'), updated_at = ? WHERE session_id = ?",
                    [status, now, now, row[0]],
                )
                con.execute(
                    "UPDATE session_lifecycle_operations SET state = 'confirmed', result_json = ?, "
                    "last_error = NULL, updated_at = ? WHERE operation_id = ?",
                    [json.dumps({"status": status}), now, operation_id],
                )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        return True

    def publications(self, session_id):
        with self.registry._connect() as con:
            result = con.execute(
                "SELECT publication_id, repo, pushed_branch, pushed_sha, session_head, base_sha, "
                "pr_number, source, actor, pr_state, pr_verified_at, reported_at "
                "FROM session_publications WHERE session_id = ? ORDER BY reported_at, publication_id",
                [session_id],
            )
            names = [column[0] for column in result.description]
            return [dict(zip(names, row)) for row in result.fetchall()]

    def report_publication(self, session_id, payload):
        import re

        from drover.server.harness.auth import redact_auth_text

        required = {
            "repo",
            "pushed_branch",
            "pushed_sha",
            "session_head",
            "base_sha",
            "source",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) - (required | {"pr_number"})
            or not required <= set(payload)
        ):
            raise ValueError("missing or unsupported publication fields")
        repo = payload["repo"]
        if (
            not isinstance(repo, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or len(repo) > 256
        ):
            raise ValueError("repo must be owner/name, without a URL")
        branch = payload["pushed_branch"]
        if (
            not isinstance(branch, str)
            or not branch
            or len(branch) > 256
            or any(c.isspace() or ord(c) < 32 for c in branch)
            or any(c in branch for c in ":@?*[\\~^")
            or ".." in branch
            or branch.startswith(("/", "-"))
            or branch.endswith(("/", ".", ".lock"))
        ):
            raise ValueError("invalid pushed_branch")
        for field in ("pushed_sha", "session_head", "base_sha"):
            if not isinstance(payload[field], str) or not re.fullmatch(
                r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", payload[field]
            ):
                raise ValueError("publication SHAs must be full hexadecimal object ids")
        if not isinstance(payload["source"], str) or payload["source"] not in {
            "orchestrator",
            "push_capture",
            "operator",
        }:
            raise ValueError("invalid publication source")
        pr = payload.get("pr_number")
        if pr is not None and (type(pr) is not int or not 0 < pr < 2147483648):
            raise ValueError("invalid pr_number")
        payload = dict(payload, pr_number=pr)
        payload["repo"] = repo.lower()
        for field in ("pushed_sha", "session_head", "base_sha"):
            payload[field] = payload[field].lower()
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if redact_auth_text(canonical) != canonical:
            raise ValueError("credentials are not publication metadata")
        session = self.registry.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        if (
            session.repo_owner
            and session.repo_name
            and repo.lower() != f"{session.repo_owner}/{session.repo_name}".lower()
        ):
            raise ValueError("publication repo differs from session repo")
        report_hash = hashlib.sha256(canonical.encode()).hexdigest()
        publication_id = hashlib.sha256(
            f"{session_id}:{report_hash}".encode()
        ).hexdigest()
        with self.registry._connect() as con:
            con.execute(
                "INSERT INTO session_publications (publication_id, session_id, repo, pushed_branch, "
                "pushed_sha, session_head, base_sha, pr_number, source, actor, report_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'authenticated_user', ?) "
                "ON CONFLICT (session_id, report_hash) DO NOTHING",
                [
                    publication_id,
                    session_id,
                    repo,
                    branch,
                    payload["pushed_sha"].lower(),
                    payload["session_head"].lower(),
                    payload["base_sha"].lower(),
                    pr,
                    payload["source"],
                    report_hash,
                ],
            )
        return next(
            p
            for p in self.publications(session_id)
            if p["publication_id"] == publication_id
        )

    def record_inventory(self, host_id, trees, now):
        with self.registry._connect() as con:
            for tree in trees:
                con.execute(
                    "INSERT INTO session_worktrees (host_id, path, session_id, repo, branch, base_sha, "
                    "observed_head, ownership, reasons_json, observation_json, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (host_id, path) DO UPDATE SET "
                    "session_id = excluded.session_id, repo = excluded.repo, branch = excluded.branch, "
                    "base_sha = excluded.base_sha, observed_head = excluded.observed_head, "
                    "ownership = excluded.ownership, reasons_json = excluded.reasons_json, "
                    "observation_json = excluded.observation_json, observed_at = excluded.observed_at",
                    [
                        host_id,
                        tree["path"],
                        tree.get("session_id"),
                        tree.get("repo"),
                        tree.get("branch"),
                        tree.get("base_sha"),
                        tree.get("observed_head"),
                        tree["ownership"],
                        json.dumps(tree["reasons"]),
                        json.dumps(tree),
                        now,
                    ],
                )
