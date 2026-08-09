"""Bounded, host-local Claude Code account and rate-limit probe.

Reads this host's own OAuth credential and asks the provider what it has
spent. The endpoint is the one the CLI's own /usage view calls; it is not
documented, so every failure here degrades to a snapshot the cards already
render rather than surfacing as an error.
"""

from __future__ import annotations

from datetime import datetime, timezone
import getpass
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from drover.server.providers.types import ProviderAccountSnapshot, ProviderUsageWindow

log = logging.getLogger(__name__)

_USAGE_PATH = "/api/oauth/usage"
_DEFAULT_BASE_URL = "https://api.anthropic.com"
_ACCOUNT_LABEL = "Claude Code"
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
_KEYCHAIN_SERVICE = "Claude Code-credentials"
_KEYCHAIN_TIMEOUT_S = 5.0


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
    ):
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
        self.keychain_reader = keychain_reader or _read_keychain

    def read(self, *, host_id: str = "local") -> ProviderAccountSnapshot:
        observed_at = datetime.now(timezone.utc)
        try:
            token, plan_label = self._credentials()
            payload = self._fetch(token)
            windows = _windows(payload)
        except _ProbeFailure as exc:
            return _snapshot(
                host_id=host_id,
                status=exc.status,
                observed_at=observed_at,
                windows=(),
                plan_label=None,
                error_category=exc.category,
            )
        except (TypeError, ValueError, OverflowError, OSError):
            return _snapshot(
                host_id=host_id,
                status="error",
                observed_at=observed_at,
                windows=(),
                plan_label=None,
                error_category="protocol_error",
            )
        if not windows:
            return _snapshot(
                host_id=host_id,
                status="usage_unavailable",
                observed_at=observed_at,
                windows=(),
                plan_label=plan_label,
                error_category="no_usage_reported",
            )
        return _snapshot(
            host_id=host_id,
            status="ok",
            observed_at=observed_at,
            windows=windows,
            plan_label=plan_label,
            error_category=None,
        )

    def _credentials(self) -> tuple[str, str | None]:
        saw_expired = False
        saw_malformed = False
        for load in (self._keychain_blob, self._file_blob):
            try:
                raw = load()
            except Exception:
                # A source that cannot be read is a source we do not have. This
                # includes a Keychain prompt we declined to wait for.
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
            if isinstance(expires_at, (int, float)) and not isinstance(
                expires_at, bool
            ):
                expiry = datetime.fromtimestamp(expires_at / 1000, tz=timezone.utc)
                if expiry <= datetime.now(timezone.utc):
                    saw_expired = True
                    continue
            plan = oauth.get("subscriptionType")
            return token, plan if isinstance(plan, str) and plan else None

        if saw_expired:
            raise _ProbeFailure("token_expired", status="usage_unavailable")
        if saw_malformed:
            raise _ProbeFailure("protocol_error", status="error")
        raise _ProbeFailure("not_authenticated", status="usage_unavailable")

    def _file_blob(self) -> str | None:
        try:
            return self.credentials_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def _keychain_blob(self) -> str | None:
        return self.keychain_reader()

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
        except OSError:
            raise _ProbeFailure("unavailable", status="error") from None

        if status in (401, 403):
            raise _ProbeFailure("not_authenticated", status="usage_unavailable")
        if status < 200 or status >= 300:
            raise _ProbeFailure("unavailable", status="error")
        try:
            payload = json.loads(body)
        except ValueError:
            raise _ProbeFailure("protocol_error", status="error") from None
        if not isinstance(payload, Mapping):
            raise _ProbeFailure("protocol_error", status="error")
        return payload


def _http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except HTTPError as exc:
        return int(exc.code), exc.read()
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError(str(exc.reason)) from None
        raise OSError(str(exc.reason)) from None


def _read_keychain() -> str | None:
    """The live credential on macOS. Returns None on any failure.

    harnessd is a different binary from claude, so macOS may put up a prompt
    before granting access to the item. The timeout is what stops a refresh
    cycle hanging behind that dialog; a host whose grant has not been given
    simply looks like a host that was never signed in.
    """
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
        # Includes TimeoutExpired. Never log: stderr can echo the item.
        return None
    if result.returncode != 0:
        return None
    blob = result.stdout.strip()
    return blob or None


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
    status: str,
    observed_at: datetime,
    windows: tuple[ProviderUsageWindow, ...],
    plan_label: str | None,
    error_category: str | None,
) -> ProviderAccountSnapshot:
    fingerprint = {
        "provider": "anthropic",
        "account_label": _ACCOUNT_LABEL,
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
        account_label=_ACCOUNT_LABEL,
        plan_label=plan_label,
        host_id=host_id,
        status=status,  # type: ignore[arg-type]
        observed_at=observed_at,
        windows=windows,
        source=_SOURCE,
        error_category=error_category,
    )
