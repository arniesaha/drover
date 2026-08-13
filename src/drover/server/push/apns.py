"""APNs delivery for "needs you" transitions.

The provider API is HTTP/2 only (the legacy binary interface is long gone),
authenticated with a short-lived ES256 JWT signed by the ``.p8`` auth key
downloaded once from the Apple developer portal. The key never leaves the
host; only the JWT goes to Apple, and it is cached because Apple rejects a
provider that mints a fresh token more than once every 20 minutes.

Delivery is fire-and-forget on a background worker. ``update_session_activity``
runs inside the event-ingest request path, and a synchronous round trip to
Apple would put ~100ms of transatlantic latency on every batch of harness
events. A push that fails is a push that is lost: the app still has the
foreground watcher and the BGTask poller behind it, so the badge and the
sessions list stay truthful even when this path is down.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Apple's documented ceiling is 1 hour and it rejects refreshes more often
# than once per 20 minutes, so sit in the middle: long enough never to be
# rate-limited, short enough never to present an expired token.
_TOKEN_LIFETIME_SECONDS = 45 * 60
_HOSTS = {
    "production": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}
_AWAITING_TITLES = {
    "approval": "approval required",
    "input": "your turn",
}


@dataclass(frozen=True)
class APNsConfig:
    """Everything the sender needs; ``enabled`` gates the whole path."""

    enabled: bool
    key_path: Path
    key_id: str
    team_id: str
    bundle_id: str

    @property
    def is_usable(self) -> bool:
        return bool(
            self.enabled
            and self.key_id
            and self.team_id
            and self.bundle_id
            and self.key_path
            and Path(self.key_path).is_file()
        )


@dataclass(frozen=True)
class AwaitingTransition:
    """A session that just started (or stopped) waiting on the user."""

    session_id: str
    harness: str
    cwd: str | None
    awaiting: str | None
    #: What the agent last said, already redacted by the registry. Empty when
    #: the session has no visible assistant output yet.
    preview: str = ""

    @property
    def needs_user(self) -> bool:
        return self.awaiting in _AWAITING_TITLES

    def alert_title(self) -> str:
        return f"{self.harness or 'Harness'} needs you"

    def alert_subtitle(self) -> str:
        """Where and what, so the body is free to carry the agent's words.

        Empty without a preview, because then the body already says exactly
        this and a notification repeating itself reads worse than a plain one.
        """
        if not self.summary():
            return ""
        return f"{self._project()} · {_AWAITING_TITLES.get(self.awaiting or '', 'your turn')}"

    def alert_body(self) -> str:
        """The agent's own words when there are any, else the old summary.

        "claude-code needs you / drover — your turn" tells you a session
        stopped, not whether it stopped on something you care about. Quoting
        the last message is the difference between unlocking the phone to find
        out and knowing before you do.
        """
        return self.summary() or self._fallback_body()

    def summary(self) -> str:
        return _condense(self.preview)

    def _fallback_body(self) -> str:
        suffix = _AWAITING_TITLES.get(self.awaiting or "", "your turn")
        return f"{self._project()} — {suffix}"

    def _project(self) -> str:
        return _basename(self.cwd) or self.harness or "session"


def _basename(cwd: str | None) -> str:
    if not cwd:
        return ""
    return Path(cwd).name


#: Long enough to carry a real sentence or two on a lock screen, short enough
#: that iOS is not the thing doing the truncating (it shows ~4 lines expanded).
_SUMMARY_MAX_CHARS = 220


def _condense(text: str | None) -> str:
    """Flatten an agent message into one notification-shaped line.

    Assistant output is markdown over many lines ("Here's where things
    stand.\\n**Done**\\n- ..."), and a notification renders none of it: the
    newlines collapse anyway and the bold markers show up as literal asterisks.
    So whitespace folds to single spaces and the emphasis markers are dropped,
    while backticks stay -- `git push --force` reads as code even unrendered,
    and stripping them would silently change what a command looks like.
    """
    if not text:
        return ""
    condensed = " ".join(str(text).split())
    condensed = condensed.replace("**", "").replace("__", "")
    # Leading list/heading markers survive the whitespace fold as stray
    # punctuation once the line breaks are gone.
    condensed = re.sub(r"(?:^|(?<= ))[#>]+ (?=\S)", "", condensed)
    condensed = re.sub(r"(?:^|(?<= ))[-*+] (?=\S)", "• ", condensed)
    condensed = condensed.strip()
    if len(condensed) <= _SUMMARY_MAX_CHARS:
        return condensed
    # Cut on a word boundary; a mid-word truncation reads like corruption.
    cut = condensed[:_SUMMARY_MAX_CHARS]
    space = cut.rfind(" ")
    if space > _SUMMARY_MAX_CHARS // 2:
        cut = cut[:space]
    cut = cut.rstrip(" ,;:—-•")
    # A cut that happens to land on a sentence end is already a clean stop;
    # "inflated the counts.…" reads like a typo rather than a truncation.
    return cut if cut.endswith((".", "!", "?")) else cut + "…"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class _AuthToken:
    """Caches the signed provider JWT and re-signs it only when stale."""

    def __init__(self, config: APNsConfig):
        self._config = config
        self._lock = threading.Lock()
        self._token: str | None = None
        self._issued_at = 0.0
        self._private_key = None

    def _load_key(self):
        if self._private_key is None:
            from cryptography.hazmat.primitives import serialization

            pem = Path(self._config.key_path).read_bytes()
            self._private_key = serialization.load_pem_private_key(pem, password=None)
        return self._private_key

    def value(self, *, now: float | None = None) -> str:
        now = time.time() if now is None else now
        with self._lock:
            if (
                self._token is not None
                and now - self._issued_at < _TOKEN_LIFETIME_SECONDS
            ):
                return self._token
            self._token = self._sign(int(now))
            self._issued_at = now
            return self._token

    def _sign(self, issued_at: int) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import (
            decode_dss_signature,
        )

        header = {"alg": "ES256", "kid": self._config.key_id}
        claims = {"iss": self._config.team_id, "iat": issued_at}
        signing_input = ".".join(
            _b64url(json.dumps(part, separators=(",", ":")).encode("utf-8"))
            for part in (header, claims)
        ).encode("ascii")

        der = self._load_key().sign(signing_input, ec.ECDSA(hashes.SHA256()))
        # `cryptography` returns a DER-wrapped signature; JWS ES256 wants the
        # raw fixed-width r||s pair. Skipping this conversion produces a token
        # Apple rejects with a 403 InvalidProviderToken and no other clue.
        r, s = decode_dss_signature(der)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{signing_input.decode('ascii')}.{_b64url(raw)}"


class APNsSender:
    """Sends one alert per awaiting transition to every paired device.

    ``credentials`` is the live ``CredentialStore``; device tokens are read at
    send time rather than cached so a revoked or re-paired phone stops getting
    alerts immediately.
    """

    def __init__(
        self,
        config: APNsConfig,
        credentials,
        *,
        client=None,
        max_workers: int = 2,
    ):
        self._config = config
        self._credentials = credentials
        self._auth = _AuthToken(config)
        self._client = client
        self._client_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="apns"
        )

    def _http(self):
        """Lazily build the HTTP/2 client; one connection, reused."""
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    import httpx

                    self._client = httpx.Client(http2=True, timeout=10.0)
        return self._client

    def notify(self, transition: AwaitingTransition) -> None:
        """Queue delivery. Never raises into the caller's request path."""
        if not self._config.is_usable or not transition.needs_user:
            return
        try:
            self._pool.submit(self._deliver, transition)
        except RuntimeError:  # pool shut down during interpreter teardown
            pass

    def _targets(self):
        for credential in self._credentials.list_all():
            if (
                credential.scope == "device"
                and credential.is_active
                and credential.apns_token
                and credential.apns_environment in _HOSTS
            ):
                yield credential

    def _deliver(self, transition: AwaitingTransition) -> None:
        try:
            targets = list(self._targets())
        except Exception as exc:  # noqa: BLE001
            log.debug("apns: could not read credentials: %s", exc)
            return
        if not targets:
            return
        badge = self._needs_user_count()
        for credential in targets:
            self._send_one(credential, transition, badge)

    def _needs_user_count(self) -> int | None:
        """Best-effort badge value; ``None`` leaves the app's badge alone."""
        counter = getattr(self, "_badge_source", None)
        if counter is None:
            return None
        try:
            return int(counter())
        except Exception:  # noqa: BLE001
            return None

    def set_badge_source(self, counter) -> None:
        self._badge_source = counter

    def _payload(self, transition: AwaitingTransition, badge: int | None) -> bytes:
        alert: dict = {
            "title": transition.alert_title(),
            "body": transition.alert_body(),
        }
        subtitle = transition.alert_subtitle()
        if subtitle:
            # Only present alongside a real preview, so the three lines never
            # say the same thing twice.
            alert["subtitle"] = subtitle
        aps: dict = {
            "alert": alert,
            "sound": "default",
            # Matches LocalNotifier's .timeSensitive so a pushed alert and a
            # locally-generated one behave identically under Focus.
            "interruption-level": "time-sensitive",
        }
        if badge is not None:
            aps["badge"] = badge
        body = {"aps": aps, "session_id": transition.session_id}
        return json.dumps(body, separators=(",", ":")).encode("utf-8")

    def _send_one(self, credential, transition: AwaitingTransition, badge) -> None:
        host = _HOSTS.get(credential.apns_environment or "")
        if host is None:
            return
        url = f"{host}/3/device/{credential.apns_token}"
        headers = {
            "authorization": f"bearer {self._auth.value()}",
            "apns-topic": self._config.bundle_id,
            "apns-push-type": "alert",
            "apns-priority": "10",
            # One session collapses onto itself: a phone that was offline
            # through three transitions wakes to the latest state, not three
            # stacked banners for the same session.
            "apns-collapse-id": transition.session_id[:64],
        }
        try:
            response = self._http().post(
                url, content=self._payload(transition, badge), headers=headers
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("apns: send failed for %s: %s", credential.id, exc)
            return

        if response.status_code == 200:
            return
        if response.status_code == 410:
            # Apple's "this token is dead" signal (app deleted, or the device
            # re-registered). Drop it so the next pairing is the only source
            # of truth and we stop paying for a send that can never land.
            log.info("apns: token for %s unregistered, clearing", credential.id)
            try:
                self._credentials.clear_apns_registration(
                    credential.id, expected_token=credential.apns_token
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("apns: could not clear registration: %s", exc)
            return
        log.warning(
            "apns: %s rejected for %s: %s",
            response.status_code,
            credential.id,
            response.text[:200],
        )

    def close(self) -> None:
        self._pool.shutdown(wait=False)
        client = self._client
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


# --- module-level dispatch -------------------------------------------------
#
# `HarnessRegistry` is constructed per request from a bare path, so the sender
# is registered once at server startup and looked up here.

_sender: APNsSender | None = None
_sender_lock = threading.Lock()


def set_sender(sender: APNsSender | None) -> None:
    global _sender
    with _sender_lock:
        _sender = sender


def configure(cfg, credentials) -> APNsSender | None:
    """Build and register the sender from a ``DroverConfig``.

    Called once at server startup. A configured-but-unusable key (typo'd path,
    missing ids) logs loudly rather than failing the boot: losing push is a
    degraded notification path, not a reason for the cockpit not to come up.
    """
    config = APNsConfig(
        enabled=bool(getattr(cfg, "apns_enabled", False)),
        key_path=Path(getattr(cfg, "apns_key_path", "") or ""),
        key_id=str(getattr(cfg, "apns_key_id", "") or ""),
        team_id=str(getattr(cfg, "apns_team_id", "") or ""),
        bundle_id=str(getattr(cfg, "apns_bundle_id", "") or ""),
    )
    if not config.is_usable:
        set_sender(None)
        if config.enabled:
            log.warning(
                "apns: enabled but not usable (key_path=%s key_id=%s team_id=%s); "
                "push disabled, falling back to in-app polling",
                config.key_path or "<unset>",
                config.key_id or "<unset>",
                config.team_id or "<unset>",
            )
        return None
    sender = APNsSender(config, credentials)
    set_sender(sender)
    log.info("apns: push enabled for %s (key %s)", config.bundle_id, config.key_id)
    return sender


def dispatch_awaiting_transition(transition: AwaitingTransition) -> None:
    """Called from the registry on a real change. Never raises."""
    sender = _sender
    if sender is None:
        return
    try:
        sender.notify(transition)
    except Exception as exc:  # noqa: BLE001
        log.debug("apns: dispatch failed: %s", exc)
