"""Lightweight host-local Claude credential and HTTP helpers."""

from __future__ import annotations

import base64
import getpass
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from drover.server.staging_credentials import is_staging

_ACCOUNT_LABEL = "Claude Code"
_KEYCHAIN_SERVICE = "Claude Code-credentials"
_KEYCHAIN_TIMEOUT_S = 2.0
_MAX_HTTP_BODY_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ClaudeCredential:
    access_token: str = field(repr=False, compare=False)
    account_identity: str
    account_label: str
    subscription_type: str | None


class ClaudeCredentialError(RuntimeError):
    def __init__(self, category: str, *, status: str):
        super().__init__(category)
        self.category = category
        self.status = status


def load_claude_credential(
    *,
    credentials_path: str | Path | None = None,
    account_path: str | Path | None = None,
    keychain_reader: Callable[[], str | None] | None = None,
    now: Callable[[], datetime] | None = None,
) -> ClaudeCredential:
    """Load the first usable Keychain/file OAuth credential.

    The source order and failure precedence intentionally match the usage
    probe that originally owned this logic. Secret material exists only in
    the returned redacted value and local parsing variables.
    """

    if is_staging():
        # This lane is API-key-only. Never discover subscription credentials,
        # including injected readers and a personal macOS Keychain item.
        raise ClaudeCredentialError("not_authenticated", status="usage_unavailable")
    resolved_credentials = (
        Path(credentials_path)
        if credentials_path is not None
        else Path.home() / ".claude" / ".credentials.json"
    )
    resolved_account = (
        Path(account_path) if account_path is not None else Path.home() / ".claude.json"
    )
    read_keychain = keychain_reader or _read_keychain
    clock = now or (lambda: datetime.now(timezone.utc))
    account_identity, account_label = _load_account_metadata(resolved_account)
    current_time = clock()

    expired_category: str | None = None
    saw_malformed = False
    read_failure: ClaudeCredentialError | None = None
    for load in (read_keychain, lambda: _read_file(resolved_credentials)):
        try:
            raw = load()
        except ClaudeCredentialError as exc:
            read_failure = exc
            continue
        except Exception:
            # A source that cannot be read is treated as absent. This includes
            # a Keychain prompt that exceeded its process bound.
            continue
        if raw is None:
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            saw_malformed = True
            continue
        oauth = parsed.get("claudeAiOauth") if isinstance(parsed, Mapping) else None
        if not isinstance(oauth, Mapping):
            saw_malformed = True
            continue
        token = oauth.get("accessToken")
        if not isinstance(token, str) or not token:
            continue
        expires_at = oauth.get("expiresAt")
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
            expiry = datetime.fromtimestamp(expires_at / 1000, tz=timezone.utc)
            if expiry <= current_time:
                category = _expired_access_token_category(oauth, current_time)
                # A login that cannot be refreshed is more specific than one
                # that can. Keep looking because another source may still be
                # usable (notably macOS Keychain), but preserve that result if
                # neither source is current.
                if category == "login_expired" or expired_category is None:
                    expired_category = category
                continue
        plan = oauth.get("subscriptionType")
        return ClaudeCredential(
            access_token=token,
            account_identity=account_identity,
            account_label=account_label,
            subscription_type=plan if isinstance(plan, str) and plan else None,
        )

    if expired_category is not None:
        raise ClaudeCredentialError(expired_category, status="usage_unavailable")
    if read_failure is not None:
        raise read_failure
    if saw_malformed:
        raise ClaudeCredentialError("protocol_error", status="error")
    # No credential source is normal on a host where Claude Code is enabled
    # for launching but has never been used. It is not a failed provider
    # connection and must not make fleet quota data look stale.
    raise ClaudeCredentialError("not_configured", status="not_configured")


def _expired_access_token_category(oauth: Mapping[str, object], now: datetime) -> str:
    """Classify an expired access token without attempting OAuth refresh.

    Claude Code owns this credential file. Drover deliberately only reads it:
    refreshing or writing it can race Claude Code and invalidate its login.
    Some versions record the refresh-token expiry while others only retain the
    opaque refresh token. An unexpired or unbounded token can still be renewed
    by running Claude Code once, so both lead to the refresh-needed action.
    """

    refresh_token = oauth.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        return "login_expired"
    expiry = _refresh_token_expiry(oauth, refresh_token)
    return (
        "login_expired"
        if expiry is not None and expiry <= now
        else "login_refresh_needed"
    )


def _refresh_token_expiry(
    oauth: Mapping[str, object], refresh_token: str
) -> datetime | None:
    """Return the refresh-token expiry recorded by supported Claude clients."""

    for field in (
        "refreshTokenExpiresAt",
        "refreshTokenExpiry",
        "refreshExpiresAt",
    ):
        value = oauth.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # Claude Code writes epoch milliseconds. Accept seconds too for
            # older/exported credentials without treating a malformed value as
            # an expired login.
            try:
                seconds = value / 1000 if value > 10_000_000_000 else value
                return datetime.fromtimestamp(seconds, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    # Some Claude Code versions retain an opaque token only; when it is a JWT,
    # its standard expiry claim gives us the same answer without contacting the
    # provider or attempting a refresh.
    try:
        payload = refresh_token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        expires_at = claims.get("exp") if isinstance(claims, Mapping) else None
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
            return datetime.fromtimestamp(expires_at, tz=timezone.utc)
    except (IndexError, ValueError, UnicodeDecodeError, OverflowError, OSError):
        pass
    return None


def _load_account_metadata(account_path: Path) -> tuple[str, str]:
    try:
        raw = json.loads(account_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _ACCOUNT_LABEL, _ACCOUNT_LABEL
    account = raw.get("oauthAccount") if isinstance(raw, Mapping) else None
    if not isinstance(account, Mapping):
        return _ACCOUNT_LABEL, _ACCOUNT_LABEL

    values: dict[str, str] = {}
    for key in ("accountUuid", "emailAddress", "organizationName"):
        value = account.get(key)
        if isinstance(value, str) and value.strip():
            values[key] = value.strip()
    identity = next(
        (
            values[key]
            for key in ("accountUuid", "emailAddress", "organizationName")
            if key in values
        ),
        _ACCOUNT_LABEL,
    )
    label = next(
        (
            values[key]
            for key in ("emailAddress", "organizationName", "accountUuid")
            if key in values
        ),
        _ACCOUNT_LABEL,
    )
    return identity, label


def _read_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ClaudeCredentialError("protocol_error", status="error") from exc


def _read_keychain() -> str | None:
    """Read Claude Code's macOS Keychain item without exposing subprocess IO."""

    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                _KEYCHAIN_SERVICE,
                "-a",
                getpass.getuser(),
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=_KEYCHAIN_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # stderr may echo the item, so failures are never logged or returned.
        return None
    if result.returncode != 0:
        return None
    blob = result.stdout.strip()
    return blob or None


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse redirects so provider credentials cannot leak to a new target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirectHandler)


def _http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    """Make one bounded GET without following provider-controlled redirects."""

    request = Request(url, headers=headers, method="GET")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return int(response.status), response.read(_MAX_HTTP_BODY_BYTES + 1)
    except HTTPError as exc:
        return int(exc.code), exc.read(_MAX_HTTP_BODY_BYTES + 1)
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError(str(exc.reason)) from None
        raise OSError(str(exc.reason)) from None
