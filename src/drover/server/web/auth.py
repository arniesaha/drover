"""Authentication for the drover-server HTTP/WebSocket surface.

One shared cluster token. Machine clients send ``Authorization: Bearer
<token>``; browsers exchange the token at /auth/login for an HMAC-signed
HttpOnly cookie (stateless: value is ``<expiry-epoch>.<hmac>``). Token
resolution order: DROVER_API_TOKEN env var, then ``[auth] api_token``
in config.toml, then an auto-generated ``~/.drover/api_token`` file (0600).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path

from drover.config import DroverConfig, config_home, resolve_api_token_env
from drover.server.web.credentials import (
    CREDENTIALS_FILENAME,
    Credential,
    CredentialStore,
)

_TOKEN_FILENAME = "api_token"


@dataclass(frozen=True)
class AuthSettings:
    enabled: bool
    api_token: str
    session_ttl_seconds: int = 30 * 86400
    cookie_name: str = "drover_session"
    credentials: CredentialStore | None = None
    legacy_token_enabled: bool = True


DISABLED = AuthSettings(enabled=False, api_token="")


def _load_or_create_token_file(token_home: Path) -> str:
    token_file = token_home / _TOKEN_FILENAME
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    token_home.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(token_file, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return token


def load_auth(cfg: DroverConfig, token_home: Path | None = None) -> AuthSettings:
    if not cfg.auth_enabled:
        return DISABLED
    home = token_home if token_home is not None else config_home()
    token = (
        resolve_api_token_env()
        or cfg.auth_api_token.strip()
        or _load_or_create_token_file(home)
    )
    return AuthSettings(
        enabled=True,
        api_token=token,
        credentials=CredentialStore(home / CREDENTIALS_FILENAME),
        legacy_token_enabled=cfg.auth_legacy_token_enabled,
    )


def _cookie_key(api_token: str) -> bytes:
    return hashlib.sha256(f"drover-session:{api_token}".encode("utf-8")).digest()


def mint_session(auth: AuthSettings, now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + auth.session_ttl_seconds)
    payload = str(expires)
    sig = hmac.new(
        _cookie_key(auth.api_token), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{sig}"


def verify_session(value: str, auth: AuthSettings, now: float | None = None) -> bool:
    if not auth.api_token or "." not in value:
        return False
    payload, _, sig = value.partition(".")
    if not payload.isdigit():
        return False
    expected = hmac.new(
        _cookie_key(auth.api_token), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    return int(payload) > (now if now is not None else time.time())


def _credential_for_token(auth: AuthSettings, candidate: str) -> Credential | None:
    """Resolve and touch an active per-credential bearer token."""
    if auth.credentials is None:
        return None
    credential = auth.credentials.find_active(candidate)
    if credential is not None:
        auth.credentials.touch(credential.id)
    return credential


def bearer_credential(auth: AuthSettings, headers) -> Credential | None:
    """Resolve an active per-credential Authorization bearer token only."""
    authorization = headers.get("Authorization", "") or ""
    if not authorization.startswith("Bearer "):
        return None
    return _credential_for_token(auth, authorization.removeprefix("Bearer ").strip())


def token_matches(auth: AuthSettings, candidate: str) -> bool:
    """Accept the legacy cluster token or any active per-credential token.

    The credential path hashes the candidate before looking it up, so lookup
    cost never varies with the secret and there is no per-credential loop.
    """
    if (
        auth.legacy_token_enabled
        and auth.api_token
        and hmac.compare_digest(candidate, auth.api_token)
    ):
        return True
    return _credential_for_token(auth, candidate) is not None


def request_authorized(auth: AuthSettings, headers) -> bool:
    """Accept either a bearer token or a valid session cookie."""
    if not auth.enabled:
        return True
    authorization = headers.get("Authorization", "") or ""
    if authorization.startswith("Bearer ") and token_matches(
        auth, authorization.removeprefix("Bearer ").strip()
    ):
        return True
    raw_cookie = headers.get("Cookie", "") or ""
    if raw_cookie:
        jar = SimpleCookie()
        try:
            jar.load(raw_cookie)
        except Exception:  # noqa: BLE001 - malformed cookie header
            return False
        morsel = jar.get(auth.cookie_name)
        if morsel is not None and verify_session(morsel.value, auth):
            return True
    return False


def session_cookie_value(auth: AuthSettings) -> str:
    """Full Set-Cookie header value for a freshly minted session."""
    return (
        f"{auth.cookie_name}={mint_session(auth)}; Path=/; HttpOnly; "
        f"SameSite=Strict; Max-Age={auth.session_ttl_seconds}"
    )
