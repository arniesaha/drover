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
SCOPES = ("device", "host")
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
        }

    def as_public_json(self) -> dict:
        """Everything except the verifier, safe to serve over the API."""
        data = self.as_json()
        del data["verifier"]
        return data


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
            self._by_id[credential_id] = replace(credential, revoked_at=_now_iso())
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
