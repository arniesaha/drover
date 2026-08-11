"""Bounded, host-local Antigravity (agy) account and capacity probe.

Reads this host's signed-in account and quota, returning a
ProviderAccountSnapshot.

``~/.gemini`` is agy's own state directory, not a Gemini CLI leftover: it
holds ``antigravity-cli/`` beside ``oauth_creds.json`` and
``google_accounts.json``, and a signed-in agy writes it directly (verified
on the Mac mini 2026-08-09, with no ``~/.agy``, ``~/.antigravity`` or
``~/.codeium`` present at all).

Capacity comes from ``POST /v1internal:retrieveUserQuotaSummary`` on
``cloudcode-pa.googleapis.com``. Three things about that call are load-bearing
and none of them are guessable, so they are recorded here:

1. **The method name.** ``FetchQuotaStatus`` is also in the agy binary, as
   ``google.cloud.businessaicode.{v1beta,v1main}``, and it genuinely 404s on
   every reachable host -- chasing it is what made this look impossible. The
   served method is ``RetrieveUserQuotaSummary`` on
   ``google.internal.cloud.code.v1internal``, the same service as the
   ``loadCodeAssist`` that was already known to route. It is gRPC-defined but
   exposed over HTTP/JSON transcoding, so a plain POST works.

   Its sibling ``retrieveUserQuota`` (no ``Summary``) also answers 200 and is
   a trap: it returns Gemini Code Assist buckets for 2.5-era models, always
   ``remainingFraction: 1``, with a reset time that slides 24h on every call.
   It looks like working data and tracks nothing agy does.

2. **The credential.** ``~/.gemini/oauth_creds.json`` is NOT refreshed by agy
   and runs hours-to-days expired -- reading it is how this probe would look
   broken on a working host. The live token lives in the macOS Keychain under
   service ``gemini`` / account ``antigravity``, and on Linux (the NAS, which
   has no Keychain) in ``antigravity-cli/antigravity-oauth-token``. Both hold
   the same JSON. agy only refreshes while it runs, so an idle host's token is
   stale and this probe refreshes it itself -- in memory, never writing back
   into a live CLI's credential store.

3. **The User-Agent.** Without an ``antigravity`` substring in it the endpoint
   answers **403 PERMISSION_DENIED even with a valid token**. That 403 is what
   read as "not permitted, by design". ``drover/... antigravity`` passes, so
   Drover identifies itself honestly rather than impersonating agy.

``read()`` must never raise. ``harnessd``'s ``do_GET`` has no try wrapper,
so an escaping exception means no HTTP response at all, which would take
every other provider's card down with it (drover#65).
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from drover.server.providers.types import ProviderAccountSnapshot, ProviderUsageWindow

log = logging.getLogger(__name__)

_ACCOUNT_LABEL = "Antigravity"
_SOURCE = "agy-usage"

_DEFAULT_BASE_URL = "https://cloudcode-pa.googleapis.com"
_QUOTA_PATH = "/v1internal:retrieveUserQuotaSummary"
_TOKEN_URL = "https://oauth2.googleapis.com/token"

# See the module docstring: an "antigravity" substring is what the endpoint
# gates on. Drover names itself first so the traffic is attributable.
_USER_AGENT = "drover/1.0 (antigravity-cli compatible)"

_KEYCHAIN_SERVICE = "gemini"
_KEYCHAIN_ACCOUNT = "antigravity"
_KEYRING_PREFIX = "go-keyring-base64:"
_TOKEN_FILE = ("antigravity-cli", "antigravity-oauth-token")

# Refreshing agy's token needs agy's own installed-app OAuth client, which is
# read out of the installed binary rather than committed here. Hardcoding it
# would put a Google client secret in a repo headed for public release (and
# GitHub's push protection rightly rejects it). Reading it from the binary
# also means a client rotation in a future agy release is picked up for free.
# These are installed-app credentials, which RFC 8252 treats as public -- but
# "extractable by anyone with the binary" is still not "fine to publish".
_CLIENT_ID_RE = re.compile(rb"\d{10,}-[a-z0-9]{16,}\.apps\.googleusercontent\.com")
_CLIENT_SECRET_RE = re.compile(rb"GOCSPX-[A-Za-z0-9_-]{28}")
_BINARY_SCAN_CHUNK = 8 << 20

# Refresh a little before the wire expiry so a token that dies mid-flight
# does not turn into a blank card.
_EXPIRY_SKEW = timedelta(seconds=60)

# The API's own bucket ids, mapped onto the vocabulary the cockpit already
# renders for Claude ("Five hour", "Seven day"). Anything unrecognised passes
# through as its bucket id rather than being dropped -- a new group should
# show up as an odd label, not vanish.
_BUCKET_KINDS = {
    "gemini-5h": "five_hour",
    "gemini-weekly": "seven_day",
    "3p-5h": "five_hour_claude_gpt",
    "3p-weekly": "seven_day_claude_gpt",
}
# Only durations that are actually known. Inventing one from an unknown
# window string would be inventing data.
_WINDOW_MINUTES = {"5h": 300, "weekly": 10080, "daily": 1440}


class _ProbeFailure(RuntimeError):
    def __init__(self, category: str, *, status: str):
        super().__init__(category)
        self.category = category
        self.status = status


class AgyUsageProbe:
    """Report the agy account this host is signed into, and its capacity."""

    def __init__(
        self,
        accounts_path: str | Path | None = None,
        state_dir: str | Path | None = None,
        opener: (
            Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]] | None
        ) = None,
        keychain_reader: Callable[[], str | None] | None = None,
        timeout_s: float = 5.0,
        base_url: str | None = None,
        now: Callable[[], datetime] | None = None,
        oauth_clients: Callable[[], tuple[tuple[str, str], ...]] | None = None,
    ):
        base = Path(state_dir) if state_dir is not None else Path.home() / ".gemini"
        self.state_dir = base
        self.accounts_path = (
            Path(accounts_path)
            if accounts_path is not None
            else base / "google_accounts.json"
        )
        self.opener = opener or _http_post
        self.keychain_reader = keychain_reader or _read_keychain
        self.timeout_s = timeout_s
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.oauth_clients = oauth_clients or _agy_oauth_clients

    def read(self, *, host_id: str = "local") -> ProviderAccountSnapshot:
        observed_at = self.now()
        account_label = self._account_label()
        try:
            windows = self._fetch_windows()
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
        except Exception:  # noqa: BLE001 -- read() must never raise
            log.debug("agy capacity probe failed", exc_info=True)
            return _snapshot(
                host_id=host_id,
                account_label=account_label,
                status="usage_unavailable",
                observed_at=observed_at,
                windows=(),
                plan_label=None,
                error_category="probe_failed",
            )
        return _snapshot(
            host_id=host_id,
            account_label=account_label,
            status="ok" if windows else "usage_unavailable",
            observed_at=observed_at,
            windows=windows,
            plan_label=None,
            error_category=None if windows else "quota_api_unreachable",
        )

    def _fetch_windows(self) -> tuple[ProviderUsageWindow, ...]:
        """Capacity windows for this account, from agy's own quota endpoint."""
        return _windows(self._fetch(self._access_token()))

    def _access_token(self) -> str:
        """The live token, refreshed in memory if agy left a stale one behind."""
        blob = self._credential_blob()
        try:
            token = json.loads(blob).get("token") or {}
            access = token["access_token"]
        except (ValueError, AttributeError, KeyError, TypeError) as exc:
            raise _ProbeFailure("protocol_error", status="error") from exc
        if not _is_expired(token.get("expiry"), self.now()):
            return str(access)
        refresh = token.get("refresh_token")
        if not refresh:
            raise _ProbeFailure("token_expired", status="usage_unavailable")
        return self._refresh(str(refresh))

    def _credential_blob(self) -> str:
        """agy's stored credential, Keychain first then the Linux file.

        Deliberately does NOT read ``~/.gemini/oauth_creds.json``: agy never
        refreshes it, so it is stale on a perfectly healthy host.
        """
        for load in (self._keychain_blob, self._file_blob):
            try:
                raw = load()
            except _ProbeFailure:
                raise
            except Exception:
                # A source that cannot be read is a source we do not have.
                # This includes a Keychain prompt we declined to wait for.
                continue
            if raw:
                return _unwrap_keyring(raw)
        raise _ProbeFailure("not_authenticated", status="usage_unavailable")

    def _keychain_blob(self) -> str | None:
        return self.keychain_reader()

    def _file_blob(self) -> str | None:
        try:
            return self.state_dir.joinpath(*_TOKEN_FILE).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            # Present but unreadable is a real error, not an absent source.
            raise _ProbeFailure("protocol_error", status="error") from exc

    def _refresh(self, refresh_token: str) -> str:
        clients = self.oauth_clients()
        if not clients:
            # Nothing to refresh with. The stale token is still reported
            # honestly rather than pretending the account is unreadable.
            raise _ProbeFailure("token_expired", status="usage_unavailable")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": _USER_AGENT,
        }
        rejected = False
        for client_id, client_secret in clients:
            body = urllib.parse.urlencode(
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                }
            ).encode()
            status, payload = self._request(_TOKEN_URL, headers, body)
            if status in (400, 401):
                # Wrong pairing out of the binary's candidates, or a genuinely
                # dead grant. Only the last one decides.
                rejected = True
                continue
            if status < 200 or status >= 300:
                raise _ProbeFailure("unavailable", status="error")
            try:
                return str(json.loads(payload)["access_token"])
            except (ValueError, KeyError, TypeError) as exc:
                raise _ProbeFailure("protocol_error", status="error") from exc
        if rejected:
            raise _ProbeFailure("not_authenticated", status="usage_unavailable")
        raise _ProbeFailure("unavailable", status="error")

    def _fetch(self, token: str) -> Mapping[str, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Load-bearing; see the module docstring.
            "User-Agent": _USER_AGENT,
        }
        status, payload = self._request(f"{self.base_url}{_QUOTA_PATH}", headers, b"{}")
        if status in (401, 403):
            raise _ProbeFailure("not_authenticated", status="usage_unavailable")
        if status < 200 or status >= 300:
            raise _ProbeFailure("unavailable", status="error")
        try:
            body = json.loads(payload)
        except ValueError as exc:
            raise _ProbeFailure("protocol_error", status="error") from exc
        if not isinstance(body, Mapping):
            raise _ProbeFailure("protocol_error", status="error")
        return body

    def _request(
        self, url: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, bytes]:
        try:
            return self.opener(url, headers, body, self.timeout_s)
        except TimeoutError:
            raise _ProbeFailure("timeout", status="error") from None
        except http.client.HTTPException:
            # Does not subclass OSError, so it would otherwise escape read()
            # and take harnessd's handler down with it.
            raise _ProbeFailure("unavailable", status="error") from None
        except OSError:
            raise _ProbeFailure("unavailable", status="error") from None

    def _account_label(self) -> str:
        """Name the account this host is signed into.

        A generic label merges distinct accounts into one card and
        misattributes one account's consumption to the other's machines
        (drover#69), so the signed-in address is read per host. Falls back to
        the generic name rather than failing -- no worse than having no
        label at all.
        """
        try:
            raw = json.loads(self.accounts_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _ACCOUNT_LABEL
        if not isinstance(raw, Mapping):
            return _ACCOUNT_LABEL
        active = raw.get("active")
        if isinstance(active, str) and active.strip():
            return active.strip()
        old = raw.get("old")
        if isinstance(old, list) and old and isinstance(old[0], str) and old[0].strip():
            return old[0].strip()
        return _ACCOUNT_LABEL


def _unwrap_keyring(raw: str) -> str:
    """Strip go-keyring's base64 wrapper. The Linux file is already plain."""
    raw = raw.strip()
    if not raw.startswith(_KEYRING_PREFIX):
        return raw
    try:
        return base64.b64decode(raw[len(_KEYRING_PREFIX) :]).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 -- malformed store, not absent
        raise _ProbeFailure("protocol_error", status="error") from exc


def _is_expired(expiry: Any, now: datetime) -> bool:
    """True when the token is past (or nearly past) its expiry.

    An unparseable or missing expiry counts as expired: refreshing a good
    token costs one request, while using a dead one blanks the card.
    """
    parsed = _timestamp(expiry)
    if parsed is None:
        return True
    return parsed - _EXPIRY_SKEW <= now


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _windows(payload: Mapping[str, Any]) -> tuple[ProviderUsageWindow, ...]:
    """Flatten the response's groups -> buckets into usage windows.

    The API reports what is LEFT; the cockpit renders what is USED.
    """
    windows: list[ProviderUsageWindow] = []
    groups = payload.get("groups")
    buckets: list[Any] = list(payload.get("buckets") or [])
    if isinstance(groups, list):
        for group in groups:
            if isinstance(group, Mapping):
                buckets.extend(group.get("buckets") or [])
    for bucket in buckets:
        if not isinstance(bucket, Mapping) or bucket.get("disabled"):
            continue
        bucket_id = str(bucket.get("bucketId") or "").strip()
        if not bucket_id:
            continue
        remaining = bucket.get("remainingFraction")
        used_percent: float | None = None
        if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
            used_percent = max(0.0, min(100.0, (1.0 - float(remaining)) * 100.0))
        windows.append(
            ProviderUsageWindow(
                kind=_BUCKET_KINDS.get(bucket_id, bucket_id.replace("-", "_")),
                used_percent=used_percent,
                remaining_value=(
                    bucket.get("remainingAmount")
                    if isinstance(bucket.get("remainingAmount"), (int, float))
                    else None
                ),
                window_minutes=_WINDOW_MINUTES.get(str(bucket.get("window") or "")),
                resets_at=_timestamp(bucket.get("resetTime")),
            )
        )
    return tuple(windows)


@lru_cache(maxsize=1)
def _agy_oauth_clients() -> tuple[tuple[str, str], ...]:
    """agy's installed-app OAuth clients, scanned out of the agy binary.

    Every (id, secret) pairing is returned because the binary's string table
    interleaves them with no structure that says which goes with which; the
    caller tries them until the token endpoint accepts one. Cached for the
    process, and only ever reached when a token has actually expired, so the
    scan does not sit in the common path.
    """
    binary = shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
    ids: list[str] = []
    secrets: list[str] = []
    try:
        with open(binary, "rb") as handle:
            tail = b""
            while True:
                chunk = handle.read(_BINARY_SCAN_CHUNK)
                if not chunk:
                    break
                window = tail + chunk
                ids.extend(m.group().decode() for m in _CLIENT_ID_RE.finditer(window))
                secrets.extend(
                    m.group().decode() for m in _CLIENT_SECRET_RE.finditer(window)
                )
                # Overlap so a credential straddling a chunk boundary is not
                # sliced in half and missed.
                tail = window[-128:]
    except OSError:
        return ()
    seen_ids = list(dict.fromkeys(ids))
    seen_secrets = list(dict.fromkeys(secrets))
    return tuple((i, s) for i in seen_ids for s in seen_secrets)


def _read_keychain() -> str | None:
    """agy's token from the macOS Keychain. Absent everywhere else."""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                _KEYCHAIN_SERVICE,
                "-a",
                _KEYCHAIN_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _http_post(
    url: str, headers: dict[str, str], body: bytes, timeout: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        # A 401/403/429 is an answer the caller must classify, not a crash.
        return exc.code, exc.read()


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
    fingerprint: dict[str, Any] = {
        "provider": "google",
        "account_label": account_label,
        "plan_label": plan_label,
        "host_id": host_id,
        "status": status,
        "windows": [
            {
                "kind": window.kind,
                "used_percent": window.used_percent,
                "window_minutes": window.window_minutes,
                "resets_at": (
                    window.resets_at.isoformat() if window.resets_at else None
                ),
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
        provider="google",
        account_label=account_label,
        plan_label=plan_label,
        host_id=host_id,
        status=status,  # type: ignore[arg-type]
        observed_at=observed_at,
        windows=windows,
        source=_SOURCE,
        error_category=error_category,
    )
