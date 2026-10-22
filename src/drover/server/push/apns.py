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
import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
_BODY_MAX_CHARS = 140
_APNS_MAX_BYTES = 4096
_APPROVAL_FLAG_SECRET_RE = re.compile(
    r"(?i)(?<!\S)(--(?:token|password)|-p)(?:=|\s+)(?:\"[^\"]*\"|'[^']*'|[^\s`]+)"
)
_APPROVAL_URL_USERINFO_RE = re.compile(r"https?://[^\s/@:]+:[^@\s]+@[^\s`]+")
_HIGH_ENTROPY_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_=-]{32,}(?![A-Za-z0-9+/_=-])"
)


def _unavailable_content_consent() -> dict:
    """The safe default when a sender was not given the hub consent reader."""
    return {}


def _next_digest_deadline(now: float, timezone_name: str, hour: int | None) -> float:
    """Return the next preferred wall-clock time, including DST transitions.

    The first valid instant after a skipped wall time is used (for example 03:00
    after a spring-forward 02:00); an ambiguous fall-back hour uses its first
    occurrence.  The result is computed from an IANA zone, never a fixed UTC
    offset, so a device keeps its local schedule across seasonal changes.
    """
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError):
        zone = timezone.utc
    local_now = datetime.fromtimestamp(now, timezone.utc).astimezone(zone)
    preferred_hour = 0 if hour is None else hour
    candidate_date = local_now.date()
    candidate = datetime.combine(
        candidate_date, datetime.min.time(), tzinfo=zone
    ).replace(hour=preferred_hour)
    if candidate.timestamp() <= now:
        candidate_date += timedelta(days=1)
        candidate = datetime.combine(
            candidate_date, datetime.min.time(), tzinfo=zone
        ).replace(hour=preferred_hour)
    # ZoneInfo permits imaginary times. Advance to the first representable
    # minute after the gap instead of sending at a different UTC offset.
    while candidate.astimezone(timezone.utc).astimezone(zone).replace(
        tzinfo=None
    ) != candidate.replace(tzinfo=None):
        candidate += timedelta(minutes=1)
    return candidate.timestamp()


# --- rejection classification ------------------------------------------------
#
# A stored registration tells the iOS app to stop generating its own alerts, so
# every rejection that means "this can never land" has to undo that promise,
# not just log. The split is by what the rejection condemns:
#
# * the device: this one token is dead or belongs elsewhere. Clear it.
# * the environment: our key/topic cannot reach this APNs host at all, so every
#   device registered against it is in the same position. Stop offering push
#   for that environment until the operator fixes the config and restarts.
# * nothing: Apple is busy or the network blinked. Keep everything.
#
# Reasons are Apple's documented ``reason`` strings for the provider API.

#: Permanent for this token in this environment.
_DEVICE_REJECTIONS = frozenset(
    {
        "BadDeviceToken",
        "DeviceTokenNotForTopic",
        "Unregistered",
        "ExpiredToken",
    }
)
#: Permanent for every token in this environment: the provider key, its
#: environment restriction, or the topic is wrong. ``BadEnvironmentKeyInToken``
#: is the TestFlight-with-a-sandbox-only-key case; it is a property of the key,
#: so no device in that environment can be reached either.
_ENVIRONMENT_REJECTIONS = frozenset(
    {
        "BadEnvironmentKeyInToken",
        "InvalidProviderToken",
        "ExpiredProviderToken",
        "MissingProviderToken",
        "TopicDisallowed",
        "BadTopic",
        "BadCertificate",
        "BadCertificateEnvironment",
    }
)

DELIVERED = "delivered"
DEVICE_REJECTED = "device"
ENVIRONMENT_REJECTED = "environment"
TRANSIENT = "transient"
REJECTED = "rejected"

#: How often an identical rejection may be logged. A broken environment stops
#: being sent to after the first rejection, but a per-message problem (an
#: oversized payload, say) would otherwise log on every transition.
_LOG_INTERVAL_SECONDS = 300.0


@dataclass(frozen=True)
class DeliveryOutcome:
    kind: str
    status: int | None
    reason: str = ""


def _response_reason(text: str | None) -> str:
    try:
        body = json.loads(text or "")
    except ValueError:
        return ""
    reason = body.get("reason") if isinstance(body, dict) else None
    return reason if isinstance(reason, str) else ""


def classify_response(status_code: int, text: str | None) -> DeliveryOutcome:
    """Map an APNs provider response onto what it condemns."""
    reason = _response_reason(text)
    if status_code == 200:
        return DeliveryOutcome(DELIVERED, status_code, reason)
    if status_code == 429 or status_code >= 500:
        return DeliveryOutcome(TRANSIENT, status_code, reason)
    if reason in _ENVIRONMENT_REJECTIONS:
        return DeliveryOutcome(ENVIRONMENT_REJECTED, status_code, reason)
    if status_code == 410 or reason in _DEVICE_REJECTIONS:
        return DeliveryOutcome(DEVICE_REJECTED, status_code, reason or "Unregistered")
    return DeliveryOutcome(REJECTED, status_code, reason)


@dataclass(frozen=True)
class EnvironmentFailure:
    status: int | None
    reason: str
    since: str


class _RateLimitedLog:
    """Lets one line per key through per interval."""

    def __init__(self, interval: float = _LOG_INTERVAL_SECONDS):
        self._interval = interval
        self._lock = threading.Lock()
        self._last: dict[tuple, float] = {}

    def allow(self, key: tuple, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        with self._lock:
            last = self._last.get(key)
            if last is not None and moment - last < self._interval:
                return False
            self._last[key] = moment
            return True


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
    """A session state transition, with public notification context."""

    session_id: str
    harness: str
    cwd: str | None
    awaiting: str | None
    #: What the agent last said, already redacted by the registry. Empty when
    #: the session has no visible assistant output yet.
    preview: str = ""

    title: str = ""
    status: str = ""
    private: bool = False
    repo_name: str | None = None
    #: Human-facing host label. This is metadata and remains safe when content
    #: consent is off.
    host: str = ""
    #: A compact, already-redacted approval description (if this is one).
    approval_summary: str = ""
    #: Content previews and prompt-derived titles are opt-in. ``private`` is
    #: retained for profile-scoped sessions; both gates fail closed.
    content_allowed: bool = True
    #: Number of state changes folded into this delivery.
    update_count: int = 1
    #: Count of sessions currently waiting for the person, not event count.
    badge_count: int | None = None
    #: Session creation time is metadata, not transcript content.  It keeps a
    #: consent-gated alert useful without naming the repository or prompt.
    started_at: datetime | None = None

    @property
    def needs_user(self) -> bool:
        return self.awaiting in _AWAITING_TITLES and not self.status in {
            "completed",
            "failed",
            "errored",
            "terminated",
        }

    @property
    def kind(self) -> str:
        if self.status in {"failed", "errored"}:
            return "failed"
        if self.status == "completed":
            return "finished"
        return "input" if self.needs_user else "progress"

    def allowed(self, mode: str) -> bool:
        """Whether this transition belongs in a device's selected mode.

        ``action`` deliberately means only a live input/approval request.  A
        terminal state is useful, but it is not an action the person can take;
        treating it as one made the default mode behave like a reduced version
        of ``all``.  A digest is a delayed summary of the same state changes as
        ``all``.
        """
        if mode in {"all", "digest"}:
            return True
        return mode == "action" and self.needs_user

    def alert_title(self) -> str:
        if not self.allows_content:
            return {
                "input": "drover needs your input",
                "finished": "drover finished a session",
                "failed": "drover needs attention",
                "progress": "drover session update",
            }[self.kind]
        project = self.project_name()
        if self.private:
            return project or "Session"
        session = self._safe_text(self.title)
        if project and session:
            return f"{project} · {session}"[:120]
        return (project or session or "Session")[:120]

    def alert_subtitle(self) -> str:
        return self._safe_text(self.host)[:60]

    def alert_body(self) -> str:
        prefix = {
            "input": "Needs your input",
            "finished": "Finished",
            "failed": "Failed",
            "progress": "Update",
        }[self.kind]
        fallback = {
            "input": (
                "approve a command"
                if self.awaiting == "approval"
                else "reply in the app"
            ),
            "finished": "ready for review",
            "failed": "open the session for details",
            "progress": "work is continuing",
        }[self.kind]
        if not self.allows_content:
            # ``_condense`` deliberately removes harness names from untrusted
            # transcript text.  This path has already reduced the trusted
            # metadata to a bounded label, so preserve it for usefulness.
            return self._metadata_body()
        if self.private:
            return _condense(f"{prefix}: {fallback}", _BODY_MAX_CHARS)
        summary = (
            self.approval_summary if self.awaiting == "approval" else self.summary()
        )
        if self.awaiting == "approval" and summary:
            return _condense(
                _redact_approval_summary(summary),
                _BODY_MAX_CHARS,
                keep_code=True,
                already_redacted=True,
            )
        if self.kind == "input" and summary:
            return _condense(summary, _BODY_MAX_CHARS)
        return _condense(f"{prefix}: {summary or fallback}", _BODY_MAX_CHARS)

    @property
    def allows_content(self) -> bool:
        return self.content_allowed

    def project_name(self) -> str:
        return (
            self._safe_text(self.repo_name) or self._safe_text(_basename(self.cwd))
        )[:60]

    def _metadata_prefix(self) -> str:
        return {
            "input": "Needs your input",
            "finished": "Finished",
            "failed": "Failed",
            "progress": "Update",
        }[self.kind]

    def _metadata_body(self) -> str:
        """Describe a session state without leaking session-derived text.

        The title already says what happened and the subtitle carries the
        host.  Repeating either in the body wastes the one useful lock-screen
        line we retain when content consent is off.  Harness and session age
        are operational metadata, never transcript, repo, or prompt content.
        """
        session = f"{self._harness_label()} session"
        opened = self._opened_age()
        detail = {
            "input": session,
            "finished": f"{session} finished",
            "failed": f"{session} needs attention",
            "progress": f"{session} is still working",
        }[self.kind]
        return " · ".join(part for part in (detail, opened) if part)

    def _harness_label(self) -> str:
        """A bounded display label for the harness metadata field."""
        raw = re.sub(r"[^A-Za-z0-9 ._+-]", "", str(self.harness or ""))
        normalized = raw.strip().lower().replace("_", "-")
        known = {
            "codex": "Codex",
            "claude": "Claude",
            "claude-code": "Claude Code",
            "gemini": "Gemini",
            "opencode": "OpenCode",
        }
        if normalized in known:
            return known[normalized]
        return raw[:32] or "Agent"

    def _opened_age(self, *, now: datetime | None = None) -> str:
        started = self.started_at
        if not isinstance(started, datetime):
            return ""
        if started.tzinfo is None:
            # DuckDB returns its legacy TIMESTAMPTZ values as local naive
            # datetimes; PostgreSQL returns an aware value.  Treat only the
            # former as local so a generic age is not shifted by the host TZ.
            started = started.replace(tzinfo=datetime.now().astimezone().tzinfo)
        moment = now or datetime.now(timezone.utc)
        seconds = max(0, int((moment - started).total_seconds()))
        if seconds < 90:
            return "opened just now"
        if seconds < 90 * 60:
            return f"opened {seconds // 60} min ago"
        if seconds < 36 * 60 * 60:
            return f"opened {seconds // 3600}h ago"
        return f"opened {seconds // 86400}d ago"

    def summary(self) -> str:
        # Git delivery details and command transcripts belong in the session.
        lines = [
            line
            for line in self.preview.splitlines()
            if not re.match(
                r"\s*(?:[-*]\s*)?(?:Pushed|Committed|Commit|SHA|Run ID|Session ID)\b",
                line,
                re.I,
            )
        ]
        return self._safe_text(" ".join(lines))

    def _safe_text(self, text: str | None) -> str:
        for identifier in (self.session_id, self.harness):
            if identifier and text:
                text = re.sub(re.escape(identifier), "", text, flags=re.I)
        return _condense(text)


def _collapse_id(session_id: str, kind: str = "") -> str:
    # APNs limits bytes, not characters. Hash long/non-ASCII IDs without collisions.
    value = f"{session_id}:{kind}" if kind else session_id
    if value.isascii() and len(value.encode()) <= 64:
        return value
    return hashlib.sha256(value.encode()).hexdigest()


def _basename(cwd: str | None) -> str:
    if not cwd:
        return ""
    return Path(cwd).name


#: Long enough to carry a real sentence or two on a lock screen, short enough
#: that iOS is not the thing doing the truncating (it shows ~4 lines expanded).
_SUMMARY_MAX_CHARS = 220


def _condense(
    text: str | None,
    maximum: int = _SUMMARY_MAX_CHARS,
    *,
    keep_code: bool = False,
    already_redacted: bool = False,
) -> str:
    """Flatten an agent message into one notification-shaped line.

    Assistant output is markdown over many lines ("Here's where things
    stand.\\n**Done**\\n- ..."), and a notification renders none of it: the
    newlines collapse anyway and the bold markers show up as literal asterisks.
    So whitespace folds to single spaces and the emphasis markers are dropped,
    and code delimiters are removed. Machine identifiers and locations are
    stripped before truncation.
    """
    if not text:
        return ""
    # Keep identifiers, secrets, links, and machine locations off the lock screen.
    from drover.server.harness.auth import redact_auth_text

    text = str(text) if already_redacted else redact_auth_text(str(text))
    text = re.sub(
        r"\b(?:run|session|harness)[_ -]?id\s*[:=]\s*[^\s,;]+", "", text, flags=re.I
    )
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "", text)
    text = re.sub(r"\b(?:codex|claude-code|gemini|opencode)\b", "", text, flags=re.I)
    text = re.sub(r"\b(?:harness|run|session)[-_][\w-]+\b", "", text, flags=re.I)
    text = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b[0-9a-f]{7,64}\b", "", text, flags=re.I
    )
    text = re.sub(
        r"https?://\S+|(?:[/~][\w.~-]+){2,}|\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[\w-]+\.(?:local|internal|lan)\b",
        "",
        text,
    )
    text = text.replace("—", ":")
    if not keep_code:
        text = text.replace("`", "")
    condensed = " ".join(str(text).split())
    condensed = condensed.replace("**", "").replace("__", "")
    # Markdown emphasis around a word, but not underscores inside identifiers.
    condensed = re.sub(r"(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)", "", condensed)
    # Leading list/heading markers survive the whitespace fold as stray
    # punctuation once the line breaks are gone.
    condensed = re.sub(r"(?:^|(?<= ))[#>]+ (?=\S)", "", condensed)
    condensed = re.sub(r"(?:^|(?<= ))[-*+] (?=\S)", "• ", condensed)
    condensed = condensed.strip()
    if len(condensed) <= maximum:
        return condensed
    # Cut on a word boundary; a mid-word truncation reads like corruption.
    cut = condensed[: maximum - 1]
    space = cut.rfind(" ")
    if space > maximum // 2:
        cut = cut[:space]
    cut = cut.rstrip(" ,;:—-•")
    # A cut that happens to land on a sentence end is already a clean stop;
    # "inflated the counts.…" reads like a typo rather than a truncation.
    return cut if cut.endswith((".", "!", "?")) else cut + "…"


def _redact_approval_summary(text: str) -> str:
    """Remove command-shaped secrets before an approval reaches APNs.

    ``redact_auth_text`` is the shared first line of defense. Notifications
    accept shell commands too, though, so cover flags and generic credential
    blobs that do not have a key name. Keep the command shape useful while
    replacing each value with an unambiguous lock-screen-safe marker.
    """
    from drover.server.harness.auth import redact_auth_text

    redacted = _APPROVAL_URL_USERINFO_RE.sub("URL(…)", str(text))
    redacted = redact_auth_text(redacted)
    redacted = _APPROVAL_FLAG_SECRET_RE.sub(r"\1 …", redacted)
    redacted = _HIGH_ENTROPY_SECRET_RE.sub("…", redacted)
    return redacted.replace("<redacted>", "…")


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
    """Batches state transitions according to each paired device's preference.

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
        clock=time.time,
        timer_factory=threading.Timer,
        batch_seconds: float = 60,
        input_batch_seconds: float = 60,
        content_consent_reader=None,
    ):
        self._clock = clock
        self._timer_factory = timer_factory
        self._batch_seconds = batch_seconds
        self._input_batch_seconds = input_batch_seconds
        # Startup supplies the durable hub reader. A missing reader is not
        # permission: content-bearing notification text must fail closed.
        self._content_consent_reader = (
            content_consent_reader or _unavailable_content_consent
        )
        self._pending_lock = threading.Lock()
        self._pending = {}
        # Digest entries survive restarts in the control store. They contain
        # routing state only; previews and other transcript content are never
        # serialized into this queue.
        try:
            for item in credentials.pending_digests():
                state = AwaitingTransition(
                    session_id=item["session_id"],
                    harness="",
                    cwd=None,
                    awaiting=item.get("awaiting"),
                    status=item.get("status") or "",
                )
                entry = self._pending.setdefault(
                    item["credential_id"], [item["due_at"], "digest", {}]
                )
                entry[0] = min(entry[0], item["due_at"])
                entry[2][state.session_id] = [state, item.get("update_count", 1)]
        except Exception as exc:  # persistence must not make push unavailable
            log.debug("apns: could not restore digest queue: %s", exc)
        # Per device/session cooldowns: the first question gets through now;
        # follow-ups are folded into one replacement push at the end of this
        # window. It is deliberately in-memory: losing it on a hub restart is
        # preferable to persisting notification content.
        self._recent: dict[str, dict[str, float]] = {}
        self._timer = None
        self._closed = False
        self._config = config
        self._credentials = credentials
        self._auth = _AuthToken(config)
        self._client = client
        self._client_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="apns"
        )
        self._environment_lock = threading.Lock()
        self._environment_failures: dict[str, EnvironmentFailure] = {}
        self._log_limit = _RateLimitedLog()
        # Re-arm restored durable work; `close()` only clears this process's
        # in-memory view, never the control-store rows.
        with self._pending_lock:
            self._schedule_locked()

    @property
    def is_available(self) -> bool:
        """True when this sender can actually deliver APNs alerts."""
        return self._config.is_usable

    def environment_available(self, environment: str) -> bool:
        """False once Apple refused our key or topic for ``environment``.

        Deliberately sticky until restart: nothing short of a config change
        can fix these rejections, and re-offering push in between would let a
        phone register, suppress its local alerts, and lose the next one.
        """
        if not self.is_available or environment not in _HOSTS:
            return False
        with self._environment_lock:
            return environment not in self._environment_failures

    def environment_failures(self) -> dict[str, EnvironmentFailure]:
        with self._environment_lock:
            return dict(self._environment_failures)

    def status(self) -> dict:
        """Operator-facing push state for readiness and diagnostics."""
        if not self.is_available:
            return {"state": "disabled"}
        failures = self.environment_failures()
        environments: dict[str, dict] = {}
        for environment in _HOSTS:
            failure = failures.get(environment)
            if failure is None:
                environments[environment] = {"available": True}
            else:
                environments[environment] = {
                    "available": False,
                    "status": failure.status,
                    "reason": failure.reason,
                    "since": failure.since,
                }
        return {
            "state": "degraded" if failures else "ok",
            "environments": environments,
        }

    def _mark_environment_unavailable(
        self, environment: str, outcome: DeliveryOutcome
    ) -> None:
        with self._environment_lock:
            if environment in self._environment_failures:
                return
            self._environment_failures[environment] = EnvironmentFailure(
                status=outcome.status,
                reason=outcome.reason,
                since=datetime.now(timezone.utc).isoformat(),
            )
        if self._log_limit.allow(("environment", environment, outcome.reason)):
            log.error(
                "apns: %s push disabled until restart: Apple answered %s %s "
                "for key %s / topic %s; devices registering for %s will be told "
                "push is unavailable and keep local notifications",
                environment,
                outcome.status,
                outcome.reason or "<no reason>",
                self._config.key_id,
                self._config.bundle_id,
                environment,
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
        """Send the first urgent state now, then coalesce per session.

        APNs collapse IDs replace an undelivered payload; this small server
        window also stops repeated *delivered* banners while an agent emits a
        burst of related state changes.
        """
        if not self._config.is_usable:
            return
        now = self._clock()
        immediate = []
        with self._pending_lock:
            if self._closed:
                return
            for credential in self._targets():
                mode = credential.notification_mode
                entry = self._pending.get(credential.id)
                cooldown_deadline = None
                if not transition.allowed(mode):
                    if entry:
                        entry[2].pop(transition.session_id, None)
                    self._recent.get(credential.id, {}).pop(transition.session_id, None)
                    continue
                if mode != "digest" and transition.needs_user:
                    recent = self._recent.setdefault(credential.id, {})
                    expired = [key for key, until in recent.items() if until <= now]
                    for key in expired:
                        recent.pop(key, None)
                    if transition.session_id not in recent:
                        # Promptly deliver the first actionable request. A
                        # later one for this session takes the debounce path.
                        if entry:
                            # A just-cleared/progress update must not arrive
                            # after the newer actionable request.
                            entry[2].pop(transition.session_id, None)
                        recent[transition.session_id] = now + self._input_batch_seconds
                        immediate.append((credential, transition))
                        continue
                    cooldown_deadline = recent[transition.session_id]
                delay = (
                    max(
                        0,
                        _next_digest_deadline(
                            now,
                            credential.notification_timezone,
                            credential.notification_hour,
                        )
                        - now,
                    )
                    if mode == "digest"
                    else (
                        max(0, cooldown_deadline - now)
                        if cooldown_deadline is not None
                        else self._batch_seconds
                    )
                )
                if entry is None or entry[1] != mode:
                    entry = [now + delay, mode, {}]
                    self._pending[credential.id] = entry
                else:
                    # An urgent state can advance a normal batch's deadline;
                    # subsequent updates can never postpone that deadline.
                    entry[0] = min(entry[0], now + delay)
                previous = entry[2].get(transition.session_id)
                entry[2][transition.session_id] = [
                    transition,
                    (previous[1] + 1) if previous else 1,
                ]
                if mode == "digest":
                    # Store before scheduling: a restart after this point
                    # can at worst re-send an APNs collapse-id, never drop it.
                    self._credentials.queue_digest(credential, transition, entry[0])
            self._schedule_locked()
        for credential, state in immediate:
            self._send_one(credential, state, self._needs_user_count(state))

    def _schedule_locked(self):
        if self._timer is not None:
            self._timer.cancel()
        if self._pending and not self._closed:
            deadline = min(entry[0] for entry in self._pending.values())
            self._timer = self._timer_factory(
                max(0, deadline - self._clock()), self._flush_async
            )
            self._timer.daemon = True
            self._timer.start()

    def _flush_async(self):
        try:
            self._pool.submit(self.flush_due)
        except RuntimeError:
            pass

    def flush_due(self):
        now = self._clock()
        with self._pending_lock:
            due = [
                (key, self._pending.pop(key))
                for key, entry in list(self._pending.items())
                if entry[0] <= now
            ]
            self._schedule_locked()
        targets = {credential.id: credential for credential in self._targets()}
        for key, (_, mode, states) in due:
            credential = targets.get(key)
            if credential is None:
                continue
            if credential.notification_mode != mode:
                # A preference change must never send an alert under the old mode.
                if mode == "digest":
                    self._credentials.complete_digest(credential.id, list(states))
                continue
            transitions = [
                replace(state, update_count=count)
                for state, count in states.values()
                if state.allowed(mode)
            ]
            if transitions:
                if mode == "digest":
                    for offset in range(0, len(transitions), 20):
                        batch = transitions[offset : offset + 20]
                        ids = [state.session_id for state in batch]
                        if self._send_one(
                            credential, batch, self._needs_user_count(transitions[0])
                        ):
                            # Deletion is after the accepted APNs response.
                            # If the process dies before this, the stable
                            # collapse-id makes retry safe for an offline app.
                            self._credentials.complete_digest(credential.id, ids)
                        else:
                            # Keep transient failures durable and retry with a
                            # bounded delay; APNs collapse IDs make a reply-
                            # lost retry idempotent for an offline device.
                            deadline = self._clock() + self._batch_seconds
                            self._credentials.defer_digest(credential.id, ids, deadline)
                            with self._pending_lock:
                                retry = self._pending.setdefault(
                                    credential.id, [deadline, "digest", {}]
                                )
                                retry[0] = min(retry[0], deadline)
                                for state in batch:
                                    retry[2][state.session_id] = [
                                        state,
                                        state.update_count,
                                    ]
                                self._schedule_locked()
                else:
                    # One APNs thread and collapse key per session. Do not
                    # turn a burst across sessions into a generic summary.
                    for state in transitions:
                        self._send_one(credential, state, self._needs_user_count(state))

    def _targets(self):
        targets = {}
        for credential in self._credentials.list_all():
            if (
                credential.scope == "device"
                and credential.is_active
                and credential.apns_token
                and credential.apns_environment in _HOSTS
                and self.environment_available(credential.apns_environment)
            ):
                # Old hubs allowed a re-paired phone's APNs endpoint to remain
                # on both credentials. Prefer the newest credential's current
                # notification mode until the next registration repairs it.
                key = (credential.apns_environment, credential.apns_token)
                existing = targets.get(key)
                if existing is None or (
                    credential.created_at,
                    credential.id,
                ) > (
                    existing.created_at,
                    existing.id,
                ):
                    targets[key] = credential
        yield from targets.values()

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
            if (
                transition.allowed(credential.notification_mode)
                and credential.notification_mode != "digest"
            ):
                self._send_one(credential, transition, badge)

    def _needs_user_count(
        self, transition: AwaitingTransition | None = None
    ) -> int | None:
        """Best-effort badge value; ``None`` leaves the app's badge alone."""
        if transition is not None and transition.badge_count is not None:
            return max(0, int(transition.badge_count))
        counter = getattr(self, "_badge_source", None)
        if counter is None:
            return None
        try:
            return int(counter())
        except Exception:  # noqa: BLE001
            return None

    def set_badge_source(self, counter) -> None:
        self._badge_source = counter

    def _payload(
        self, transition, badge: int | None, *, summary: bool = False
    ) -> bytes:
        states = transition if isinstance(transition, list) else [transition]
        ids = sorted(state.session_id for state in states)
        if len(states) == 1 and not summary:
            body = states[0].alert_body()
            if states[0].update_count > 1:
                body = _condense(
                    f"{states[0].update_count} updates · latest: {body}",
                    _BODY_MAX_CHARS,
                )
            alert = {"title": states[0].alert_title(), "body": body}
            subtitle = states[0].alert_subtitle()
            if subtitle:
                alert["subtitle"] = subtitle
            route = {"session_id": ids[0]}
            thread = ids[0]
        else:
            counts = {
                kind: sum(state.kind == kind for state in states)
                for kind in ("finished", "input", "failed", "progress")
            }
            parts = []
            for kind, verb in (
                ("finished", "finished"),
                ("input", "needs input"),
                ("failed", "failed"),
                ("progress", "updated"),
            ):
                count = counts[kind]
                if count:
                    if kind == "input" and count > 1:
                        verb = "need input"
                    parts.append(f"{count} session{'s' if count != 1 else ''} {verb}")
            alert = {"title": "Session updates", "body": ", ".join(parts)}
            route = {"session_ids": ids}
            thread = "session-summary"
        aps = {
            "alert": alert,
            "category": (
                "input" if any(state.needs_user for state in states) else states[0].kind
            ),
            "sound": "default",
            "thread-id": thread,
            "summary-arg": (
                states[0].project_name() if len(states) == 1 else "sessions"
            )
            or "Drover",
            "summary-arg-count": (
                states[0].update_count if len(states) == 1 else len(states)
            ),
            "interruption-level": (
                "time-sensitive"
                if any(state.needs_user for state in states)
                else "active"
            ),
        }
        if badge is not None:
            aps["badge"] = badge
        payload = json.dumps({"aps": aps, **route}, separators=(",", ":")).encode(
            "utf-8"
        )
        # A malformed/upstream title must never make APNs reject a useful
        # notification. The normal builders are far below this ceiling; this
        # is a final fail-safe for long metadata and digest IDs.
        if len(payload) > _APNS_MAX_BYTES:
            aps["alert"] = {"title": "Drover", "body": "Session updates available"}
            route = (
                {"session_ids": ids[:20]} if len(ids) > 1 else {"session_id": ids[0]}
            )
            payload = json.dumps({"aps": aps, **route}, separators=(",", ":")).encode(
                "utf-8"
            )
        return payload

    def _device_payload(self, credential, transition, badge):
        transition = self._consent_gated_transition(transition)
        is_digest = credential.notification_mode == "digest"
        payload = json.loads(self._payload(transition, badge, summary=is_digest))
        if is_digest:
            payload["aps"]["interruption-level"] = "passive"
        return json.dumps(payload, separators=(",", ":")).encode()

    def _consent_gated_transition(self, transition):
        """Apply the hub's durable content gate immediately before APNs I/O."""
        reader = self._content_consent_reader
        try:
            state = reader()
            enabled = state.get("enabled") is True
            epoch = state.get("epoch")
            allowed = enabled and type(epoch) is int and epoch > 0
        except Exception:  # noqa: BLE001 - consent reads must fail closed.
            allowed = False
        if allowed:
            return transition
        if isinstance(transition, list):
            return [replace(item, content_allowed=False) for item in transition]
        return replace(transition, content_allowed=False)

    def _send_one(self, credential, transition: AwaitingTransition, badge) -> None:
        host = _HOSTS.get(credential.apns_environment or "")
        # Re-checked per send: an earlier device in this same batch may just
        # have shown that the environment is unreachable.
        if host is None or not self.environment_available(credential.apns_environment):
            return False
        url = f"{host}/3/device/{credential.apns_token}"
        headers = {
            "authorization": f"bearer {self._auth.value()}",
            "apns-topic": self._config.bundle_id,
            "apns-push-type": "alert",
            "apns-priority": "10",
            # One session collapses onto itself across *all* state kinds: a
            # phone that was offline through Update, Needs input, and Finished
            # wakes to its latest state, not three stacked banners.
            "apns-collapse-id": (
                _collapse_id(transition.session_id)
                if not isinstance(transition, list)
                else (
                    _collapse_id(transition[0].session_id)
                    if len(transition) == 1
                    else hashlib.sha256(
                        "\0".join(
                            sorted(state.session_id for state in transition)
                        ).encode()
                    ).hexdigest()
                )
            ),
        }
        try:
            response = self._http().post(
                url,
                content=self._device_payload(credential, transition, badge),
                headers=headers,
            )
        except Exception as exc:  # noqa: BLE001
            # Timeouts and connection resets say nothing about the token.
            log.debug("apns: send failed for %s: %s", credential.id, exc)
            return False
        outcome = classify_response(response.status_code, response.text)
        self._handle_outcome(credential, outcome)
        return outcome.kind == DELIVERED

    def _handle_outcome(self, credential, outcome: DeliveryOutcome) -> None:
        environment = credential.apns_environment or ""
        if outcome.kind == DELIVERED:
            return
        if outcome.kind == ENVIRONMENT_REJECTED:
            # The registration itself is fine; our side cannot reach the
            # environment. Leaving it in place means push resumes for this
            # phone as soon as a fixed hub restarts, while the registration
            # route refuses new promises in the meantime.
            self._mark_environment_unavailable(environment, outcome)
            return
        if outcome.kind == DEVICE_REJECTED:
            # Dead token (app deleted, re-registered, or reported the wrong
            # environment). Clearing it alone is not enough: the app re-sends
            # the same token on its next launch, so the rejection is recorded
            # for the registration route to keep refusing it.
            log.warning(
                "apns: %s registration for %s rejected (%s %s); cleared so the "
                "app falls back to local notifications",
                environment,
                credential.id,
                outcome.status,
                outcome.reason,
            )
            try:
                self._credentials.mark_apns_registration_failed(
                    credential.id,
                    expected_token=credential.apns_token,
                    reason=outcome.reason,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("apns: could not clear registration: %s", exc)
            return
        if self._log_limit.allow(
            (outcome.kind, environment, outcome.status, outcome.reason)
        ):
            log.warning(
                "apns: %s %s %s for %s; registration kept",
                "transient" if outcome.kind == TRANSIENT else "rejected",
                outcome.status,
                outcome.reason or "<no reason>",
                credential.id,
            )

    def close(self) -> None:
        with self._pending_lock:
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
            self._pending.clear()
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


def push_available(environment: str | None = None) -> bool:
    """Return whether hub push is currently able to deliver alerts.

    This is intentionally read-only so request handlers can fail closed before
    accepting a device token. A stored token means the iOS app suppresses local
    notifications, so registration is safe only when a usable sender has been
    registered at startup. With ``environment``, it is also false once Apple
    has rejected the hub's key or topic for that APNs environment.
    """
    sender = _sender
    if sender is None:
        return False
    availability = getattr(sender, "is_available", None)
    try:
        if availability is not None and not (
            availability() if callable(availability) else availability
        ):
            return False
        # Test doubles and future sender types are registered only after their
        # own setup succeeds, so presence is the availability signal.
        per_environment = getattr(sender, "environment_available", None)
        if environment is not None and per_environment is not None:
            return bool(per_environment(environment))
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("push availability check failed: %s", exc)
        return False


def push_status() -> dict:
    """Push state for ``/readyz``: disabled, ok, or degraded per environment."""
    sender = _sender
    if sender is None:
        return {"state": "disabled"}
    status = getattr(sender, "status", None)
    if status is None:
        return {"state": "ok"}
    try:
        return status()
    except Exception as exc:  # noqa: BLE001
        log.debug("push status check failed: %s", exc)
        return {"state": "unknown"}


def configure(cfg, credentials, content_consent_reader=None) -> APNsSender | None:
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
    sender = APNsSender(
        config, credentials, content_consent_reader=content_consent_reader
    )
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
