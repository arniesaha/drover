"""Durable report consumption at the Factory observer/owner adapter boundary.

Uses the existing control store and outbox conventions: immutable identities,
transactional intent, bounded leases, at-least-once delivery, fenced ack. There
is no executor, scheduler, TaskFlow client, or permission/approval mutation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from drover.server.control_outbox import (
    canonical_payload,
    event_payload_expression,
    event_payload_join,
    is_postgres_connection,
)
from drover.server.db import control_plane_connection
from drover.server.harness.factory_observer import factory_observer_projection
from drover.server.harness.registry import HarnessRegistry

MAX_DELIVERIES = 3
MAX_LEASE_SECONDS = 300
MAX_COMPLETION_PROJECTION_BATCH = 100
SCOPES = {
    "implementation": "commit_only",
    "integration": "publish_review",
    "deployment": "explicit_approval_required",
}
ACTION_TYPES = {
    "worker_started": "observe_worker_start",
    "worker_awaiting_input": "request_input",
    "worker_brief_completed": "review_worker_result",
    "worker_done": "review_worker_result",
    "worker_blocked": "resolve_worker_block",
    "worker_needs_approval": "request_approved_gate",
    "ci_red": "correct_ci",
    "ci_green": "review_ci",
}


class ContinuityConflict(ValueError):
    """An owner fence or immutable identity does not match."""


def continuity_request(store: FactoryObserverContinuity, body: dict) -> dict:
    """Fixed boundary operations for OpenClaw and future owner adapters."""
    contracts = {
        "initialize": (
            {"session_id", "objective", "checkpoint"},
            {"authority_scope", "authorized_owner_id", "handoff_token"},
        ),
        "issue_handoff": (
            {"session_id", "objective", "checkpoint", "authorized_owner_id"},
            {"authority_scope", "issued_by"},
        ),
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
    run_id = (
        result["run_id"]
        if operation == "issue_handoff"
        else result if operation == "initialize" else args["run_id"]
    )
    response = {"continuity": store.status(run_id)}
    if operation == "issue_handoff":
        response["handoff_token"] = result["handoff_token"]
    if operation == "report":
        response["event_id"] = result
    if operation == "consume":
        response["delivery"] = result
    return response


def _text(value: str, name: str, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be non-empty text of at most {limit} characters")
    return value


def _handoff_token_sha256(token: str) -> str:
    _text(token, "handoff_token", 512)
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _row(con, sql, params):
    result = con.execute(sql, params)
    row = result.fetchone()
    return dict(zip((col[0] for col in result.description), row)) if row else None


def _lock_observer_session(con, session_id: str):
    """Lock the stable observer key shared by projection and first handoff."""
    lock = " FOR UPDATE" if is_postgres_connection(con) else ""
    return _row(
        con,
        "SELECT handoff_mode FROM harness_sessions WHERE session_id = ?" + lock,
        [session_id],
    )


def lock_observer_sessions(con, session_ids) -> None:
    """Acquire all observer-session locks in one global order for a batch."""
    for session_id in sorted({str(session_id) for session_id in session_ids}):
        _lock_observer_session(con, session_id)


def _validate_report(
    *,
    source: str,
    source_event_id: str,
    subject: str,
    sequence: int,
    kind: str,
    summary: str,
) -> str:
    """Validate one immutable report and return its canonical payload."""
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
    return payload


def _report_event_id(run_id: str, source: str, source_event_id: str) -> str:
    return hashlib.sha256(
        canonical_payload(
            {"run_id": run_id, "source": source, "source_event_id": source_event_id}
        ).encode()
    ).hexdigest()


def _record_report(
    con,
    *,
    run_id: str,
    source: str,
    source_event_id: str,
    subject: str,
    sequence: int,
    kind: str,
    summary: str,
    received_at: datetime,
) -> str:
    """Insert a report using the caller's already-open control transaction."""
    payload = _validate_report(
        source=source,
        source_event_id=source_event_id,
        subject=subject,
        sequence=sequence,
        kind=kind,
        summary=summary,
    )
    event_id = _report_event_id(run_id, source, source_event_id)
    existing = _row(
        con,
        "SELECT payload_json FROM factory_observer_inbox WHERE event_id = ?",
        [event_id],
    )
    if existing:
        if existing["payload_json"] != payload:
            raise ContinuityConflict("source event identity has divergent content")
        return event_id
    con.execute(
        """INSERT INTO factory_observer_inbox
        (event_id, run_id, source, subject, sequence, payload_json, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [event_id, run_id, source, subject, sequence, payload, received_at],
    )
    return event_id


def _record_completion_receipt(
    con,
    *,
    harness_event_id: str,
    run_id: str | None,
    session_id: str,
    sequence: int | None,
    state: str,
    reason: str | None,
    continuity_event_id: str | None,
    recorded_at: datetime,
) -> str:
    """Durably retain one completion-projection result by harness event ID."""
    existing = _row(
        con,
        "SELECT state FROM factory_observer_completion_receipts WHERE harness_event_id = ?",
        [harness_event_id],
    )
    if existing is not None:
        # A completion received before the run was initialized or its owner
        # handoff was authorized is a durable pending fact, not a terminal
        # rejection. Once the authorized handoff exists, reconcile that exact
        # existing harness event to its one projected action. Other outcomes
        # are immutable, so replay cannot mutate history.
        if (
            existing["state"] != "pending_authorization"
            or state == "pending_authorization"
        ):
            return existing["state"]
        con.execute(
            """UPDATE factory_observer_completion_receipts
               SET run_id = ?, session_id = ?, sequence = ?, state = ?, reason = ?,
                   continuity_event_id = ?, recorded_at = ?
             WHERE harness_event_id = ?""",
            [
                run_id,
                session_id,
                sequence,
                state,
                reason,
                continuity_event_id,
                recorded_at,
                harness_event_id,
            ],
        )
        return state
    con.execute(
        """INSERT INTO factory_observer_completion_receipts
        (harness_event_id, run_id, session_id, sequence, state, reason,
         continuity_event_id, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            harness_event_id,
            run_id,
            session_id,
            sequence,
            state,
            reason,
            continuity_event_id,
            recorded_at,
        ],
    )
    return state


def _completion_payload(payload: dict | None) -> dict | None:
    """Return the completion member from flat or EventPusher wire payloads."""
    if not isinstance(payload, dict):
        return None
    inner = payload.get("payload")
    if isinstance(inner, dict) and "turn_complete" in inner:
        return inner
    return payload if "turn_complete" in payload else None


def record_observer_worker_completion(
    con,
    *,
    event_id: str,
    session_id: str,
    event_type: str,
    sequence: int | None,
    payload: dict | None,
    received_at: datetime,
) -> str | None:
    """Project one Drover completion and retain its truthful durable outcome.

    Only Factory observer sessions enter this ledger.  Each relevant source
    event has one receipt, including a malformed completion or a completion
    received before an authorized owner handoff.  The caller keeps all writes
    in its existing event transaction, so an inserted event cannot lose a
    successful projection and a failed projection cannot disappear silently.
    """
    completion = _completion_payload(payload)
    if completion is None:
        return None
    session = _lock_observer_session(con, session_id)
    if session is None or session["handoff_mode"] != "factory_observer":
        return None
    # Serialize projection with owner-handoff redemption. The session row
    # exists before a run does, so it also protects the first-run gap where a
    # missing run cannot be locked. Without it, ingestion can commit a pending
    # receipt after redemption's recovery scan and strand a valid completion.
    lock = " FOR UPDATE" if is_postgres_connection(con) else ""
    run = _row(
        con,
        "SELECT run_id FROM factory_observer_runs WHERE session_id = ?" + lock,
        [session_id],
    )
    if run is None:
        return _record_completion_receipt(
            con,
            harness_event_id=event_id,
            run_id=None,
            session_id=session_id,
            sequence=sequence if type(sequence) is int else None,
            state="pending_authorization",
            reason="observer_run_not_initialized",
            continuity_event_id=None,
            recorded_at=received_at,
        )
    if completion.get("turn_complete") is not True:
        return _record_completion_receipt(
            con,
            harness_event_id=event_id,
            run_id=run["run_id"],
            session_id=session_id,
            sequence=sequence if type(sequence) is int else None,
            state="rejected",
            reason="completion_requires_true_turn_complete",
            continuity_event_id=None,
            recorded_at=received_at,
        )
    if event_type != "status":
        return _record_completion_receipt(
            con,
            harness_event_id=event_id,
            run_id=run["run_id"],
            session_id=session_id,
            sequence=sequence if type(sequence) is int else None,
            state="rejected",
            reason="completion_requires_status_event",
            continuity_event_id=None,
            recorded_at=received_at,
        )
    if type(sequence) is not int or sequence < 0:
        return _record_completion_receipt(
            con,
            harness_event_id=event_id,
            run_id=run["run_id"],
            session_id=session_id,
            sequence=None,
            state="rejected",
            reason="completion_requires_nonnegative_sequence",
            continuity_event_id=None,
            recorded_at=received_at,
        )
    owner = _row(
        con,
        "SELECT authorized_owner_id FROM factory_observer_runs WHERE run_id = ?",
        [run["run_id"]],
    )
    if not owner or not owner["authorized_owner_id"]:
        return _record_completion_receipt(
            con,
            harness_event_id=event_id,
            run_id=run["run_id"],
            session_id=session_id,
            sequence=sequence,
            state="pending_authorization",
            reason="authorized_owner_handoff_required",
            continuity_event_id=None,
            recorded_at=received_at,
        )
    continuity_event_id = _record_report(
        con,
        run_id=run["run_id"],
        source="drover",
        source_event_id=event_id,
        subject=session_id,
        sequence=sequence,
        kind="worker_brief_completed",
        summary="Drover harness session reported a completed turn.",
        received_at=received_at,
    )
    return _record_completion_receipt(
        con,
        harness_event_id=event_id,
        run_id=run["run_id"],
        session_id=session_id,
        sequence=sequence,
        state="projected",
        reason=None,
        continuity_event_id=continuity_event_id,
        recorded_at=received_at,
    )


def record_work_coordinator_state(
    con,
    *,
    event_id: str,
    session_id: str,
    event_type: str,
    sequence: int | None,
    payload: dict | None,
    received_at: datetime,
) -> str | None:
    """Project explicit coordinator worker states into the same durable inbox.

    Factory sessions retain their TaskFlow completion contract. Coordinator
    sessions opt in only through their committed launch binding, and all state
    reports use the immutable harness event identity for replay safety.
    """
    if event_type != "assistant_output" or type(sequence) is not int or sequence < 0:
        return None
    session = _lock_observer_session(con, session_id)
    if session is None or session["handoff_mode"] != "work_coordinator":
        return None
    run = _row(
        con,
        "SELECT run_id FROM factory_observer_runs WHERE session_id = ?",
        [session_id],
    )
    if run is None:
        return None
    from drover.server.harness.events import is_text_stream_delta
    from drover.server.harness.worker_state import _message_text, parse_state_line

    # The persisted envelope carries the message in ``text`` next to an empty
    # ``payload`` object, so an inner-payload-first lookup misses every
    # declaration (#809's worker declared done and nobody heard).
    if is_text_stream_delta(payload):
        return None
    parsed = parse_state_line(_message_text(payload))
    if not parsed or parsed["state"] not in _DECLARED_KINDS:
        return None
    return _record_declared_state(
        con,
        run_id=run["run_id"],
        event_id=event_id,
        session_id=session_id,
        sequence=sequence,
        parsed=parsed,
        received_at=received_at,
    )


_DECLARED_KINDS = {
    "done": "worker_done",
    "blocked": "worker_blocked",
    "needs_approval": "worker_needs_approval",
}


def _record_declared_state(
    con, *, run_id, event_id, session_id, sequence, parsed, received_at
) -> str:
    """One identity for a declared state, shared by ingest and the sweep."""
    detail = parsed.get("reason") or parsed.get("sha") or parsed["state"]
    return _record_report(
        con,
        run_id=run_id,
        source="drover-work-coordinator",
        source_event_id=event_id,
        subject=session_id,
        sequence=sequence,
        kind=_DECLARED_KINDS[parsed["state"]],
        summary=f"Worker declared {parsed['state']}: {detail}",
        received_at=received_at,
    )


IDLE_SOURCE = "drover-work-coordinator-idle"
DEFAULT_IDLE_UNDECLARED_SECONDS = 300
_IDLE_SWEEP_LIMIT = 50
_IDLE_EVENT_SCAN = 200
_IDLE_EXCERPT = 1200
_ENDED = ("completed", "terminated", "errored", "failed")


def _final_turn_output(
    con, session_id: str
) -> tuple[str, int, str, dict | None] | None:
    """Event id, sequence, text and declared state of the turn's last output.

    None when the turn produced no final output. A declared terminal state
    anywhere in the turn wins over the last message.
    """
    from drover.server.harness.events import is_text_stream_delta
    from drover.server.harness.worker_state import _message_text, parse_state_line

    rows = con.execute(
        f"""SELECT e.event_id, e.event_type, e.seq,
                   {event_payload_expression(con)} AS payload_json
              FROM harness_events e {event_payload_join(con)}
             WHERE e.session_id = ? AND e.event_type IN ('assistant_output', 'user_input')
             ORDER BY e.created_at DESC, e.seq DESC, e.event_id DESC
             LIMIT {_IDLE_EVENT_SCAN}""",
        [session_id],
    ).fetchall()
    final = None
    for event_id, event_type, seq, payload_json in rows:
        if event_type == "user_input":
            break
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or is_text_stream_delta(payload):
            continue
        text = _message_text(payload)
        if not text or not text.strip():
            continue
        entry = (str(event_id), seq if type(seq) is int and seq >= 0 else 0, text)
        parsed = parse_state_line(text)
        if parsed and parsed["state"] in _DECLARED_KINDS:
            return (*entry, parsed)
        if final is None:
            final = (*entry, None)
    return final


def sweep_idle_undeclared(
    con,
    *,
    now: datetime,
    deadline_seconds: int = DEFAULT_IDLE_UNDECLARED_SECONDS,
    limit: int = _IDLE_SWEEP_LIMIT,
) -> list[str]:
    """Return the new report ids for workers idle at ``awaiting: input``.

    Real workers routinely finish and stop without a ``Drover-State`` line. Once
    such a worker has produced output and been idle for the deadline, one
    ``worker_brief_completed`` fact carrying its final output wakes the bound
    owner, who decides done or blocked. A turn that did declare done, blocked or
    needs-approval but whose ingest-time projection was missed is reconciled to
    the same fact the projection would have written. The identity is the worker's last output
    event, so repeated sweeps and replays are no-ops while a new turn that ends
    idle again is a new fact. Runs inside the caller's transaction.
    """
    _integer(deadline_seconds, "deadline_seconds", 1, 7 * 24 * 3600)
    cutoff = now - timedelta(seconds=deadline_seconds)
    lock = " FOR UPDATE OF s, r SKIP LOCKED" if is_postgres_connection(con) else ""
    placeholders = ", ".join("?" for _ in _ENDED)
    candidates = con.execute(
        f"""SELECT s.session_id, r.run_id
              FROM harness_sessions s
              JOIN factory_observer_runs r ON r.session_id = s.session_id
             WHERE s.handoff_mode = 'work_coordinator' AND s.awaiting = 'input'
               AND s.status NOT IN ({placeholders})
               AND s.last_activity <= ?
               AND r.authorized_owner_id IS NOT NULL
             ORDER BY s.last_activity, s.session_id
             LIMIT ?{lock}""",
        [*_ENDED, cutoff, limit],
    ).fetchall()
    recorded = []
    for session_id, run_id in candidates:
        final = _final_turn_output(con, str(session_id))
        if final is None:
            continue
        source_event_id, sequence, text, declared = final
        source = "drover-work-coordinator" if declared else IDLE_SOURCE
        if _row(
            con,
            "SELECT 1 AS present FROM factory_observer_inbox WHERE event_id = ?",
            [_report_event_id(run_id, source, source_event_id)],
        ):
            continue
        if declared:
            # Ingest-time projection missed this declaration (it is the same
            # identity, so a later replay of the event is a no-op).
            try:
                recorded.append(
                    _record_declared_state(
                        con,
                        run_id=run_id,
                        event_id=source_event_id,
                        session_id=str(session_id),
                        sequence=sequence,
                        parsed=declared,
                        received_at=now,
                    )
                )
            except ContinuityConflict:
                pass
            continue
        excerpt = " ".join(text.split())[:_IDLE_EXCERPT]
        try:
            recorded.append(
                _record_report(
                    con,
                    run_id=run_id,
                    source=IDLE_SOURCE,
                    source_event_id=source_event_id,
                    subject=str(session_id),
                    sequence=sequence,
                    kind="worker_brief_completed",
                    summary=(
                        f"idle_undeclared: worker idle at awaiting:input for "
                        f"{deadline_seconds}s without declaring done, blocked or "
                        f"needs-approval. Decide which from its final output: {excerpt}"
                    ),
                    received_at=now,
                )
            )
        except ContinuityConflict:
            continue
    return recorded


def record_observer_worker_completion_batch(con, records: list[dict]) -> dict[str, int]:
    """Project one bounded batch in source order using the caller transaction."""
    if len(records) > MAX_COMPLETION_PROJECTION_BATCH:
        raise ValueError("completion projection batch exceeds maximum")
    outcomes: dict[str, int] = {}
    for record in sorted(
        records,
        key=lambda item: (
            str(item["session_id"]),
            (
                item["sequence"]
                if type(item.get("sequence")) is int and item["sequence"] >= 0
                else 2**63
            ),
            item["received_at"],
            str(item["event_id"]),
        ),
    ):
        state = record_observer_worker_completion(
            con,
            event_id=str(record["event_id"]),
            session_id=str(record["session_id"]),
            event_type=str(record["event_type"]),
            sequence=record.get("sequence"),
            payload=record.get("payload"),
            received_at=record["received_at"],
        )
        if state is not None:
            outcomes[state] = outcomes.get(state, 0) + 1
    return outcomes


def _recover_pending_completions(con, *, session_id: str) -> None:
    """Reproject pending source facts after their first authorized handoff.

    Harness-event ingestion cannot reinsert an already durable event ID, so
    authorization must reconcile the retained pending receipt from the stored
    immutable event instead of waiting for an unreliable duplicate delivery.
    This caller is already inside initialize's transaction.
    """
    rows = con.execute(
        f"""SELECT e.event_id, e.session_id, e.event_type,
                   {event_payload_expression(con)} AS payload_json,
                   e.seq, e.created_at
              FROM factory_observer_completion_receipts r
              JOIN harness_events e ON e.event_id = r.harness_event_id
                   {event_payload_join(con)}
             WHERE r.session_id = ? AND r.state = 'pending_authorization'
             ORDER BY CASE WHEN e.seq IS NULL THEN 1 ELSE 0 END,
                      e.seq, e.created_at, e.event_id""",
        [session_id],
    ).fetchall()
    records = []
    for (
        event_id,
        event_session_id,
        event_type,
        payload_json,
        sequence,
        created_at,
    ) in rows:
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            payload = None
        records.append(
            {
                "event_id": str(event_id),
                "session_id": str(event_session_id),
                "event_type": str(event_type),
                "sequence": sequence,
                "payload": payload,
                "received_at": created_at,
            }
        )
    for offset in range(0, len(records), MAX_COMPLETION_PROJECTION_BATCH):
        record_observer_worker_completion_batch(
            con, records[offset : offset + MAX_COMPLETION_PROJECTION_BATCH]
        )


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

    def sweep_idle_undeclared(
        self, deadline_seconds: int = DEFAULT_IDLE_UNDECLARED_SECONDS
    ) -> list[str]:
        """One bounded sweep; see :func:`sweep_idle_undeclared`."""
        with self._transaction() as (con, _):
            return sweep_idle_undeclared(
                con, now=self.clock(), deadline_seconds=deadline_seconds
            )

    def initialize(
        self,
        *,
        session_id: str,
        objective: str,
        checkpoint: str,
        authority_scope: str = "implementation",
        authorized_owner_id: str | None = None,
        handoff_token: str | None = None,
    ) -> str:
        """Idempotent opt-in; never launch or change the correlated session."""
        _text(session_id, "session_id", 191)
        _text(objective, "objective")
        _text(checkpoint, "checkpoint")
        if authorized_owner_id is not None:
            _text(authorized_owner_id, "authorized_owner_id", 191)
        if handoff_token is not None:
            _handoff_token_sha256(handoff_token)
            if authorized_owner_id is None:
                raise PermissionError("handoff redemption requires an owner identity")
        if authority_scope not in SCOPES:
            raise ValueError("unsupported authority_scope")
        session = HarnessRegistry(self.path).get_session(session_id)
        projection = factory_observer_projection(session)
        if projection is None:
            raise ValueError("an existing Factory observer session is required")
        run_id = projection["run_id"]
        with self._transaction() as (con, _):
            _lock_observer_session(con, session_id)
            con.execute(
                """INSERT INTO factory_observer_runs
                (run_id, session_id, expected_revision, objective, checkpoint,
                 authority_scope, authorized_owner_id, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id) DO NOTHING""",
                [
                    run_id,
                    session_id,
                    projection["expected_revision"],
                    objective,
                    checkpoint,
                    authority_scope,
                    authorized_owner_id,
                    self.clock(),
                ],
            )
            lock = " FOR UPDATE" if is_postgres_connection(con) else ""
            existing = _row(
                con,
                "SELECT * FROM factory_observer_runs WHERE run_id = ?" + lock,
                [run_id],
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
            existing_owner = existing.get("authorized_owner_id")
            if handoff_token is not None:
                handoff_owner = existing.get("handoff_owner_id")
                token_sha256 = existing.get("handoff_token_sha256")
                if (
                    handoff_owner != authorized_owner_id
                    or not token_sha256
                    or not hmac.compare_digest(
                        token_sha256, _handoff_token_sha256(handoff_token)
                    )
                ):
                    # A bearer has proved its identity at the HTTP edge, but
                    # not that it was named by the observer-session handoff.
                    raise PermissionError("owner is not authorized for this handoff")
                if existing_owner is None:
                    con.execute(
                        """UPDATE factory_observer_runs
                        SET authorized_owner_id = ?, handoff_redeemed_at = ?, updated_at = ?
                        WHERE run_id = ?""",
                        [authorized_owner_id, self.clock(), self.clock(), run_id],
                    )
                    _recover_pending_completions(con, session_id=session_id)
                elif existing_owner != authorized_owner_id:
                    raise PermissionError("owner is not authorized for this handoff")
            elif existing_owner is None and authorized_owner_id is not None:
                # An existing local observer can receive its first explicit
                # trusted-adapter handoff. The HTTP boundary requires the
                # separately issued bearer-bound token path above.
                con.execute(
                    "UPDATE factory_observer_runs SET authorized_owner_id = ?, updated_at = ? WHERE run_id = ?",
                    [authorized_owner_id, self.clock(), run_id],
                )
            elif existing_owner != authorized_owner_id and (
                existing_owner is not None or authorized_owner_id is not None
            ):
                raise ContinuityConflict("run authorized owner is immutable")
            if authorized_owner_id is not None:
                _recover_pending_completions(con, session_id=session_id)
        return run_id

    def issue_handoff(
        self,
        *,
        session_id: str,
        objective: str,
        checkpoint: str,
        authorized_owner_id: str,
        issued_by: str,
        authority_scope: str = "implementation",
    ) -> dict[str, str]:
        """Persist a one-time host/operator handoff for a named device owner.

        Only the HTTP boundary may identify a host-bound issuer.  The raw
        token is returned once to that trusted caller; the ledger retains only
        its digest and the target identity until the target bearer redeems it.
        """
        _text(session_id, "session_id", 191)
        _text(objective, "objective")
        _text(checkpoint, "checkpoint")
        _text(authorized_owner_id, "authorized_owner_id", 191)
        _text(issued_by, "issued_by", 191)
        if authority_scope not in SCOPES:
            raise ValueError("unsupported authority_scope")
        session = HarnessRegistry(self.path).get_session(session_id)
        projection = factory_observer_projection(session)
        if projection is None:
            raise ValueError("an existing Factory observer session is required")
        run_id = projection["run_id"]
        handoff_token = secrets.token_urlsafe(32)
        token_sha256 = _handoff_token_sha256(handoff_token)
        with self._transaction() as (con, _):
            _lock_observer_session(con, session_id)
            con.execute(
                """INSERT INTO factory_observer_runs
                (run_id, session_id, expected_revision, objective, checkpoint,
                 authority_scope, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?)
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
            lock = " FOR UPDATE" if is_postgres_connection(con) else ""
            existing = _row(
                con,
                "SELECT * FROM factory_observer_runs WHERE run_id = ?" + lock,
                [run_id],
            )
            for name, value in {
                "session_id": session_id,
                "objective": objective,
                "authority_scope": authority_scope,
                "expected_revision": projection["expected_revision"],
            }.items():
                if existing[name] != value:
                    raise ContinuityConflict(f"run {name} is immutable")
            if existing.get("authorized_owner_id"):
                raise ContinuityConflict("run authorized owner is immutable")
            previous_target = existing.get("handoff_owner_id")
            if previous_target not in {None, authorized_owner_id}:
                raise ContinuityConflict("run handoff owner is immutable")
            con.execute(
                """UPDATE factory_observer_runs
                SET handoff_owner_id = ?, handoff_token_sha256 = ?,
                    handoff_issued_by = ?, handoff_issued_at = ?, updated_at = ?
                WHERE run_id = ?""",
                [
                    authorized_owner_id,
                    token_sha256,
                    issued_by,
                    self.clock(),
                    self.clock(),
                    run_id,
                ],
            )
        return {"run_id": run_id, "handoff_token": handoff_token}

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
            self._authorized_owner(run, owner_id)
            # A delayed renewal is a compare-and-swap even after expiry. It
            # must not silently reacquire after a newer owner epoch expired.
            if owner_epoch is not None and (
                type(owner_epoch) is not int or owner_epoch != run["owner_epoch"]
            ):
                raise ContinuityConflict("owner epoch changed; read recovery status")
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
    def _authorized_owner(run, owner_id):
        authorized = run.get("authorized_owner_id")
        if not authorized:
            raise ContinuityConflict("authorized owner handoff is required")
        if authorized != owner_id:
            raise ContinuityConflict("owner is not authorized for this run")

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
        with self._transaction(run_id) as (con, _):
            return _record_report(
                con,
                run_id=run_id,
                source=source,
                source_event_id=source_event_id,
                subject=subject,
                sequence=sequence,
                kind=kind,
                summary=summary,
                received_at=self.clock(),
            )

    def consume(self, run_id: str, *, owner_id: str, owner_epoch: int) -> dict | None:
        """Commit one action intent before delivery; never perform that action.

        The same outstanding action is redelivered with bounded backoff. A new
        lease recovers it without resetting attempts or changing its identity.
        """
        with self._transaction(run_id) as (con, run):
            now = self.clock()
            self._authorized_owner(run, owner_id)
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
            self._authorized_owner(run, owner_id)
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
            receipt_counts = dict(
                con.execute(
                    "SELECT state, count(*) FROM factory_observer_completion_receipts "
                    "WHERE run_id = ? GROUP BY state",
                    [run_id],
                ).fetchall()
            )
            receipt_errors = [
                {"harness_event_id": event_id, "state": state, "reason": reason}
                for event_id, state, reason in con.execute(
                    """SELECT harness_event_id, state, reason
                    FROM factory_observer_completion_receipts
                    WHERE run_id = ? AND state <> 'projected'
                    ORDER BY recorded_at, harness_event_id LIMIT ?""",
                    [run_id, limit],
                ).fetchall()
            ]
            if receipt_counts.get("rejected"):
                handoff_state = "failed"
            elif not run.get("authorized_owner_id"):
                handoff_state = "authorization_required"
            elif action:
                handoff_state = "acknowledgement_required"
            elif counts.get("pending") or counts.get("delivered"):
                handoff_state = "owner_lease_required"
            elif counts.get("acknowledged"):
                handoff_state = "acknowledged"
            else:
                handoff_state = "awaiting_completion"
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
                "owner_handoff": {
                    "authorized_owner_id": run.get("authorized_owner_id"),
                    "state": handoff_state,
                },
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
                "completion_receipts": {
                    "counts": receipt_counts,
                    "errors": receipt_errors,
                },
                "events": events,
                "has_more": sum(
                    n for state, n in counts.items() if state != "acknowledged"
                )
                > len(events),
            }
