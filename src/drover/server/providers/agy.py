"""Bounded, host-local Antigravity (agy) account and capacity probe.

Reads this host's signed-in account and quota, returning a
ProviderAccountSnapshot.

``~/.gemini`` is agy's own state directory, not a Gemini CLI leftover: it
holds ``antigravity-cli/`` beside ``oauth_creds.json`` and
``google_accounts.json`` on older versions, and a signed-in agy writes it directly (verified
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
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from drover.server.providers.types import ProviderAccountSnapshot, ProviderUsageWindow

log = logging.getLogger(__name__)

_SOURCE = "agy-usage"
_OBSERVED_SOURCE = "observed_429"
_SLIDING_WINDOW_TOLERANCE_SECONDS = 120.0

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

_RESET_DURATION_PATTERN = re.compile(
    r"Resets in\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ObservedExhaustion:
    host_id: str
    model_group: str
    resets_at: datetime
    observed_at: datetime


_OBSERVED_EXHAUSTIONS: dict[tuple[str, str], ObservedExhaustion] = {}
_OBSERVED_LOCK = threading.Lock()


def parse_agy_quota_exhaustion(
    text: str, now: datetime | None = None
) -> datetime | None:
    """Parse reset time from an agy 429 quota exhaustion message."""
    if not isinstance(text, str) or not text.strip():
        return None
    lower = text.lower()
    indicators = (
        "resource_exhausted",
        "individual quota reached",
        "agy_error",
        "quota reached",
        "code 429",
        "resets in",
    )
    if not any(ind in lower for ind in indicators):
        return None
    match = _RESET_DURATION_PATTERN.search(text)
    if not match:
        return None
    h_str, m_str, s_str = match.groups()
    if not h_str and not m_str and not s_str:
        return None
    hours = int(h_str) if h_str else 0
    minutes = int(m_str) if m_str else 0
    seconds = int(s_str) if s_str else 0
    duration = timedelta(hours=hours, minutes=minutes, seconds=seconds)
    if duration.total_seconds() <= 0:
        return None
    base_time = now if now is not None else datetime.now(timezone.utc)
    if base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=timezone.utc)
    return base_time + duration


def model_group_from_model(model: str | None, hint_text: str | None = None) -> str:
    """Map a model identifier to its agy model group ('gemini' vs '3p').

    Without a model, fall back to the 429 text itself ("quota reached for
    Claude Sonnet ...") so a 3p exhaustion is never pinned on Gemini.
    """
    if not model:
        hint = (hint_text or "").lower()
        if "claude" in hint or "gpt" in hint:
            return "3p"
        return "gemini"
    m = model.strip().lower()
    if m.startswith("gemini"):
        return "gemini"
    if m.startswith("claude") or m.startswith("gpt") or "3p" in m:
        return "3p"
    return "gemini"


def record_observed_exhaustion(
    host_id: str,
    model_group: str,
    resets_at: datetime,
    observed_at: datetime | None = None,
    store: dict[tuple[str, str], ObservedExhaustion] | None = None,
) -> None:
    host = str(host_id or "local").strip() or "local"
    group = "gemini" if model_group == "gemini" else "3p"
    obs_at = observed_at or datetime.now(timezone.utc)
    if obs_at.tzinfo is None:
        obs_at = obs_at.replace(tzinfo=timezone.utc)
    if resets_at.tzinfo is None:
        resets_at = resets_at.replace(tzinfo=timezone.utc)
    with _OBSERVED_LOCK:
        target_store = _OBSERVED_EXHAUSTIONS if store is None else store
        target_store[(host, group)] = ObservedExhaustion(
            host_id=host,
            model_group=group,
            resets_at=resets_at,
            observed_at=obs_at,
        )


def get_active_observed_exhaustions(
    host_id: str | None = None,
    now: datetime | None = None,
    store: dict[tuple[str, str], ObservedExhaustion] | None = None,
) -> list[ObservedExhaustion]:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    active: list[ObservedExhaustion] = []
    with _OBSERVED_LOCK:
        target_store = _OBSERVED_EXHAUSTIONS if store is None else store
        expired_keys = []
        for key, item in list(target_store.items()):
            if item.resets_at <= current_time:
                expired_keys.append(key)
                continue
            if host_id is None or item.host_id == host_id:
                active.append(item)
        for key in expired_keys:
            target_store.pop(key, None)
    return active


def clear_observed_exhaustions(
    host_id: str | None = None,
    store: dict[tuple[str, str], ObservedExhaustion] | None = None,
) -> None:
    with _OBSERVED_LOCK:
        target_store = _OBSERVED_EXHAUSTIONS if store is None else store
        if host_id is None:
            target_store.clear()
        else:
            for key in list(target_store.keys()):
                if key[0] == host_id:
                    target_store.pop(key, None)


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
        observed_exhaustions: dict[tuple[str, str], ObservedExhaustion] | None = None,
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
        self.observed_exhaustions = observed_exhaustions

    def read(self, *, host_id: str = "local") -> ProviderAccountSnapshot:
        observed_at = self.now()
        account_label, account_identity = self._account_metadata()
        active_exhaustions = get_active_observed_exhaustions(
            host_id=host_id, now=observed_at, store=self.observed_exhaustions
        )
        try:
            windows = self._fetch_windows(observed_at=observed_at)
            status = "ok" if windows else "usage_unavailable"
            error_category = None if windows else "quota_api_unreachable"
        except _ProbeFailure as exc:
            windows = ()
            status = exc.status
            error_category = exc.category
        except Exception:  # noqa: BLE001 -- read() must never raise
            log.debug("agy capacity probe failed", exc_info=True)
            windows = ()
            status = "usage_unavailable"
            error_category = "probe_failed"

        source = _SOURCE
        if active_exhaustions:
            status = "observed_exhausted"
            source = _OBSERVED_SOURCE
            error_category = None
            windows_by_kind = {w.kind: w for w in windows}
            for ex in active_exhaustions:
                duration_seconds = (ex.resets_at - observed_at).total_seconds()
                is_weekly = duration_seconds > 5 * 3600
                if ex.model_group == "gemini":
                    kind = "seven_day" if is_weekly else "five_hour"
                    window_minutes = 10080 if is_weekly else 300
                else:
                    kind = (
                        "seven_day_claude_gpt" if is_weekly else "five_hour_claude_gpt"
                    )
                    window_minutes = 10080 if is_weekly else 300
                windows_by_kind[kind] = ProviderUsageWindow(
                    kind=kind,
                    used_percent=100.0,
                    remaining_value=0.0,
                    window_minutes=window_minutes,
                    resets_at=ex.resets_at,
                )
            windows = tuple(windows_by_kind.values())

        return _snapshot(
            host_id=host_id,
            account_label=account_label,
            account_identity=account_identity,
            status=status,
            observed_at=observed_at,
            windows=windows,
            plan_label=None,
            error_category=error_category,
            source=source,
        )

    def _fetch_windows(
        self, observed_at: datetime | None = None
    ) -> tuple[ProviderUsageWindow, ...]:
        """Capacity windows for this account, from agy's own quota endpoint."""
        return _windows(
            self._fetch(self._access_token()),
            fetch_time=observed_at or self.now(),
        )

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
        from drover.server.staging_credentials import is_staging

        sources = (
            (self._file_blob,)
            if is_staging()
            else (self._keychain_blob, self._file_blob)
        )
        for load in sources:
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

    def stored_account(self) -> tuple[str, str] | None:
        """Local sign-in metadata shared with auth and model discovery.

        The stored access/refresh credential indicates local sign-in; ID token
        claims provide identity only, not proof that the server accepts it.
        No network refresh, credential writes, or logging occurs here. Return
        an opaque scope for credentials that have no usable identity claims.
        """
        try:
            credential = json.loads(self._credential_blob())
            token = credential.get("token")
            if not isinstance(token, dict):
                return None
            secret = token.get("refresh_token") or token.get("access_token")
            if not isinstance(secret, str) or not secret.strip():
                return None
            try:
                metadata = _credential_account_metadata(credential)
            except (ValueError, AttributeError, TypeError):
                metadata = None
            if metadata is not None:
                return metadata
            return (
                "Unknown account",
                "credential:" + hashlib.sha256(secret.encode()).hexdigest(),
            )
        except Exception:
            # An unreadable credential must not leak its contents or suppress
            # discovery through the older account layout.
            return None

    def _account_metadata(self) -> tuple[str, str | None]:
        """Identity from the credential actually used for quota, then legacy state.

        agy 1.2.11 stores the signed-in email in the credential's ID token on
        macOS too (Keychain or antigravity-cli/antigravity-oauth-token). Decode
        claims only as local display metadata, never as authentication proof.
        Old account history is not evidence of the currently signed-in user.
        """
        try:
            credential = json.loads(self._credential_blob())
            metadata = _credential_account_metadata(credential)
            if metadata is not None:
                return metadata
        except Exception:  # Identity failure must not suppress quota reporting.
            pass
        try:
            raw = json.loads(self.accounts_path.read_text(encoding="utf-8"))
            active = raw.get("active")
            if isinstance(active, str) and "@" in active and active.strip():
                return active.strip(), active.strip().lower()
        except (OSError, ValueError, AttributeError):
            pass
        return "Unknown account", None


def _credential_account_metadata(credential: Any) -> tuple[str, str] | None:
    encoded = credential.get("id_token")
    if isinstance(encoded, str):
        parts = encoded.split(".")
        if len(parts) == 3:
            claims = json.loads(
                base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
            )
            email = claims.get("email")
            if isinstance(email, str) and "@" in email and email.strip():
                return email.strip(), email.strip().lower()
            subject = claims.get("sub")
            if isinstance(subject, str) and subject.strip():
                identity = hashlib.sha256(subject.strip().encode()).hexdigest()
                return "Unknown account", "google-sub:" + identity
    return None


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


def _windows(
    payload: Mapping[str, Any], *, fetch_time: datetime | None = None
) -> tuple[ProviderUsageWindow, ...]:
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
        resets_at = _timestamp(bucket.get("resetTime"))
        window_minutes = _WINDOW_MINUTES.get(str(bucket.get("window") or ""))

        remaining = bucket.get("remainingFraction")
        # An untouched bucket whose resetTime is within ~2 min of (fetch time +
        # window length) slides with every call: it is not tracking the real
        # individual quota (drover#522). Omit it rather than report 0% used.
        # A bucket with consumption has a fixed reset, even if the window
        # started moments ago, so it is kept.
        untouched = not (
            isinstance(remaining, (int, float))
            and not isinstance(remaining, bool)
            and remaining < 1
        )
        if (
            untouched
            and fetch_time is not None
            and resets_at is not None
            and window_minutes is not None
        ):
            expected_sliding = fetch_time + timedelta(minutes=window_minutes)
            if (
                abs((resets_at - expected_sliding).total_seconds())
                <= _SLIDING_WINDOW_TOLERANCE_SECONDS
            ):
                continue

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
                window_minutes=window_minutes,
                resets_at=resets_at,
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
    account_identity: str | None,
    status: str,
    observed_at: datetime,
    windows: tuple[ProviderUsageWindow, ...],
    plan_label: str | None,
    error_category: str | None,
    source: str = _SOURCE,
) -> ProviderAccountSnapshot:
    fingerprint: dict[str, Any] = {
        "provider": "google",
        "account_label": account_label,
        "account_identity": account_identity,
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
        "source": source,
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
        account_identity=account_identity,
        plan_label=plan_label,
        host_id=host_id,
        status=status,  # type: ignore[arg-type]
        observed_at=observed_at,
        windows=windows,
        source=source,
        error_category=error_category,
    )
