"""Per-device and per-host credentials for the drover-server API.

One record type covers phones and harness hosts; they differ only by
``scope``. The store keeps a SHA-256 verifier and never the token itself: the
plaintext exists once, in the pairing response, and thereafter only on the
client. That is what makes a lost phone a one-line revocation instead of a
fleet-wide token rotation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

CREDENTIALS_FILENAME = "credentials.json"
STORE_VERSION = 1
TOKEN_BYTES = 32
TOUCH_DEBOUNCE_SECONDS = 60.0
SCOPES = ("device", "host", "preflight", "profile")
APNS_ENVIRONMENTS = ("sandbox", "production")
_VERIFIER_DOMAIN = b"drover-cred-v1\0"


def verifier_from_token(token: str) -> str:
    digest = hashlib.sha256(_VERIFIER_DOMAIN + token.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def apns_registration_fingerprint(token: str, environment: str) -> str:
    """Identify a rejected (token, environment) pair without keeping the token.

    A token Apple refused is re-sent by the app on every cold launch, so the
    hub has to recognise it to keep answering "push unavailable". The pair
    matters, not the token alone: the same token is valid in the other
    environment when the app simply reported the wrong one.
    """
    digest = hashlib.sha256(
        b"drover-apns-v1\0"
        + environment.encode("utf-8")
        + b"\0"
        + token.encode("utf-8")
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class Credential:
    id: str
    scope: str
    label: str
    verifier: str
    created_at: str
    host_id: str | None = None
    last_used_at: str | None = None
    revoked_at: str | None = None
    notification_mode: str = "action"
    notification_timezone: str = "UTC"
    notification_hour: int | None = None
    apns_token: str | None = None
    apns_environment: str | None = None
    #: Set when Apple permanently rejected this device's registration. The
    #: fingerprint (never the token) lets the registration route refuse the
    #: same dead token when the app re-sends it.
    apns_failure_reason: str | None = None
    apns_failed_at: str | None = None
    apns_failed_fingerprint: str | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def apns_registration_rejected(self, token: str, environment: str) -> bool:
        return (
            self.apns_failed_fingerprint is not None
            and self.apns_failed_fingerprint
            == apns_registration_fingerprint(token, environment)
        )

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "scope": self.scope,
            "label": self.label,
            "verifier": self.verifier,
            "created_at": self.created_at,
            "host_id": self.host_id,
            "last_used_at": self.last_used_at,
            "revoked_at": self.revoked_at,
            "notification_mode": self.notification_mode,
            "notification_timezone": self.notification_timezone,
            "notification_hour": self.notification_hour,
            "apns_token": self.apns_token,
            "apns_environment": self.apns_environment,
            "apns_failure_reason": self.apns_failure_reason,
            "apns_failed_at": self.apns_failed_at,
            "apns_failed_fingerprint": self.apns_failed_fingerprint,
        }

    def as_public_json(self) -> dict:
        """Return an explicit allowlist so future private fields stay private."""
        return {
            "id": self.id,
            "scope": self.scope,
            "label": self.label,
            "created_at": self.created_at,
            "host_id": self.host_id,
            "last_used_at": self.last_used_at,
            "revoked_at": self.revoked_at,
        }


class CredentialStore:
    """Credential records persisted as one owner-only JSON document."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._server_id = ""
        self._fleet_name = ""
        self._by_id: dict[str, Credential] = {}
        self._by_verifier: dict[str, str] = {}
        self._touched_at: dict[str, float] = {}
        self._digest_pending: dict[tuple[str, str], dict] = {}
        self._load()

    @property
    def server_id(self) -> str:
        return self._server_id

    @property
    def fleet_name(self) -> str:
        return self._fleet_name

    def issue(
        self, *, scope: str, label: str, host_id: str | None = None
    ) -> tuple[Credential, str]:
        if scope not in SCOPES:
            raise ValueError(f"unknown scope: {scope}")
        if scope == "host" and (not isinstance(host_id, str) or not host_id.strip()):
            raise ValueError("host credentials require host_id")
        token = secrets.token_urlsafe(TOKEN_BYTES)
        credential = Credential(
            id=str(uuid4()),
            scope=scope,
            label=label,
            verifier=verifier_from_token(token),
            created_at=_now_iso(),
            host_id=host_id,
        )
        with self._lock:
            if scope == "host":
                for previous in list(self._by_id.values()):
                    if (
                        previous.scope == "host"
                        and previous.host_id == host_id
                        and previous.is_active
                    ):
                        self._index(replace(previous, revoked_at=_now_iso()))
            self._index(credential)
            self._write()
        return credential, token

    def find_active(self, token: str) -> Credential | None:
        """Look up by verifier, so lookup cost never depends on the secret."""
        verifier = verifier_from_token(token)
        with self._lock:
            credential_id = self._by_verifier.get(verifier)
            return self._by_id.get(credential_id) if credential_id else None

    def find_for_revocation(self, token: str) -> Credential | None:
        """Resolve even a revoked verifier, only for idempotent self-revocation."""
        verifier = verifier_from_token(token)
        with self._lock:
            return next(
                (item for item in self._by_id.values() if item.verifier == verifier),
                None,
            )

    def get(self, credential_id: str) -> Credential | None:
        with self._lock:
            return self._by_id.get(credential_id)

    def set_apns_registration(
        self, credential_id: str, *, token: str, environment: str
    ) -> bool:
        if environment not in APNS_ENVIRONMENTS:
            raise ValueError(f"unknown APNs environment: {environment}")
        with self._lock:
            credential = self._by_id.get(credential_id)
            if (
                credential is None
                or not credential.is_active
                or credential.scope != "device"
            ):
                return False
            # APNs assigns one token to one app installation. Re-pairing the
            # same phone creates a fresh Drover credential, so transfer the
            # endpoint instead of letting an old notification preference keep
            # sending to that same installation.
            for other_id, other in list(self._by_id.items()):
                if (
                    other_id != credential_id
                    and other.scope == "device"
                    and other.is_active
                    and other.apns_token == token
                    and other.apns_environment == environment
                ):
                    self._by_id[other_id] = replace(
                        other,
                        apns_token=None,
                        apns_environment=None,
                    )
            self._by_id[credential_id] = replace(
                credential,
                apns_token=token,
                apns_environment=environment,
                apns_failure_reason=None,
                apns_failed_at=None,
                apns_failed_fingerprint=None,
            )
            self._write()
            return True

    def set_notification_preferences(
        self,
        credential_id: str,
        mode: str,
        *,
        timezone: str = "UTC",
        hour: int | None = None,
    ) -> bool:
        if mode not in {"all", "action", "digest"}:
            raise ValueError("invalid notification mode")
        if not isinstance(timezone, str) or not timezone:
            raise ValueError("invalid notification timezone")
        if hour is not None and (type(hour) is not int or not 0 <= hour <= 23):
            raise ValueError("invalid notification hour")
        with self._lock:
            credential = self._by_id.get(credential_id)
            if (
                credential is None
                or not credential.is_active
                or credential.scope != "device"
            ):
                return False
            self._by_id[credential_id] = replace(
                credential,
                notification_mode=mode,
                notification_timezone=timezone,
                notification_hour=hour,
            )
            self._write()
            return True

    def set_notification_mode(self, credential_id: str, mode: str) -> bool:
        return self.set_notification_preferences(credential_id, mode)

    def queue_digest(self, credential: Credential, transition, due_at: float) -> None:
        """Persist only digest routing metadata, never transcript previews/content."""
        item = {
            "credential_id": credential.id,
            "session_id": transition.session_id,
            "awaiting": transition.awaiting,
            "status": transition.status,
            "due_at": due_at,
            "update_count": 1,
        }
        with self._lock:
            previous = self._digest_pending.get((credential.id, transition.session_id))
            if previous:
                item["update_count"] = int(previous.get("update_count", 1)) + 1
            self._digest_pending[(credential.id, transition.session_id)] = item
            self._write()

    def pending_digests(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._digest_pending.values()]

    def claim_digests(
        self, credential_id: str, session_ids: list[str], now: float, owner: str
    ) -> dict[str, int]:
        """Claim local-file items. The PostgreSQL implementation fences workers."""
        with self._lock:
            return {
                session_id: int(item.get("update_count", 1))
                for session_id in session_ids
                if (item := self._digest_pending.get((credential_id, session_id)))
                and float(item.get("due_at", 0)) <= now
            }

    def cancel_digests(self, credential_id: str, session_ids: list[str]) -> None:
        with self._lock:
            for session_id in session_ids:
                self._digest_pending.pop((credential_id, session_id), None)
            self._write()

    def complete_digest(
        self, credential_id: str, claimed: dict[str, int], owner: str
    ) -> None:
        # Do not erase an update queued while the batch was in flight.
        with self._lock:
            for session_id, version in claimed.items():
                item = self._digest_pending.get((credential_id, session_id))
                if item is not None and int(item.get("update_count", 1)) == version:
                    self._digest_pending.pop((credential_id, session_id), None)
            self._write()

    def defer_digest(
        self, credential_id: str, claimed: dict[str, int], due_at: float, owner: str
    ) -> None:
        with self._lock:
            for session_id, version in claimed.items():
                item = self._digest_pending.get((credential_id, session_id))
                if item is not None and int(item.get("update_count", 1)) == version:
                    item["due_at"] = due_at
            self._write()

    def clear_apns_registration(
        self, credential_id: str, *, expected_token: str | None = None
    ) -> bool:
        with self._lock:
            credential = self._by_id.get(credential_id)
            if (
                credential is None
                or credential.apns_token is None
                or (
                    expected_token is not None
                    and credential.apns_token != expected_token
                )
            ):
                return False
            self._by_id[credential_id] = replace(
                credential,
                apns_token=None,
                apns_environment=None,
            )
            self._write()
            return True

    def mark_apns_registration_failed(
        self, credential_id: str, *, expected_token: str, reason: str
    ) -> bool:
        """Clear a registration Apple permanently refused, remembering why.

        ``expected_token`` guards against a send that raced a re-registration:
        a fresh token must not be condemned by its predecessor's rejection.
        """
        with self._lock:
            credential = self._by_id.get(credential_id)
            if (
                credential is None
                or credential.apns_token is None
                or credential.apns_token != expected_token
            ):
                return False
            self._by_id[credential_id] = replace(
                credential,
                apns_token=None,
                apns_environment=None,
                apns_failure_reason=reason,
                apns_failed_at=_now_iso(),
                apns_failed_fingerprint=apns_registration_fingerprint(
                    expected_token, credential.apns_environment or ""
                ),
            )
            self._write()
            return True

    def touch(self, credential_id: str, *, now: float | None = None) -> None:
        """Record use, debounced so a busy client is not a write per request."""
        moment = time.time() if now is None else now
        with self._lock:
            credential = self._by_id.get(credential_id)
            if credential is None:
                return
            last = self._touched_at.get(credential_id, 0.0)
            if moment - last < TOUCH_DEBOUNCE_SECONDS:
                return
            self._touched_at[credential_id] = moment
            self._by_id[credential_id] = replace(credential, last_used_at=_now_iso())
            self._write()

    def revoke(self, credential_id: str) -> bool:
        with self._lock:
            credential = self._by_id.get(credential_id)
            if credential is None or not credential.is_active:
                return False
            self._by_id[credential_id] = replace(
                credential,
                revoked_at=_now_iso(),
                apns_token=None,
                apns_environment=None,
            )
            self._by_verifier.pop(credential.verifier, None)
            self._write()
            return True

    def list_all(self) -> list[Credential]:
        with self._lock:
            return sorted(self._by_id.values(), key=lambda item: item.created_at)

    def has_active_host_credential(self, host_id: str) -> bool:
        """Whether ``host_id`` has the bound credential that requires relay."""
        with self._lock:
            return any(
                credential.scope == "host"
                and credential.host_id == host_id
                and credential.is_active
                for credential in self._by_id.values()
            )

    def _index(self, credential: Credential) -> None:
        self._by_id[credential.id] = credential
        if credential.is_active:
            self._by_verifier[credential.verifier] = credential.id
        else:
            self._by_verifier.pop(credential.verifier, None)

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        self._server_id = str(raw.get("server_id") or uuid4())
        self._fleet_name = str(raw.get("fleet_name") or "drover")
        for item in raw.get("credentials") or []:
            if not isinstance(item, dict):
                continue
            try:
                credential = Credential(
                    id=str(item["id"]),
                    scope=str(item["scope"]),
                    label=str(item["label"]),
                    verifier=str(item["verifier"]),
                    created_at=str(item["created_at"]),
                    host_id=item.get("host_id"),
                    last_used_at=item.get("last_used_at"),
                    revoked_at=item.get("revoked_at"),
                    notification_mode=item.get("notification_mode", "action"),
                    notification_timezone=item.get("notification_timezone", "UTC"),
                    notification_hour=item.get("notification_hour"),
                    apns_token=item.get("apns_token"),
                    apns_environment=item.get("apns_environment"),
                    apns_failure_reason=item.get("apns_failure_reason"),
                    apns_failed_at=item.get("apns_failed_at"),
                    apns_failed_fingerprint=item.get("apns_failed_fingerprint"),
                )
            except KeyError:
                continue
            self._index(credential)
        for item in raw.get("digest_pending") or []:
            if (
                isinstance(item, dict)
                and isinstance(item.get("credential_id"), str)
                and isinstance(item.get("session_id"), str)
            ):
                self._digest_pending[(item["credential_id"], item["session_id"])] = item
        if not self._path.exists():
            # Persist server_id immediately so it survives a restart even if
            # no credential is ever issued.
            self._write()

    def _write(self) -> None:
        """Caller holds the lock. Atomic replace so a crash cannot truncate."""
        payload = {
            "version": STORE_VERSION,
            "server_id": self._server_id,
            "fleet_name": self._fleet_name,
            "credentials": [item.as_json() for item in self._by_id.values()],
            "digest_pending": list(self._digest_pending.values()),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        descriptor = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self._path)
        os.chmod(self._path, 0o600)


_CREDENTIAL_COLUMNS = """
credential_id, scope, label, verifier, created_at, host_id, last_used_at,
revoked_at, apns_token, apns_environment, apns_failure_reason, apns_failed_at,
apns_failed_fingerprint, notification_mode, notification_timezone, notification_hour
"""


def _credential_from_row(row: tuple[object, ...]) -> Credential:
    def timestamp(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    created_at = timestamp(row[4])
    assert created_at is not None
    return Credential(
        id=str(row[0]),
        scope=str(row[1]),
        label=str(row[2]),
        verifier=str(row[3]),
        created_at=created_at,
        host_id=str(row[5]) if row[5] is not None else None,
        last_used_at=timestamp(row[6]),
        revoked_at=timestamp(row[7]),
        apns_token=str(row[8]) if row[8] is not None else None,
        apns_environment=str(row[9]) if row[9] is not None else None,
        apns_failure_reason=str(row[10]) if row[10] is not None else None,
        apns_failed_at=timestamp(row[11]),
        apns_failed_fingerprint=str(row[12]) if row[12] is not None else None,
        notification_mode=str(row[13]),
        notification_timezone=str(row[14] or "UTC"),
        notification_hour=int(row[15]) if row[15] is not None else None,
    )


class PostgresCredentialStore:
    """Credential verifier repository shared safely by API processes.

    Active-verifier lookups always read PostgreSQL. A process may debounce its
    own ``last_used_at`` writes, but it never caches authorization state, so a
    revocation by another API process takes effect on the next request.
    """

    def __init__(self, control_path: Path) -> None:
        self._control_path = Path(control_path)
        self._lock = threading.Lock()
        self._touched_at: dict[str, float] = {}
        self._server_id = self._identity("server_id", str(uuid4()))
        self._fleet_name = self._identity("fleet_name", "drover")

    @property
    def server_id(self) -> str:
        return self._server_id

    @property
    def fleet_name(self) -> str:
        return self._fleet_name

    def issue(
        self, *, scope: str, label: str, host_id: str | None = None
    ) -> tuple[Credential, str]:
        if scope not in SCOPES:
            raise ValueError(f"unknown scope: {scope}")
        if scope == "host" and (not isinstance(host_id, str) or not host_id.strip()):
            raise ValueError("host credentials require host_id")
        token = secrets.token_urlsafe(TOKEN_BYTES)
        credential = Credential(
            id=str(uuid4()),
            scope=scope,
            label=label,
            verifier=verifier_from_token(token),
            created_at=_now_iso(),
            host_id=host_id,
        )
        with self._connection() as con:
            con.execute("BEGIN")
            try:
                if scope == "host":
                    con.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
                        ["host-credential:" + host_id],
                    )
                    con.execute(
                        "UPDATE control_credentials SET revoked_at = ? "
                        "WHERE scope = 'host' AND host_id = ? AND revoked_at IS NULL",
                        [_now_iso(), host_id],
                    )
                con.execute(
                    """INSERT INTO control_credentials
                       (credential_id, scope, label, verifier, created_at, host_id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [
                        credential.id,
                        credential.scope,
                        credential.label,
                        credential.verifier,
                        credential.created_at,
                        credential.host_id,
                    ],
                )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        return credential, token

    def find_active(self, token: str) -> Credential | None:
        verifier = verifier_from_token(token)
        with self._connection() as con:
            row = con.execute(
                f"SELECT {_CREDENTIAL_COLUMNS} FROM control_credentials "
                "WHERE verifier = ? AND revoked_at IS NULL",
                [verifier],
            ).fetchone()
        return _credential_from_row(row) if row is not None else None

    def find_for_revocation(self, token: str) -> Credential | None:
        """Resolve even a revoked verifier, only for idempotent self-revocation."""
        with self._connection() as con:
            row = con.execute(
                f"SELECT {_CREDENTIAL_COLUMNS} FROM control_credentials "
                "WHERE verifier = ?",
                [verifier_from_token(token)],
            ).fetchone()
        return _credential_from_row(row) if row is not None else None

    def get(self, credential_id: str) -> Credential | None:
        with self._connection() as con:
            row = con.execute(
                f"SELECT {_CREDENTIAL_COLUMNS} FROM control_credentials "
                "WHERE credential_id = ?",
                [credential_id],
            ).fetchone()
        return _credential_from_row(row) if row is not None else None

    def set_apns_registration(
        self, credential_id: str, *, token: str, environment: str
    ) -> bool:
        if environment not in APNS_ENVIRONMENTS:
            raise ValueError(f"unknown APNs environment: {environment}")
        with self._connection() as con:
            con.execute("BEGIN")
            try:
                # Registrations are rare, so serialize them all. Locking just
                # the destination endpoint deadlocks when two paired devices
                # concurrently swap endpoints: each owns one device row and
                # then needs the other's row while clearing duplicates.
                con.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
                    ["apns-registration"],
                )
                target = con.execute(
                    """SELECT credential_id FROM control_credentials
                       WHERE credential_id = ? AND revoked_at IS NULL AND scope = 'device'
                       FOR UPDATE""",
                    [credential_id],
                ).fetchone()
                if target is None:
                    con.execute("ROLLBACK")
                    return False
                con.execute(
                    """UPDATE control_credentials
                       SET apns_token = NULL, apns_environment = NULL
                       WHERE credential_id != ? AND revoked_at IS NULL AND scope = 'device'
                         AND apns_token = ? AND apns_environment = ?""",
                    [credential_id, token, environment],
                )
                con.execute(
                    """UPDATE control_credentials
                       SET apns_token = ?, apns_environment = ?,
                           apns_failure_reason = NULL, apns_failed_at = NULL,
                           apns_failed_fingerprint = NULL
                       WHERE credential_id = ?""",
                    [token, environment, credential_id],
                )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        return True

    def set_notification_preferences(
        self,
        credential_id: str,
        mode: str,
        *,
        timezone: str = "UTC",
        hour: int | None = None,
    ) -> bool:
        if mode not in {"all", "action", "digest"}:
            raise ValueError("invalid notification mode")
        if not isinstance(timezone, str) or not timezone:
            raise ValueError("invalid notification timezone")
        if hour is not None and (type(hour) is not int or not 0 <= hour <= 23):
            raise ValueError("invalid notification hour")
        with self._connection() as con:
            row = con.execute(
                "UPDATE control_credentials SET notification_mode = ?, notification_timezone = ?, notification_hour = ? "
                "WHERE credential_id = ? AND revoked_at IS NULL AND scope = 'device' RETURNING credential_id",
                [mode, timezone, hour, credential_id],
            ).fetchone()
        return row is not None

    def set_notification_mode(self, credential_id: str, mode: str) -> bool:
        return self.set_notification_preferences(credential_id, mode)

    def queue_digest(self, credential: Credential, transition, due_at: float) -> None:
        from datetime import datetime, timezone

        due = datetime.fromtimestamp(due_at, timezone.utc)
        local_date = (
            datetime.fromtimestamp(due_at, timezone.utc)
            .astimezone(
                __import__("zoneinfo").ZoneInfo(credential.notification_timezone)
            )
            .date()
        )
        with self._connection() as con:
            con.execute(
                """INSERT INTO notification_digest_items
                   (credential_id, local_date, session_id, kind, awaiting, status, due_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (credential_id, local_date, session_id) DO UPDATE
                   SET kind = EXCLUDED.kind, awaiting = EXCLUDED.awaiting, status = EXCLUDED.status,
                       due_at = LEAST(notification_digest_items.due_at, EXCLUDED.due_at),
                       update_count = notification_digest_items.update_count + 1""",
                [
                    credential.id,
                    local_date,
                    transition.session_id,
                    transition.kind,
                    transition.awaiting,
                    transition.status,
                    due,
                ],
            )

    def pending_digests(self) -> list[dict]:
        with self._connection() as con:
            rows = con.execute(
                "SELECT credential_id, session_id, awaiting, status, EXTRACT(EPOCH FROM due_at), update_count FROM notification_digest_items WHERE sent_at IS NULL"
            ).fetchall()
        return [
            {
                "credential_id": str(r[0]),
                "session_id": str(r[1]),
                "awaiting": r[2],
                "status": r[3],
                "due_at": float(r[4]),
                "update_count": int(r[5]),
            }
            for r in rows
        ]

    def claim_digests(
        self, credential_id: str, session_ids: list[str], now: float, owner: str
    ) -> dict[str, int]:
        """Atomically lease due items so hub workers never send the same batch."""
        if not session_ids:
            return {}
        with self._connection() as con:
            rows = con.execute(
                """UPDATE notification_digest_items
                   SET lease_until = to_timestamp(?) + interval '2 minutes', lease_token = ?
                   WHERE credential_id = ? AND session_id = ANY(?) AND sent_at IS NULL
                     AND due_at <= to_timestamp(?)
                     AND (lease_until IS NULL OR lease_until < to_timestamp(?))
                   RETURNING session_id, update_count""",
                [now, owner, credential_id, session_ids, now, now],
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def cancel_digests(self, credential_id: str, session_ids: list[str]) -> None:
        if not session_ids:
            return
        with self._connection() as con:
            con.execute(
                "DELETE FROM notification_digest_items WHERE credential_id = ? AND session_id = ANY(?)",
                [credential_id, session_ids],
            )

    def complete_digest(
        self, credential_id: str, claimed: dict[str, int], owner: str
    ) -> None:
        # Match both the lease owner and version: a later notification must
        # remain queued instead of being deleted by this earlier APNs response.
        if not claimed:
            return
        with self._connection() as con:
            for session_id, version in claimed.items():
                con.execute(
                    """DELETE FROM notification_digest_items
                       WHERE credential_id = ? AND session_id = ? AND update_count = ?
                         AND lease_token = ? AND sent_at IS NULL""",
                    [credential_id, session_id, version, owner],
                )

    def defer_digest(
        self, credential_id: str, claimed: dict[str, int], due_at: float, owner: str
    ) -> None:
        if not claimed:
            return
        with self._connection() as con:
            for session_id, version in claimed.items():
                con.execute(
                    """UPDATE notification_digest_items
                       SET due_at = to_timestamp(?), lease_until = NULL, lease_token = NULL
                       WHERE credential_id = ? AND session_id = ? AND update_count = ?
                         AND lease_token = ? AND sent_at IS NULL""",
                    [due_at, credential_id, session_id, version, owner],
                )

    def clear_apns_registration(
        self, credential_id: str, *, expected_token: str | None = None
    ) -> bool:
        sql = (
            "UPDATE control_credentials SET apns_token = NULL, apns_environment = NULL "
            "WHERE credential_id = ? AND apns_token IS NOT NULL"
        )
        params: list[object] = [credential_id]
        if expected_token is not None:
            sql += " AND apns_token = ?"
            params.append(expected_token)
        sql += " RETURNING credential_id"
        with self._connection() as con:
            row = con.execute(sql, params).fetchone()
        return row is not None

    def mark_apns_registration_failed(
        self, credential_id: str, *, expected_token: str, reason: str
    ) -> bool:
        with self._connection() as con:
            current = con.execute(
                "SELECT apns_environment FROM control_credentials "
                "WHERE credential_id = ? AND apns_token = ?",
                [credential_id, expected_token],
            ).fetchone()
            if current is None:
                return False
            row = con.execute(
                """UPDATE control_credentials
                   SET apns_token = NULL, apns_environment = NULL,
                       apns_failure_reason = ?, apns_failed_at = ?,
                       apns_failed_fingerprint = ?
                   WHERE credential_id = ? AND apns_token = ?
                   RETURNING credential_id""",
                [
                    reason,
                    _now_iso(),
                    apns_registration_fingerprint(
                        expected_token, str(current[0] or "")
                    ),
                    credential_id,
                    expected_token,
                ],
            ).fetchone()
        return row is not None

    def touch(self, credential_id: str, *, now: float | None = None) -> None:
        moment = time.time() if now is None else now
        with self._lock:
            last = self._touched_at.get(credential_id, 0.0)
            if moment - last < TOUCH_DEBOUNCE_SECONDS:
                return
        with self._connection() as con:
            touched = con.execute(
                """UPDATE control_credentials SET last_used_at = ?
                   WHERE credential_id = ? AND revoked_at IS NULL
                   RETURNING credential_id""",
                [_now_iso(), credential_id],
            ).fetchone()
        if touched is not None:
            with self._lock:
                self._touched_at[credential_id] = moment

    def revoke(self, credential_id: str) -> bool:
        with self._connection() as con:
            row = con.execute(
                """UPDATE control_credentials
                   SET revoked_at = ?, apns_token = NULL, apns_environment = NULL
                   WHERE credential_id = ? AND revoked_at IS NULL
                   RETURNING credential_id""",
                [_now_iso(), credential_id],
            ).fetchone()
        return row is not None

    def list_all(self) -> list[Credential]:
        with self._connection() as con:
            rows = con.execute(
                f"SELECT {_CREDENTIAL_COLUMNS} FROM control_credentials "
                "ORDER BY created_at"
            ).fetchall()
        return [_credential_from_row(row) for row in rows]

    def has_active_host_credential(self, host_id: str) -> bool:
        with self._connection() as con:
            row = con.execute(
                """SELECT 1 FROM control_credentials
                   WHERE scope = 'host' AND host_id = ? AND revoked_at IS NULL
                   LIMIT 1""",
                [host_id],
            ).fetchone()
        return row is not None

    def _identity(self, key: str, fallback: str) -> str:
        with self._connection() as con:
            con.execute(
                """INSERT INTO control_server_identity (identity_key, identity_value)
                   VALUES (?, ?) ON CONFLICT (identity_key) DO NOTHING""",
                [key, fallback],
            )
            row = con.execute(
                "SELECT identity_value FROM control_server_identity WHERE identity_key = ?",
                [key],
            ).fetchone()
        assert row is not None
        return str(row[0])

    def _connection(self):
        from drover.server.db import control_plane_connection

        return control_plane_connection(self._control_path)


def credential_store_for_control_path(
    control_path: Path, fallback_path: Path
) -> CredentialStore | PostgresCredentialStore:
    """Choose the explicit central backend without changing host-local paths."""
    from drover.server.control_store import is_postgres_control_store

    if is_postgres_control_store(control_path):
        return PostgresCredentialStore(control_path)
    return CredentialStore(fallback_path)
