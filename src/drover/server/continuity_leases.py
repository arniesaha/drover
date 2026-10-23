"""Deadline policy, fenced continuity ownership, and non-destructive reaping.

This module deliberately does not call harnessd itself.  It selects and records
safe candidates; an injected terminator performs the external side effect only
when ``dry_run`` is false.  That keeps the default path observational and makes
both the fence and every applied action auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping
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
        row = _one(
            self.con,
            "SELECT issue_key, owner_id, owner_epoch, lease_until "
            "FROM continuity_owner_leases WHERE issue_key = ?" + _lock(self.con),
            [issue_key],
        )
        until = now + timedelta(seconds=lease_seconds)
        if row is None:
            epoch = 1
            self.con.execute(
                "INSERT INTO continuity_owner_leases "
                "(issue_key, owner_id, owner_epoch, lease_until, updated_at) VALUES (?, ?, ?, ?, ?)",
                [issue_key, owner_id, epoch, until, now],
            )
        elif _live(row.get("lease_until"), now):
            if row["owner_id"] != owner_id:
                raise LeaseConflict("issue has a live owner lease")
            # Acquiring our own live lease is a fenced renewal, not a takeover.
            epoch = row["owner_epoch"]
            self.con.execute(
                "UPDATE continuity_owner_leases SET lease_until = ?, updated_at = ? "
                "WHERE issue_key = ? AND owner_id = ? AND owner_epoch = ?",
                [until, now, issue_key, owner_id, epoch],
            )
        else:
            epoch = int(row["owner_epoch"]) + 1
            self.con.execute(
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
        row, now = self._fenced(issue_key, owner_id, owner_epoch)
        until = now + timedelta(seconds=lease_seconds)
        self.con.execute(
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
        self._fenced(issue_key, owner_id, owner_epoch)
        self.con.execute(
            "UPDATE continuity_owner_leases SET lease_until = ?, updated_at = ? "
            "WHERE issue_key = ? AND owner_id = ? AND owner_epoch = ?",
            [_utc(self.clock()), _utc(self.clock()), issue_key, owner_id, owner_epoch],
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
        if sid in watched or row.get("has_open_pr") or row.get("has_live_lease"):
            continue
        state = row.get("worker_state")
        if state == "done" and row.get("accepted_pr"):
            candidates.append(
                ReapCandidate(sid, "accepted_done", _as_datetime(row.get("state_at")))
            )
        elif state == "ended" or row.get("status") in {
            "completed",
            "terminated",
            "errored",
            "failed",
        }:
            candidates.append(
                ReapCandidate(sid, "ended", _as_datetime(row.get("state_at")))
            )
        elif state == "idle_undeclared":
            at = _as_datetime(row.get("state_at") or row.get("last_activity"))
            if at is not None and now - at > timedelta(hours=24):
                candidates.append(ReapCandidate(sid, "idle_undeclared_24h", at))
    return candidates


def reap(
    con: Any,
    candidates: Iterable[ReapCandidate],
    *,
    actor: str,
    dry_run: bool = True,
    terminator: Callable[[str], None] | None = None,
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
            if terminator is not None:
                terminator(candidate.session_id)
            con.execute(
                "UPDATE harness_sessions SET archived_at = COALESCE(archived_at, ?) WHERE session_id = ?",
                [at, candidate.session_id],
            )
            record["applied"] = True
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
        results.append(record)
    return results


def _lock(con: Any) -> str:
    return " FOR UPDATE" if is_postgres_connection(con) else ""


def _one(con: Any, statement: str, params: list[Any]) -> dict | None:
    cursor = con.execute(statement, params)
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
