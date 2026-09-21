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
SCOPES = ("device", "host", "preflight")
APNS_ENVIRONMENTS = ("sandbox", "production")
_VERIFIER_DOMAIN = b"drover-cred-v1\0"


def verifier_from_token(token: str) -> str:
    digest = hashlib.sha256(_VERIFIER_DOMAIN + token.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    apns_token: str | None = None
    apns_environment: str | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

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
            "apns_token": self.apns_token,
            "apns_environment": self.apns_environment,
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
            self._index(credential)
            self._write()
        return credential, token

    def find_active(self, token: str) -> Credential | None:
        """Look up by verifier, so lookup cost never depends on the secret."""
        verifier = verifier_from_token(token)
        with self._lock:
            credential_id = self._by_verifier.get(verifier)
            return self._by_id.get(credential_id) if credential_id else None

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
            self._by_id[credential_id] = replace(
                credential,
                apns_token=token,
                apns_environment=environment,
            )
            self._write()
            return True

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
                    apns_token=item.get("apns_token"),
                    apns_environment=item.get("apns_environment"),
                )
            except KeyError:
                continue
            self._index(credential)
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
revoked_at, apns_token, apns_environment
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
            row = con.execute(
                """UPDATE control_credentials
                   SET apns_token = ?, apns_environment = ?
                   WHERE credential_id = ? AND revoked_at IS NULL AND scope = 'device'
                   RETURNING credential_id""",
                [token, environment, credential_id],
            ).fetchone()
        return row is not None

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
