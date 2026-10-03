"""Durable report consumption at the Factory observer/owner adapter boundary.

Uses the existing control store and outbox conventions: immutable identities,
transactional intent, bounded leases, at-least-once delivery, fenced ack. There
is no executor, scheduler, TaskFlow client, or permission/approval mutation.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from drover.server.control_outbox import canonical_payload, is_postgres_connection
from drover.server.db import control_plane_connection
from drover.server.harness.factory_observer import factory_observer_projection
from drover.server.harness.registry import HarnessRegistry

MAX_DELIVERIES = 3
MAX_LEASE_SECONDS = 300
SCOPES = {
    "implementation": "commit_only",
    "integration": "publish_review",
    "deployment": "explicit_approval_required",
}
ACTION_TYPES = {
    "worker_awaiting_input": "request_input",
    "worker_brief_completed": "review_worker_result",
    "ci_red": "correct_ci",
    "ci_green": "review_ci",
}


class ContinuityConflict(ValueError):
    """An owner fence or immutable identity does not match."""


def continuity_request(store: FactoryObserverContinuity, body: dict) -> dict:
    """Fixed boundary operations for OpenClaw and future owner adapters."""
    contracts = {
        "initialize": ({"session_id", "objective", "checkpoint"}, {"authority_scope"}),
        "lease": ({"run_id", "owner_id"}, {"owner_epoch", "lease_seconds"}),
        "report": (
            {
                "run_id",
                "source",
                "source_event_id",
                "subject",
                "sequence",
                "kind",
                "summary",
            },
            set(),
        ),
        "consume": ({"run_id", "owner_id", "owner_epoch"}, set()),
        "acknowledge": (
            {"run_id", "owner_id", "owner_epoch", "event_id", "checkpoint"},
            set(),
        ),
    }
    operation = body.get("operation")
    if not isinstance(operation, str) or operation not in contracts:
        raise ValueError("unsupported continuity operation")
    required, optional = contracts[operation]
    args = {key: value for key, value in body.items() if key != "operation"}
    if not required <= args.keys() or args.keys() - (required | optional):
        raise ValueError("missing or unsupported continuity fields")
    # getattr is fenced by the fixed operation allowlist above.
    result = getattr(store, operation)(**args)
    run_id = result if operation == "initialize" else args["run_id"]
    response = {"continuity": store.status(run_id)}
    if operation == "report":
        response["event_id"] = result
    if operation == "consume":
        response["delivery"] = result
    return response


def _text(value: str, name: str, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be non-empty text of at most {limit} characters")
    return value


def _integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _row(con, sql, params):
    result = con.execute(sql, params)
    row = result.fetchone()
    return dict(zip((col[0] for col in result.description), row)) if row else None


class FactoryObserverContinuity:
    """Local continuity facts correlated to an existing observer launch."""

    def __init__(
        self, path: str | Path, *, clock: Callable[[], datetime] | None = None
    ):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @contextmanager
    def _transaction(self, run_id: str | None = None):
        with control_plane_connection(self.path, timeout=5) as con:
            con.execute("BEGIN")
            try:
                run = None
                if run_id is not None:
                    _text(run_id, "run_id", 191)
                    lock = " FOR UPDATE" if is_postgres_connection(con) else ""
                    run = _row(
                        con,
                        "SELECT * FROM factory_observer_runs WHERE run_id = ?" + lock,
                        [run_id],
                    )
                    if run is None:
                        raise KeyError(run_id)
                yield con, run
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

    def initialize(
        self,
        *,
        session_id: str,
        objective: str,
        checkpoint: str,
        authority_scope: str = "implementation",
    ) -> str:
        """Idempotent opt-in; never launch or change the correlated session."""
        _text(session_id, "session_id", 191)
        _text(objective, "objective")
        _text(checkpoint, "checkpoint")
        if authority_scope not in SCOPES:
            raise ValueError("unsupported authority_scope")
        session = HarnessRegistry(self.path).get_session(session_id)
        projection = factory_observer_projection(session)
        if projection is None:
            raise ValueError("an existing Factory observer session is required")
        run_id = projection["run_id"]
        with self._transaction() as (con, _):
            con.execute(
                """INSERT INTO factory_observer_runs
                (run_id, session_id, expected_revision, objective, checkpoint,
                 authority_scope, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id) DO NOTHING""",
                [
                    run_id,
                    session_id,
                    projection["expected_revision"],
                    objective,
                    checkpoint,
                    authority_scope,
                    self.clock(),
                ],
            )
            existing = _row(
                con, "SELECT * FROM factory_observer_runs WHERE run_id = ?", [run_id]
            )
            # A replay must not replace an advanced checkpoint or broaden scope.
            for name, value in {
                "session_id": session_id,
                "objective": objective,
                "authority_scope": authority_scope,
                "expected_revision": projection["expected_revision"],
            }.items():
                if existing[name] != value:
                    raise ContinuityConflict(f"run {name} is immutable")
        return run_id

    def lease(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_seconds: int = 60,
        owner_epoch: int | None = None,
    ) -> dict:
        _text(owner_id, "owner_id", 191)
        _integer(lease_seconds, "lease_seconds", 1, MAX_LEASE_SECONDS)
        with self._transaction(run_id) as (con, run):
            now = self.clock()
            live = run["lease_until"] is not None and run["lease_until"] > now
            if live:
                self._fence(run, owner_id, owner_epoch, now)
                epoch = run["owner_epoch"]
            else:
                epoch = run["owner_epoch"] + 1
            until = now + timedelta(seconds=lease_seconds)
            con.execute(
                """UPDATE factory_observer_runs SET owner_id = ?,
                owner_epoch = ?, lease_until = ?, updated_at = ? WHERE run_id = ?""",
                [owner_id, epoch, until, now, run_id],
            )
        return {
            "owner_id": owner_id,
            "owner_epoch": epoch,
            "lease_until": until.isoformat(),
        }

    @staticmethod
    def _fence(run, owner_id, owner_epoch, now):
        if (
            not run["owner_id"]
            or run["owner_id"] != owner_id
            or type(owner_epoch) is not int
            or run["owner_epoch"] != owner_epoch
            or run["lease_until"] is None
            or run["lease_until"] <= now
        ):
            raise ContinuityConflict("current owner lease and epoch are required")

    def report(
        self,
        run_id: str,
        *,
        source: str,
        source_event_id: str,
        subject: str,
        sequence: int,
        kind: str,
        summary: str,
    ) -> str:
        """Accept only bounded report facts; retries must retain source identity."""
        for name, value in {
            "source": source,
            "source_event_id": source_event_id,
            "subject": subject,
        }.items():
            _text(value, name, 191)
        _integer(sequence, "sequence", 0, 2**63 - 1)
        _text(summary, "summary", 2000)
        if kind not in ACTION_TYPES:
            raise ValueError("unsupported report kind")
        payload = canonical_payload(
            dict(
                source=source,
                source_event_id=source_event_id,
                subject=subject,
                sequence=sequence,
                kind=kind,
                summary=summary,
            )
        )
        event_id = hashlib.sha256(
            canonical_payload(
                {"run_id": run_id, "source": source, "source_event_id": source_event_id}
            ).encode()
        ).hexdigest()
        with self._transaction(run_id) as (con, _):
            existing = _row(
                con,
                "SELECT payload_json FROM factory_observer_inbox WHERE event_id = ?",
                [event_id],
            )
            if existing:
                if existing["payload_json"] != payload:
                    raise ContinuityConflict(
                        "source event identity has divergent content"
                    )
                return event_id
            con.execute(
                """INSERT INTO factory_observer_inbox
                (event_id, run_id, source, subject, sequence, payload_json, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [event_id, run_id, source, subject, sequence, payload, self.clock()],
            )
        return event_id

    def consume(self, run_id: str, *, owner_id: str, owner_epoch: int) -> dict | None:
        """Commit one action intent before delivery; never perform that action.

        The same outstanding action is redelivered with bounded backoff. A new
        lease recovers it without resetting attempts or changing its identity.
        """
        with self._transaction(run_id) as (con, run):
            now = self.clock()
            self._fence(run, owner_id, owner_epoch, now)
            action = (
                json.loads(run["next_action_json"]) if run["next_action_json"] else None
            )
            if action:
                event = _row(
                    con,
                    "SELECT * FROM factory_observer_inbox WHERE event_id = ?",
                    [action["event_id"]],
                )
                if event["state"] == "exhausted" or event["next_delivery_at"] > now:
                    return None
                if event["attempts"] >= MAX_DELIVERIES:
                    con.execute(
                        "UPDATE factory_observer_inbox SET state = 'exhausted' WHERE event_id = ?",
                        [event["event_id"]],
                    )
                    return None
            else:
                event = _row(
                    con,
                    """SELECT * FROM factory_observer_inbox
                    WHERE run_id = ? AND state = 'pending'
                    ORDER BY received_at, event_id LIMIT 1""",
                    [run_id],
                )
                if event is None:
                    return None
                report = json.loads(event["payload_json"])
                latest = con.execute(
                    """SELECT max(sequence) FROM factory_observer_inbox
                    WHERE run_id = ? AND source = ? AND subject = ? AND state <> 'pending'""",
                    [run_id, event["source"], event["subject"]],
                ).fetchone()[0]
                late = latest is not None and event["sequence"] <= latest
                scope = run["authority_scope"]
                action = {
                    "action_id": "observer-action-" + event["event_id"],
                    "event_id": event["event_id"],
                    "type": (
                        "review_late_report" if late else ACTION_TYPES[report["kind"]]
                    ),
                    "authority_scope": scope,
                    "authorized": scope != "deployment",
                }
                if scope == "deployment":
                    action["type"] = "request_explicit_approval"
                if not late and report["kind"].startswith("worker_"):
                    con.execute(
                        "UPDATE factory_observer_runs SET worker_state = ? WHERE run_id = ?",
                        [report["kind"].removeprefix("worker_"), run_id],
                    )
                con.execute(
                    "UPDATE factory_observer_runs SET next_action_json = ?, updated_at = ? WHERE run_id = ?",
                    [canonical_payload(action), now, run_id],
                )
            attempts = event["attempts"] + 1
            con.execute(
                """UPDATE factory_observer_inbox SET state = 'delivered',
                action_json = ?, attempts = ?, next_delivery_at = ? WHERE event_id = ?""",
                [
                    canonical_payload(action),
                    attempts,
                    now + timedelta(seconds=5 * 2 ** (attempts - 1)),
                    event["event_id"],
                ],
            )
        return action

    def acknowledge(
        self,
        run_id: str,
        *,
        owner_id: str,
        owner_epoch: int,
        event_id: str,
        checkpoint: str,
    ) -> None:
        """Atomically ack an owner continuation and persist its new checkpoint."""
        _text(event_id, "event_id", 64)
        _text(checkpoint, "checkpoint")
        with self._transaction(run_id) as (con, run):
            now = self.clock()
            self._fence(run, owner_id, owner_epoch, now)
            event = _row(
                con,
                "SELECT * FROM factory_observer_inbox WHERE run_id = ? AND event_id = ?",
                [run_id, event_id],
            )
            if event is None:
                raise KeyError(event_id)
            if event["state"] == "acknowledged":
                return  # A replay must never clobber a newer checkpoint/action.
            if not event["action_json"]:
                raise ContinuityConflict("consume the report before acknowledging")
            if not json.loads(event["action_json"])["authorized"]:
                raise ContinuityConflict(
                    "explicit deployment approval is required outside this bridge"
                )
            con.execute(
                "UPDATE factory_observer_inbox SET state = 'acknowledged', acknowledged_at = ? WHERE event_id = ?",
                [now, event_id],
            )
            con.execute(
                """UPDATE factory_observer_runs SET checkpoint = ?,
                next_action_json = NULL, updated_at = ? WHERE run_id = ?""",
                [checkpoint, now, run_id],
            )

    def status(self, run_id: str, *, limit: int = 20) -> dict:
        """Bounded recovery projection; polling neither delivers nor acknowledges."""
        _integer(limit, "limit", 1, 50)
        with self._transaction(run_id) as (con, run):
            now = self.clock()
            live = run["lease_until"] is not None and run["lease_until"] > now
            counts = dict(
                con.execute(
                    "SELECT state, count(*) FROM factory_observer_inbox WHERE run_id = ? GROUP BY state",
                    [run_id],
                ).fetchall()
            )
            result = con.execute(
                """SELECT event_id, state, attempts, next_delivery_at,
                payload_json FROM factory_observer_inbox WHERE run_id = ? AND state <> 'acknowledged'
                ORDER BY received_at, event_id LIMIT ?""",
                [run_id, limit],
            )
            events = [
                {
                    "event_id": eid,
                    "state": state,
                    "attempts": attempts,
                    "next_delivery_at": due.isoformat() if due else None,
                    "report": json.loads(payload),
                }
                for eid, state, attempts, due, payload in result.fetchall()
            ]
            action = (
                json.loads(run["next_action_json"]) if run["next_action_json"] else None
            )
            blocked = run["authority_scope"] == "deployment"
            recovery = (
                ("claim_owner" if not run["owner_id"] else "reclaim_expired_owner")
                if not live
                else (
                    "approval_required"
                    if blocked
                    else (
                        "manual_attention"
                        if counts.get("exhausted")
                        else "continue_owner"
                    )
                )
            )
            return {
                "version": 1,
                "run_id": run_id,
                "session_id": run["session_id"],
                "expected_revision": run["expected_revision"],
                "authority": "taskflow",
                "objective": run["objective"],
                "checkpoint": run["checkpoint"],
                "authority_scope": run["authority_scope"],
                "scope_limit": SCOPES[run["authority_scope"]],
                "owner": {
                    "id": run["owner_id"],
                    "epoch": run["owner_epoch"],
                    "lease_until": (
                        run["lease_until"].isoformat() if run["lease_until"] else None
                    ),
                    "live": live,
                },
                "worker_state": run["worker_state"],
                "terminal_release": False,
                "next_action": action,
                "recovery": recovery,
                "owner_wake": bool(
                    live
                    and not blocked
                    and not counts.get("exhausted")
                    and (action or counts.get("pending"))
                ),
                "inbox_counts": counts,
                "events": events,
                "has_more": sum(
                    n for state, n in counts.items() if state != "acknowledged"
                )
                > len(events),
            }
