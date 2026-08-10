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
import http.client
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
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
# The central server's fetch of /providers/usage times out at 10s (see
# metrics.py, near line 1017), and read() spends this budget on the
# Keychain sequentially before spending up to timeout_s (default 5s) on the
# HTTP call. At 5.0 the two together could exhaust the whole 10s host-fetch
# budget, losing every provider card for that host (not just this one) to a
# single slow Keychain prompt. 2.0 leaves headroom for the HTTP leg and the
# rest of that request.
_KEYCHAIN_TIMEOUT_S = 2.0


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
        self.keychain_reader = keychain_reader or _read_keychain

    def read(self, *, host_id: str = "local") -> ProviderAccountSnapshot:
        observed_at = datetime.now(timezone.utc)
        account_label = self._account_label()
        try:
            token, plan_label = self._credentials()
            payload = self._fetch(token)
            windows = _windows(payload)
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

    def _account_label(self) -> str:
        """Name the subscription this host is signed into.

        Anthropic's usage endpoint says nothing about *which* account it
        answered for, and the fleet runs more than one: a personal
        subscription on some hosts and a work subscription on others. Reported
        under one generic name they merge into a single card, which attributes
        one account's consumption to the other's machines. The signed-in
        identity is in the CLI's own config, so it is read per host and used
        the way the Codex probe uses its account email.

        Falls back rather than failing: an unreadable or unfamiliar config
        gives the generic name, which is no worse than before this existed.
        """
        try:
            raw = json.loads(self.account_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _ACCOUNT_LABEL
        account = raw.get("oauthAccount") if isinstance(raw, Mapping) else None
        if not isinstance(account, Mapping):
            return _ACCOUNT_LABEL
        for key in ("emailAddress", "organizationName", "accountUuid"):
            value = account.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return _ACCOUNT_LABEL

    def _credentials(self) -> tuple[str, str | None]:
        saw_expired = False
        saw_malformed = False
        read_failure: _ProbeFailure | None = None
        for load in (self._keychain_blob, self._file_blob):
            try:
                raw = load()
            except _ProbeFailure as exc:
                # A source that is present but broken (e.g. an unreadable
                # credentials file) is a real error, not an absent source --
                # but it is deferred rather than raised immediately, so an
                # expired token already seen from an earlier source (the
                # Keychain runs first) wins: token_expired is more
                # actionable than a read failure on a later, unrelated
                # source.
                read_failure = exc
                continue
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
        if read_failure is not None:
            raise read_failure
        if saw_malformed:
            raise _ProbeFailure("protocol_error", status="error")
        raise _ProbeFailure("not_authenticated", status="usage_unavailable")

    def _file_blob(self) -> str | None:
        try:
            return self.credentials_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            # Present but unreadable (permission denied, a directory, a
            # decode failure, ...) is a real error, not an absent source.
            raise _ProbeFailure("protocol_error", status="error") from exc

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


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse to follow redirects on this request.

    urlopen's default opener follows 3xx responses AND copies the original
    request's headers -- including this bearer token -- onto the new
    request it builds, with no restriction on the target host or scheme.
    /api/oauth/usage is undocumented; a redirect from it is not a case we
    can reason about, so the only safe move is to refuse to chase it and
    let the caller treat the raw 3xx as a failure.

    This deliberately does NOT inspect or restrict base_url's own scheme or
    host. ANTHROPIC_BASE_URL is honoured as-is elsewhere in this module
    because the Claude CLI honours the same variable, and pointing it at a
    local http:// proxy is a legitimate operator choice. Do not "harden"
    this handler into a scheme check on the *original* request -- that
    would break proxy support for a leak this handler already closes by
    refusing to hop anywhere in response to a redirect.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirectHandler)


def _http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = Request(url, headers=headers, method="GET")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
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
