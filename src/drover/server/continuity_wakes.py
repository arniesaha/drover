"""Durable, issue-level continuity wake outbox.

The hub records wake intent here; it never delivers chat messages or performs
GitHub writes.  Owners claim an issue lease, consume one wake, reconcile it
idempotently by ``wake_id``, then explicitly acknowledge it.  Consequently a
lost owner turn is visible and re-delivered rather than silently dropped.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from drover.server.continuity_leases import LeaseConflict, OwnerLeaseStore
from drover.server.control_outbox import canonical_payload, is_postgres_connection
from drover.server.db import control_plane_connection

MAX_DELIVERIES = 3
MAX_LEASE_SECONDS = 300
MAX_WAKE_PAYLOAD_BYTES = 16_384

# Used by the legacy DuckDB control-plane bootstrap. PostgreSQL uses the
# numbered additive migration in postgres_schema.py.
CONTINUITY_WAKES_DDL = (
    """CREATE TABLE IF NOT EXISTS continuity_owner_leases (
      issue_key TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
      owner_epoch BIGINT NOT NULL CHECK (owner_epoch > 0),
      lease_until TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS continuity_wakes (
      wake_id TEXT PRIMARY KEY, issue_key TEXT NOT NULL, action TEXT NOT NULL,
      fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN
        ('pending', 'delivered', 'acknowledged', 'exhausted')),
      owner_id TEXT, owner_epoch BIGINT, delivery_token TEXT,
      attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0 AND attempts <= 3),
      next_delivery_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(), delivered_at TIMESTAMPTZ,
      acknowledged_at TIMESTAMPTZ, exhausted_at TIMESTAMPTZ,
      UNIQUE (issue_key, action, fingerprint)
    )""",
    # Existing DuckDB compatibility stores predate the per-delivery fence.
    "ALTER TABLE continuity_wakes ADD COLUMN IF NOT EXISTS delivery_token TEXT",
    "CREATE INDEX IF NOT EXISTS continuity_wakes_delivery ON continuity_wakes (issue_key, state, next_delivery_at, created_at, wake_id)",
)


class ContinuityWakeConflict(ValueError):
    """A wake identity or owner lease fence did not match."""


def _text(value: str, name: str, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be non-empty text of at most {limit} characters")
    return value


def _integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _row(con, sql: str, params: list):
    result = con.execute(sql, params)
    row = result.fetchone()
    return dict(zip((column[0] for column in result.description), row)) if row else None


def _wake_id(issue_key: str, action: str, fingerprint: str) -> str:
    return hashlib.sha256(
        canonical_payload(
            {"issue_key": issue_key, "action": action, "fingerprint": fingerprint}
        ).encode()
    ).hexdigest()


class ContinuityWakes:
    """Transactional issue wake producer/owner-consumer service.

    Authentication is intentionally owned by the HTTP/tool boundary, as it is
    for ``FactoryObserverContinuity``.  This service accepts a trusted owner
    identity and fences every state-changing consumer operation with its lease
    epoch.  It is safe to construct per request.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        visibility_seconds: int = 30,
    ):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        _integer(visibility_seconds, "visibility_seconds", 1, 3600)
        self.visibility_seconds = visibility_seconds

    @contextmanager
    def _transaction(self):
        with control_plane_connection(self.path, timeout=5) as con:
            con.execute("BEGIN")
            try:
                yield con
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

    @staticmethod
    def _lease(con, issue_key: str):
        lock = " FOR UPDATE" if is_postgres_connection(con) else ""
        return _row(
            con,
            "SELECT * FROM continuity_owner_leases WHERE issue_key = ?" + lock,
            [issue_key],
        )

    @staticmethod
    def _fence(lease: dict | None, owner_id: str, owner_epoch: int, now: datetime):
        if (
            lease is None
            or lease["owner_id"] != owner_id
            or type(owner_epoch) is not int
            or lease["owner_epoch"] != owner_epoch
            or lease["lease_until"] is None
            or lease["lease_until"] <= now
        ):
            raise ContinuityWakeConflict("current owner lease and epoch are required")

    def enqueue(
        self,
        *,
        issue_key: str,
        action: str,
        fingerprint: str,
        payload: dict,
    ) -> str:
        """Record a wake once; a duplicate must retain exactly the same payload."""
        _text(issue_key, "issue_key", 191)
        _text(action, "action", 191)
        _text(fingerprint, "fingerprint", 512)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        payload_json = canonical_payload(payload)
        if len(payload_json.encode()) > MAX_WAKE_PAYLOAD_BYTES:
            raise ValueError("payload exceeds bounded wake size")
        wake_id = _wake_id(issue_key, action, fingerprint)
        with self._transaction() as con:
            # The unique wake identity is the concurrency fence.  Do not use a
            # check-then-insert here: two producers can both observe no row,
            # and one must safely reselect the winner after its conflict.
            con.execute(
                """INSERT INTO continuity_wakes
                (wake_id, issue_key, action, fingerprint, payload_json, next_delivery_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (wake_id) DO NOTHING""",
                [wake_id, issue_key, action, fingerprint, payload_json, self.clock()],
            )
            existing = _row(
                con,
                "SELECT payload_json FROM continuity_wakes WHERE wake_id = ?",
                [wake_id],
            )
            # A successful insert must be immediately visible in our
            # transaction; failing closed is safer than returning an identity
            # whose canonical payload we could not verify.
            if existing is None:
                raise RuntimeError("wake insert was not durable")
            if existing["payload_json"] != payload_json:
                raise ContinuityWakeConflict("wake identity has divergent payload")
        return wake_id

    def lease(
        self,
        issue_key: str,
        *,
        owner_id: str,
        lease_seconds: int = 60,
        owner_epoch: int | None = None,
    ) -> dict:
        _text(issue_key, "issue_key", 191)
        _text(owner_id, "owner_id", 191)
        _integer(lease_seconds, "lease_seconds", 1, MAX_LEASE_SECONDS)
        with self._transaction() as con:
            now = self.clock()
            current = self._lease(con, issue_key)
            live = bool(current and current["lease_until"] > now)
            store = OwnerLeaseStore(con, clock=self.clock)
            try:
                if live:
                    if owner_epoch is None:
                        raise ContinuityWakeConflict(
                            "owner epoch is required to renew a live lease"
                        )
                    result = store.renew(
                        issue_key,
                        owner_id=owner_id,
                        owner_epoch=owner_epoch,
                        lease_seconds=lease_seconds,
                    )
                else:
                    if owner_epoch is not None and (
                        current is None or owner_epoch != current["owner_epoch"]
                    ):
                        raise ContinuityWakeConflict(
                            "owner epoch changed; read recovery status"
                        )
                    result = store.acquire(
                        issue_key,
                        owner_id=owner_id,
                        lease_seconds=lease_seconds,
                    )
            except LeaseConflict as exc:
                raise ContinuityWakeConflict(str(exc)) from exc
        return result

    def consume(
        self, issue_key: str, *, owner_id: str, owner_epoch: int
    ) -> dict | None:
        """Deliver the oldest due wake under a live owner lease.

        The returned ``delivery_token`` fences this specific delivery and is
        required to acknowledge it; ``wake_id`` remains its durable identity.
        Delivery is at-least-once: failed or killed consumers leave the row
        delivered until its visibility deadline, at which point it is eligible
        again.
        """
        _text(issue_key, "issue_key", 191)
        _text(owner_id, "owner_id", 191)
        with self._transaction() as con:
            now = self.clock()
            self._fence(self._lease(con, issue_key), owner_id, owner_epoch, now)
            # Expire an already-delivered wake before selecting.  History is
            # retained; no row is deleted, and attempt counters never reset.
            con.execute(
                """UPDATE continuity_wakes SET state = 'exhausted', exhausted_at = ?
                 WHERE issue_key = ? AND state = 'delivered'
                   AND next_delivery_at <= ? AND attempts >= ?""",
                [now, issue_key, now, MAX_DELIVERIES],
            )
            lock = " FOR UPDATE" if is_postgres_connection(con) else ""
            wake = _row(
                con,
                """SELECT * FROM continuity_wakes
                   WHERE issue_key = ?
                     AND state IN ('pending', 'delivered')
                     AND next_delivery_at <= ?
                   ORDER BY next_delivery_at, created_at, wake_id LIMIT 1""" + lock,
                [issue_key, now],
            )
            if wake is None:
                return None
            attempts = wake["attempts"] + 1
            # The visibility timeout grows on each delivery, limiting retries
            # while preserving a deterministic recovery window.
            visible_until = now + timedelta(
                seconds=self.visibility_seconds * 2 ** (attempts - 1)
            )
            delivery_token = uuid4().hex
            con.execute(
                """UPDATE continuity_wakes
                   SET state = 'delivered', attempts = ?, next_delivery_at = ?,
                       owner_id = ?, owner_epoch = ?, delivery_token = ?, delivered_at = ?
                 WHERE wake_id = ?""",
                [
                    attempts,
                    visible_until,
                    owner_id,
                    owner_epoch,
                    delivery_token,
                    now,
                    wake["wake_id"],
                ],
            )
            return {
                "wake_id": wake["wake_id"],
                "issue_key": wake["issue_key"],
                "action": wake["action"],
                "fingerprint": wake["fingerprint"],
                "payload": json.loads(wake["payload_json"]),
                "attempts": attempts,
                "delivery_token": delivery_token,
                "next_delivery_at": visible_until.isoformat(),
            }

    def acknowledge(
        self,
        issue_key: str,
        *,
        owner_id: str,
        owner_epoch: int,
        wake_id: str,
        delivery_token: str,
    ) -> None:
        _text(issue_key, "issue_key", 191)
        _text(owner_id, "owner_id", 191)
        if not isinstance(wake_id, str) or len(wake_id) != 64:
            raise ValueError("wake_id must be a SHA-256 identity")
        if not isinstance(delivery_token, str) or len(delivery_token) != 32:
            raise ValueError("delivery_token must be a delivery claim token")
        with self._transaction() as con:
            now = self.clock()
            self._fence(self._lease(con, issue_key), owner_id, owner_epoch, now)
            wake = _row(
                con,
                "SELECT * FROM continuity_wakes WHERE issue_key = ? AND wake_id = ?",
                [issue_key, wake_id],
            )
            if wake is None:
                raise KeyError(wake_id)
            # Only the exact delivery can be retried idempotently.  In
            # particular, attempt 1 must never be able to acknowledge an
            # attempt-2 redelivery under the same owner epoch.
            if wake["state"] == "acknowledged":
                if (
                    wake["owner_id"] == owner_id
                    and wake["owner_epoch"] == owner_epoch
                    and wake["delivery_token"] == delivery_token
                ):
                    return
                raise ContinuityWakeConflict(
                    "wake was acknowledged by a different delivery"
                )
            if wake["state"] != "delivered":
                raise ContinuityWakeConflict("consume the wake before acknowledging")
            if (
                wake["owner_id"] != owner_id
                or wake["owner_epoch"] != owner_epoch
                or wake["delivery_token"] != delivery_token
            ):
                raise ContinuityWakeConflict(
                    "wake was delivered under a different owner epoch or delivery"
                )
            con.execute(
                """UPDATE continuity_wakes SET state = 'acknowledged', acknowledged_at = ?
                   WHERE wake_id = ?""",
                [now, wake_id],
            )

    def status(self, issue_key: str, *, limit: int = 20) -> dict:
        """Bounded recovery projection; polling never delivers or acknowledges."""
        _text(issue_key, "issue_key", 191)
        _integer(limit, "limit", 1, 50)
        with self._transaction() as con:
            now = self.clock()
            lease = self._lease(con, issue_key)
            counts = dict(
                con.execute(
                    "SELECT state, count(*) FROM continuity_wakes WHERE issue_key = ? GROUP BY state",
                    [issue_key],
                ).fetchall()
            )
            rows = con.execute(
                """SELECT wake_id, action, fingerprint, payload_json, state, attempts, next_delivery_at
                   FROM continuity_wakes WHERE issue_key = ? AND state <> 'acknowledged'
                   ORDER BY created_at, wake_id LIMIT ?""",
                [issue_key, limit],
            ).fetchall()
            wakes = [
                {
                    "wake_id": wake_id,
                    "action": action,
                    "fingerprint": fingerprint,
                    "payload": json.loads(payload),
                    "state": state,
                    "attempts": attempts,
                    "next_delivery_at": due.isoformat() if due else None,
                }
                for wake_id, action, fingerprint, payload, state, attempts, due in rows
            ]
            live = bool(lease and lease["lease_until"] and lease["lease_until"] > now)
            return {
                "issue_key": issue_key,
                "owner": {
                    "id": lease["owner_id"] if lease else None,
                    "epoch": lease["owner_epoch"] if lease else 0,
                    "lease_until": (
                        lease["lease_until"].isoformat()
                        if lease and lease["lease_until"]
                        else None
                    ),
                    "live": live,
                },
                "wake_counts": counts,
                "wakes": wakes,
                "has_more": sum(counts.values()) - counts.get("acknowledged", 0)
                > len(wakes),
            }
