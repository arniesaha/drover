"""Durable, bounded work-item event fanout.

The timeline is the source of truth.  Its UUID is also the public event id,
which lets receivers safely deduplicate retries and lets the pull stream avoid
maintaining a second identity scheme.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

log = logging.getLogger(__name__)

EVENT_TYPE = "work_item.event"
EVENT_VERSION = 1
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class _NoRedirect(HTTPRedirectHandler):
    # A validated subscription must not relay its signed payload to an
    # arbitrary redirect target.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_DELIVERY_OPENER = build_opener(_NoRedirect())


def _open_delivery(request, timeout):
    return _DELIVERY_OPENER.open(request, timeout=timeout)


def validate_subscription_url(url: object, allowed_hosts: object) -> str:
    """Allow HTTPS everywhere and explicit local/tailnet HTTP endpoints only."""
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("subscription url is required")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("subscription url must be an absolute HTTP URL")
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host in _LOOPBACK_HOSTS
    configured = {str(value).lower().rstrip(".") for value in allowed_hosts or []}
    if parsed.scheme == "https" or is_loopback:
        return url
    if host.endswith(".ts.net") and host in configured:
        return url
    raise ValueError("subscription url must use HTTPS")


def _filter_kinds(value: object) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(kind, str) or not kind or len(kind) > 64 for kind in value
    ):
        raise ValueError("event_kinds must be a list of event kinds")
    if len(value) > 64 or len(set(value)) != len(value):
        raise ValueError("event_kinds must be unique and bounded")
    return value


def append_timeline_event(
    con,
    *,
    workspace_id: str,
    item_id: str,
    kind: str,
    stage: str | None,
    gate: str | None,
    detail: str | None,
    from_stage: str | None,
) -> str:
    """Append one timeline fact, event, and matching delivery intents atomically."""
    timeline_id = str(uuid4())
    timeline = con.execute(
        """INSERT INTO work_item_timeline (id, work_item_id, kind, stage, gate, detail)
           VALUES (?, ?, ?, ?, ?, ?) RETURNING created_at""",
        [timeline_id, item_id, kind, stage, gate, detail],
    ).fetchone()
    item = con.execute(
        """SELECT repo, session_id, pr, next_action FROM work_items
           WHERE id = ? AND workspace_id = ?""",
        [item_id, workspace_id],
    ).fetchone()
    if item is None:
        raise KeyError(item_id)
    cursor = con.execute(
        """INSERT INTO work_item_event_cursors (workspace_id, cursor) VALUES (?, 1)
           ON CONFLICT (workspace_id) DO UPDATE
             SET cursor = work_item_event_cursors.cursor + 1
           RETURNING cursor""",
        [workspace_id],
    ).fetchone()[0]
    con.execute(
        """INSERT INTO work_item_events
           (event_id, workspace_id, cursor, item_id, kind, from_stage, to_stage,
            gate, next_action, session_id, pr, occurred_at, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            timeline_id,
            workspace_id,
            cursor,
            item_id,
            kind,
            from_stage,
            stage,
            gate,
            item[3],
            item[1],
            item[2],
            timeline[0],
            detail,
        ],
    )
    subscriptions = con.execute(
        """SELECT id, event_kinds_json, repo FROM work_item_subscriptions
           WHERE workspace_id = ?""",
        [workspace_id],
    ).fetchall()
    for subscription_id, kinds_json, repo_filter in subscriptions:
        try:
            kinds = json.loads(kinds_json)
        except (TypeError, json.JSONDecodeError):
            continue  # invalid old data must never stop an item transition
        if (kinds and kind not in kinds) or (repo_filter and repo_filter != item[0]):
            continue
        con.execute(
            """INSERT INTO work_item_event_deliveries (subscription_id, event_id)
               VALUES (?, ?) ON CONFLICT DO NOTHING""",
            [subscription_id, timeline_id],
        )
    return timeline_id


def event_payload(row: dict) -> dict:
    return {
        "v": EVENT_VERSION,
        "type": EVENT_TYPE,
        "event_id": row["event_id"],
        "cursor": row["cursor"],
        "item_id": row["item_id"],
        "kind": row["kind"],
        "from_stage": row["from_stage"],
        "to_stage": row["to_stage"],
        "gate": row["gate"],
        "next": row["next_action"],
        "session_id": row["session_id"],
        "pr": row["pr"],
        "occurred_at": row["occurred_at"],
        "detail": row.get("detail"),
    }


class WorkItemEventWorker:
    """Small polling worker; the database is the bounded durable queue."""

    def __init__(self, store, config):
        self.store = store
        self.config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._in_flight = threading.BoundedSemaphore(config.max_in_flight)
        self._guard = threading.Lock()
        self._health = {"sent": 0, "failed": 0, "dead_lettered": 0, "in_flight": 0}

    def start(self, shutdown_event: threading.Event | None = None) -> None:
        if self._thread is not None:
            return

        def run() -> None:
            while not self._stop.is_set() and not (
                shutdown_event and shutdown_event.is_set()
            ):
                try:
                    self.process_once()
                except Exception:  # noqa: BLE001 - one failed pass is retryable
                    log.exception("work-item event delivery pass failed")
                self._stop.wait(0.25)

        self._thread = threading.Thread(
            target=run, name="work-item-events", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def health(self) -> dict:
        with self._guard:
            result = dict(self._health)
        result.update(self.store.work_item_delivery_health())
        result["max_in_flight"] = self.config.max_in_flight
        return result

    def process_once(self) -> int:
        claimed = self.store.claim_work_item_deliveries(self.config.batch_size)
        if not claimed:
            return 0
        with ThreadPoolExecutor(max_workers=self.config.max_in_flight) as executor:
            list(executor.map(self._deliver, claimed))
        return len(claimed)

    def _deliver(self, row: dict) -> None:
        with self._in_flight:
            with self._guard:
                self._health["in_flight"] += 1
            try:
                payload = event_payload(row)
                body = json.dumps(payload, separators=(",", ":"), default=str).encode()
                signature = hmac.new(
                    row["hmac_secret"].encode(), body, hashlib.sha256
                ).hexdigest()
                request = Request(
                    row["url"],
                    data=body,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Drover-Signature": "sha256=" + signature,
                        "X-Drover-Event-Id": row["event_id"],
                    },
                )
                try:
                    with _open_delivery(
                        request, self.config.timeout_seconds
                    ) as response:
                        status = int(response.status)
                except Exception as exc:  # urllib's HTTPError has a code too
                    status = int(getattr(exc, "code", 0) or 0)
                delivered = 200 <= status < 300
                outcome = self.store.complete_work_item_delivery(
                    row,
                    status=status,
                    delivered=delivered,
                    max_attempts=self.config.max_attempts,
                )
                with self._guard:
                    if delivered:
                        self._health["sent"] += 1
                    else:
                        self._health["failed"] += 1
                        if outcome == "dead_lettered":
                            self._health["dead_lettered"] += 1
            finally:
                with self._guard:
                    self._health["in_flight"] -= 1
