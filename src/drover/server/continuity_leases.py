"""Deadline policy, fenced continuity ownership, and non-destructive reaping.

This module deliberately does not call harnessd itself.  It selects and records
safe candidates; an injected terminator performs the external side effect only
when ``dry_run`` is false.  That keeps the default path observational and makes
both the fence and every applied action auditable.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Iterator, Mapping
from uuid import uuid4

from drover.server.control_outbox import is_postgres_connection

DEFAULT_DEADLINES_SECONDS = {
    "working": 45 * 60,
    "idle_undeclared": 10 * 60,
    "pr_ci_pending": 40 * 60,
    "release_running": 60 * 60,
    "needs_approval": 0,
}
MAX_LEASE_SECONDS = 24 * 60 * 60


class LeaseConflict(RuntimeError):
    """The caller no longer owns the current fenced lease."""


@dataclass(frozen=True)
class DeadlinePolicy:
    """Expected durations by projected worker state or pipeline stage."""

    seconds: Mapping[str, int] = field(
        default_factory=lambda: DEFAULT_DEADLINES_SECONDS
    )

    def budget(self, state_or_stage: str) -> timedelta:
        try:
            seconds = self.seconds[state_or_stage]
        except KeyError as exc:
            raise KeyError(
                f"no continuity deadline configured for {state_or_stage!r}"
            ) from exc
        if type(seconds) is not int or seconds < 0:
            raise ValueError("deadline seconds must be a non-negative integer")
        return timedelta(seconds=seconds)

    def due_at(self, state_or_stage: str, state_at: datetime) -> datetime:
        return _utc(state_at) + self.budget(state_or_stage)

    def overdue(self, state_or_stage: str, state_at: datetime, now: datetime) -> bool:
        return _utc(now) >= self.due_at(state_or_stage, state_at)


class OwnerLeaseStore:
    """CAS owner leases for issue-level continuity work.

    A new claimant advances ``owner_epoch``. Renew/release require the exact
    owner and epoch, so a delayed prior owner cannot act after takeover.
    """

    def __init__(self, con: Any, *, clock: Callable[[], datetime] | None = None):
        self.con = con
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def acquire(
        self, issue_key: str, *, owner_id: str, lease_seconds: int = 60
    ) -> dict:
        _validate_text(issue_key, "issue_key")
        _validate_text(owner_id, "owner_id")
        _validate_lease_seconds(lease_seconds)
        now = _utc(self.clock())
        # psycopg's control connection is deliberately autocommit=True.  A
        # transaction-scoped advisory lock makes the missing-row INSERT race
        # impossible as well as serialising expired-lease takeover.
        with _transaction(self.con):
            _advisory_lock(self.con, issue_key)
            row = _one(
                self.con,
                "SELECT issue_key, owner_id, owner_epoch, lease_until "
                "FROM continuity_owner_leases WHERE issue_key = ?" + _lock(self.con),
                [issue_key],
            )
            until = now + timedelta(seconds=lease_seconds)
            if row is None:
                epoch = 1
                _write_lease(
                    self.con,
                    "INSERT INTO continuity_owner_leases "
                    "(issue_key, owner_id, owner_epoch, lease_until, updated_at) VALUES (?, ?, ?, ?, ?)",
                    [issue_key, owner_id, epoch, until, now],
                )
            elif _live(row.get("lease_until"), now):
                if row["owner_id"] != owner_id:
                    raise LeaseConflict("issue has a live owner lease")
                epoch = row["owner_epoch"]
                _write_lease(
                    self.con,
                    "UPDATE continuity_owner_leases SET lease_until = ?, updated_at = ? "
                    "WHERE issue_key = ? AND owner_id = ? AND owner_epoch = ?",
                    [until, now, issue_key, owner_id, epoch],
                )
            else:
                epoch = int(row["owner_epoch"]) + 1
                _write_lease(
                    self.con,
                    "UPDATE continuity_owner_leases SET owner_id = ?, owner_epoch = ?, "
                    "lease_until = ?, updated_at = ? WHERE issue_key = ? AND owner_epoch = ?",
                    [owner_id, epoch, until, now, issue_key, row["owner_epoch"]],
                )
        return {
            "issue_key": issue_key,
            "owner_id": owner_id,
            "owner_epoch": epoch,
            "lease_until": until.isoformat(),
        }

    def renew(
        self,
        issue_key: str,
        *,
        owner_id: str,
        owner_epoch: int,
        lease_seconds: int = 60,
    ) -> dict:
        _validate_lease_seconds(lease_seconds)
        with _transaction(self.con):
            _advisory_lock(self.con, issue_key)
            row, now = self._fenced(issue_key, owner_id, owner_epoch)
            until = now + timedelta(seconds=lease_seconds)
            _write_lease(
                self.con,
                "UPDATE continuity_owner_leases SET lease_until = ?, updated_at = ? "
                "WHERE issue_key = ? AND owner_id = ? AND owner_epoch = ?",
                [until, now, issue_key, owner_id, owner_epoch],
            )
        return {
            "issue_key": issue_key,
            "owner_id": owner_id,
            "owner_epoch": row["owner_epoch"],
            "lease_until": until.isoformat(),
        }

    def release(self, issue_key: str, *, owner_id: str, owner_epoch: int) -> None:
        with _transaction(self.con):
            _advisory_lock(self.con, issue_key)
            self._fenced(issue_key, owner_id, owner_epoch)
            now = _utc(self.clock())
            _write_lease(
                self.con,
                "UPDATE continuity_owner_leases SET lease_until = ?, updated_at = ? "
                "WHERE issue_key = ? AND owner_id = ? AND owner_epoch = ?",
                [now, now, issue_key, owner_id, owner_epoch],
            )

    def _fenced(
        self, issue_key: str, owner_id: str, owner_epoch: int
    ) -> tuple[dict, datetime]:
        _validate_text(issue_key, "issue_key")
        _validate_text(owner_id, "owner_id")
        if type(owner_epoch) is not int or owner_epoch < 1:
            raise LeaseConflict("owner epoch is required")
        now = _utc(self.clock())
        row = _one(
            self.con,
            "SELECT issue_key, owner_id, owner_epoch, lease_until FROM continuity_owner_leases WHERE issue_key = ?"
            + _lock(self.con),
            [issue_key],
        )
        if (
            row is None
            or row.get("owner_id") != owner_id
            or row.get("owner_epoch") != owner_epoch
            or not _live(row.get("lease_until"), now)
        ):
            raise LeaseConflict("current owner lease and epoch are required")
        return row, now


@dataclass(frozen=True)
class ReapCandidate:
    session_id: str
    reason: str
    state_at: datetime | None = None
    protections: Mapping[str, bool] = field(default_factory=dict)


def select_reap_candidates(
    sessions: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    enabled_watchers: Iterable[str] = (),
) -> list[ReapCandidate]:
    """Return only sessions safe to reap under the documented conservative rule.

    Callers must supply verified open-PR/live-lease facts, never infer absent
    evidence.  ``done`` requires accepted (merged) PR evidence; ``ended`` is
    terminal; undeclared idleness must exceed 24 hours.
    """
    now = _utc(now)
    watched = set(enabled_watchers)
    candidates: list[ReapCandidate] = []
    for row in sessions:
        sid = str(row["session_id"])
        protections = {
            "has_open_pr": row.get("has_open_pr"),
            "has_live_lease": row.get("has_live_lease"),
            "has_watcher": True if sid in watched else row.get("has_watcher"),
        }
        # Absence is unknown, not false.  Reaping must never use a partially
        # hydrated projection as evidence that a safeguard is absent.
        if any(value is not False for value in protections.values()):
            continue
        state = row.get("worker_state")
        if state == "done" and row.get("accepted_pr"):
            candidates.append(
                ReapCandidate(
                    sid, "accepted_done", _as_datetime(row.get("state_at")), protections
                )
            )
        elif state == "ended" or row.get("status") in {
            "completed",
            "terminated",
            "errored",
            "failed",
        }:
            candidates.append(
                ReapCandidate(
                    sid, "ended", _as_datetime(row.get("state_at")), protections
                )
            )
        elif state == "idle_undeclared":
            at = _as_datetime(row.get("state_at") or row.get("last_activity"))
            if at is not None and now - at > timedelta(hours=24):
                candidates.append(
                    ReapCandidate(sid, "idle_undeclared_24h", at, protections)
                )
    return candidates


def reap(
    con: Any,
    candidates: Iterable[ReapCandidate],
    *,
    actor: str,
    dry_run: bool = True,
    terminator: Callable[[str], None] | None = None,
    protection_check: Callable[[str], Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Audit candidates and optionally terminate/archive them; never delete data."""
    _validate_text(actor, "actor")
    at = _utc(now or datetime.now(timezone.utc))
    results = []
    for candidate in candidates:
        record = {
            "session_id": candidate.session_id,
            "reason": candidate.reason,
            "dry_run": dry_run,
            "applied": False,
        }
        if not dry_run:
            if terminator is None or protection_check is None:
                _audit(con, candidate, actor, dry_run, at, "reap_failed")
                record["error"] = "terminator and protection_check are required"
                results.append(record)
                continue
            # Recheck protections and insert durable intent under a session
            # fence.  The commit intentionally precedes the external call.
            operation_id = _prepare_reap(con, candidate, actor, at, protection_check)
            if operation_id is None:
                _audit(con, candidate, actor, dry_run, at, "reap_failed")
                record["error"] = "reap protected or already in progress"
                results.append(record)
                continue
            try:
                acknowledged = terminator(candidate.session_id)
                if acknowledged is not True:
                    raise RuntimeError("terminator did not acknowledge termination")
                con.execute(
                    "UPDATE harness_sessions SET archived_at = COALESCE(archived_at, ?) WHERE session_id = ?",
                    [at, candidate.session_id],
                )
            except Exception as exc:
                _finish_reap(con, operation_id, "failed", at)
                _audit(con, candidate, actor, dry_run, at, "reap_failed")
                record["error"] = str(exc)
                results.append(record)
                continue
            _finish_reap(con, operation_id, "confirmed", at)
            _audit(con, candidate, actor, dry_run, at, "reap_confirmed")
            record["applied"] = True
            results.append(record)
            continue
        _audit(con, candidate, actor, dry_run, at, "reap")
        results.append(record)
    return results


def _audit(
    con: Any,
    candidate: ReapCandidate,
    actor: str,
    dry_run: bool,
    at: datetime,
    action: str,
) -> None:
    con.execute(
        "INSERT INTO continuity_reap_audit (audit_id, session_id, action, reason, actor, dry_run, applied_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            str(uuid4()),
            candidate.session_id,
            "reap",
            candidate.reason,
            actor,
            dry_run,
            at,
        ],
    )


def _protections_clear(evidence: Mapping[str, Any]) -> bool:
    return all(
        evidence.get(key) is False
        for key in ("has_open_pr", "has_live_lease", "has_watcher")
    )


def _prepare_reap(
    con: Any,
    candidate: ReapCandidate,
    actor: str,
    at: datetime,
    protection_check: Callable[[str], Mapping[str, Any]],
) -> str | None:
    """Claim one durable in-flight operation before the external call.

    The transaction lock is released before calling harnessd, but the unique
    per-session operation row remains the durable fence. A second sweeper sees
    ``intent``/``confirmed`` and cannot invoke the terminator. Failed operations
    may be reclaimed after protections are rechecked.
    """
    with _transaction(con):
        _advisory_lock(con, "reap:" + candidate.session_id)
        current = _one(
            con,
            "SELECT operation_id, action FROM continuity_reap_operations "
            "WHERE session_id = ?" + _lock(con),
            [candidate.session_id],
        )
        if current is not None and current["action"] in {"intent", "confirmed"}:
            return None
        if not _protections_clear(protection_check(candidate.session_id)):
            return None
        operation_id = current["operation_id"] if current is not None else str(uuid4())
        if current is None:
            con.execute(
                "INSERT INTO continuity_reap_operations "
                "(operation_id, session_id, action, reason, actor, recorded_at) "
                "VALUES (?, ?, 'intent', ?, ?, ?)",
                [operation_id, candidate.session_id, candidate.reason, actor, at],
            )
        else:
            con.execute(
                "UPDATE continuity_reap_operations SET action = 'intent', "
                "reason = ?, actor = ?, recorded_at = ? "
                "WHERE operation_id = ? AND action = 'failed'",
                [candidate.reason, actor, at, operation_id],
            )
        _audit(con, candidate, actor, False, at, "reap_intent")
    return operation_id


def _finish_reap(con: Any, operation_id: str, action: str, at: datetime) -> None:
    if action not in {"confirmed", "failed"}:
        raise ValueError("reap outcome must be confirmed or failed")
    with _transaction(con):
        result = con.execute(
            "UPDATE continuity_reap_operations SET action = ?, recorded_at = ? "
            "WHERE operation_id = ? AND action = 'intent'",
            [action, at, operation_id],
        )
        if is_postgres_connection(con) and result.rowcount != 1:
            raise RuntimeError("reap operation fence changed before outcome")


def _lock(con: Any) -> str:
    return " FOR UPDATE" if is_postgres_connection(con) else ""


@contextmanager
def _transaction(con: Any) -> Iterator[None]:
    """Make lease read-modify-write atomic on autocommit PostgreSQL.

    Compatibility stores already provide their own single-process semantics;
    keeping them out of explicit transactions also preserves their lightweight
    test and tooling connections.
    """
    if not is_postgres_connection(con):
        yield
        return
    con.execute("BEGIN")
    try:
        yield
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def _advisory_lock(con: Any, issue_key: str) -> None:
    if is_postgres_connection(con):
        con.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", [issue_key])


def _write_lease(con: Any, statement: str, params: list[Any]) -> None:
    """Fence every PostgreSQL mutation with a returned row, not optimism."""
    if not is_postgres_connection(con):
        con.execute(statement, params)
        return
    row = _one(con, statement + " RETURNING issue_key", params)
    if row is None:
        raise LeaseConflict("lease changed before the fenced write completed")


def _one(con: Any, statement: str, params: list[Any]) -> dict | None:
    cursor = con.execute(statement, params)
    if cursor is None:  # lightweight compatibility/test connections
        return None
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return dict(zip((item[0] for item in cursor.description), value))


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _utc(value) if isinstance(value, datetime) else None


def _live(value: Any, now: datetime) -> bool:
    parsed = _as_datetime(value)
    return parsed is not None and parsed > now


def _validate_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 191:
        raise ValueError(f"{name} must be a non-empty string up to 191 characters")


def _validate_lease_seconds(value: int) -> None:
    if type(value) is not int or not 1 <= value <= MAX_LEASE_SECONDS:
        raise ValueError(
            f"lease_seconds must be an integer from 1 to {MAX_LEASE_SECONDS}"
        )
