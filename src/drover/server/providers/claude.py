"""Bounded, host-local Claude Code account and rate-limit probe.

Reads this host's own OAuth credential and asks the provider what it has
spent. The endpoint is the one the CLI's own /usage view calls; it is not
documented, so every failure here degrades to a snapshot the cards already
render rather than surfacing as an error.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from drover.server.providers.claude_credentials import (
    ClaudeCredentialError,
    _http_get,
    _load_account_metadata,
    load_claude_credential,
)
from drover.server.providers.types import ProviderAccountSnapshot, ProviderUsageWindow

log = logging.getLogger(__name__)

_USAGE_PATH = "/api/oauth/usage"
_DEFAULT_BASE_URL = "https://api.anthropic.com"
_SOURCE = "claude-oauth-usage"
# Only the windows whose duration is actually known. Anything else passes
# through with window_minutes=None; inferring a duration from the key would
# be inventing data.
_WINDOW_MINUTES = {
    "five_hour": 300,
    "seven_day": 10080,
    "seven_day_opus": 10080,
    "seven_day_sonnet": 10080,
}


class _ProbeFailure(RuntimeError):
    def __init__(self, category: str, *, status: str):
        super().__init__(category)
        self.category = category
        self.status = status


class ClaudeUsageProbe:
    def __init__(
        self,
        credentials_path: str | Path | None = None,
        opener: Callable[[str, dict[str, str], float], tuple[int, bytes]] | None = None,
        timeout_s: float = 5.0,
        base_url: str | None = None,
        keychain_reader: Callable[[], str | None] | None = None,
        account_path: str | Path | None = None,
    ):
        self.account_path = (
            Path(account_path)
            if account_path is not None
            else Path.home() / ".claude.json"
        )
        self.credentials_path = (
            Path(credentials_path)
            if credentials_path is not None
            else Path.home() / ".claude" / ".credentials.json"
        )
        self.opener = opener or _http_get
        self.timeout_s = timeout_s
        self.base_url = (
            base_url or os.environ.get("ANTHROPIC_BASE_URL") or _DEFAULT_BASE_URL
        ).rstrip("/")
        self.keychain_reader = keychain_reader

    def read(self, *, host_id: str = "local") -> ProviderAccountSnapshot:
        observed_at = datetime.now(timezone.utc)
        _, account_label = _load_account_metadata(self.account_path)
        try:
            credential = load_claude_credential(
                credentials_path=self.credentials_path,
                account_path=self.account_path,
                keychain_reader=self.keychain_reader,
            )
            payload = self._fetch(credential.access_token)
            windows = _windows(payload)
            plan_label = credential.subscription_type
        except ClaudeCredentialError as exc:
            return _snapshot(
                host_id=host_id,
                account_label=account_label,
                status=exc.status,
                observed_at=observed_at,
                windows=(),
                plan_label=None,
                error_category=exc.category,
            )
        except _ProbeFailure as exc:
            return _snapshot(
                host_id=host_id,
                account_label=account_label,
                status=exc.status,
                observed_at=observed_at,
                windows=(),
                plan_label=None,
                error_category=exc.category,
            )
        except (TypeError, ValueError, OverflowError, OSError):
            return _snapshot(
                host_id=host_id,
                account_label=account_label,
                status="error",
                observed_at=observed_at,
                windows=(),
                plan_label=None,
                error_category="protocol_error",
            )
        if not windows:
            return _snapshot(
                host_id=host_id,
                account_label=account_label,
                status="usage_unavailable",
                observed_at=observed_at,
                windows=(),
                plan_label=plan_label,
                error_category="no_usage_reported",
            )
        return _snapshot(
            host_id=host_id,
            account_label=account_label,
            status="ok",
            observed_at=observed_at,
            windows=windows,
            plan_label=plan_label,
            error_category=None,
        )

    def _fetch(self, token: str) -> Mapping[str, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        try:
            status, body = self.opener(
                f"{self.base_url}{_USAGE_PATH}", headers, self.timeout_s
            )
        except TimeoutError:
            raise _ProbeFailure("timeout", status="error") from None
        except http.client.HTTPException:
            # Covers http.client.IncompleteRead (truncated body) and
            # BadStatusLine (garbage status line from a proxy), among
            # others -- these do not subclass OSError and would otherwise
            # escape read() entirely and take the daemon handler down with
            # them, since do_GET has no try wrapper around this call.
            raise _ProbeFailure("unavailable", status="error") from None
        except OSError:
            raise _ProbeFailure("unavailable", status="error") from None

        if status in (401, 403):
            raise _ProbeFailure("not_authenticated", status="usage_unavailable")
        if status < 200 or status >= 300:
            # Also covers 3xx: the opener below refuses to follow redirects,
            # so any redirect response reaches here as a raw status instead
            # of being transparently chased.
            raise _ProbeFailure("unavailable", status="error")
        try:
            payload = json.loads(body)
        except ValueError:
            raise _ProbeFailure("protocol_error", status="error") from None
        if not isinstance(payload, Mapping):
            raise _ProbeFailure("protocol_error", status="error")
        return payload


def _windows(payload: Mapping[str, Any]) -> tuple[ProviderUsageWindow, ...]:
    windows: list[ProviderUsageWindow] = []
    for key, value in payload.items():
        if not isinstance(value, Mapping):
            continue
        utilization = value.get("utilization")
        if not isinstance(utilization, (int, float)) or isinstance(utilization, bool):
            continue
        windows.append(
            ProviderUsageWindow(
                kind=str(key),
                used_percent=float(utilization),
                window_minutes=_WINDOW_MINUTES.get(str(key)),
                resets_at=_timestamp(value.get("resets_at")),
            )
        )
    return tuple(windows)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _snapshot(
    *,
    host_id: str,
    account_label: str,
    status: str,
    observed_at: datetime,
    windows: tuple[ProviderUsageWindow, ...],
    plan_label: str | None,
    error_category: str | None,
) -> ProviderAccountSnapshot:
    fingerprint = {
        "provider": "anthropic",
        "account_label": account_label,
        "plan_label": plan_label,
        "host_id": host_id,
        "status": status,
        "windows": [
            {
                "kind": window.kind,
                "used_percent": window.used_percent,
                "window_minutes": window.window_minutes,
                "resets_at": window.resets_at.isoformat() if window.resets_at else None,
            }
            for window in windows
        ],
        "source": _SOURCE,
        "error_category": error_category,
    }
    dedup_key = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ProviderAccountSnapshot(
        snapshot_id=str(uuid4()),
        dedup_key=dedup_key,
        provider="anthropic",
        account_label=account_label,
        plan_label=plan_label,
        host_id=host_id,
        status=status,  # type: ignore[arg-type]
        observed_at=observed_at,
        windows=windows,
        source=_SOURCE,
        error_category=error_category,
    )
