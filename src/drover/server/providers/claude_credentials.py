"""Host-local Claude Code credential loading with redacted values."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import getpass
import json
from pathlib import Path
import subprocess
import sys
from typing import Callable, Mapping

_ACCOUNT_LABEL = "Claude Code"
_KEYCHAIN_SERVICE = "Claude Code-credentials"
_KEYCHAIN_TIMEOUT_S = 2.0


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

    saw_expired = False
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
            if expiry <= clock():
                saw_expired = True
                continue
        plan = oauth.get("subscriptionType")
        return ClaudeCredential(
            access_token=token,
            account_identity=account_identity,
            account_label=account_label,
            subscription_type=plan if isinstance(plan, str) and plan else None,
        )

    if saw_expired:
        raise ClaudeCredentialError("token_expired", status="usage_unavailable")
    if read_failure is not None:
        raise read_failure
    if saw_malformed:
        raise ClaudeCredentialError("protocol_error", status="error")
    raise ClaudeCredentialError("not_authenticated", status="usage_unavailable")


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
