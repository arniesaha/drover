"""A thin client over Drover's public harness API.

Decision D4: the driver speaks to Drover the way any other client does. It
imports nothing from ``drover.server`` on purpose -- if Phase 0 needs a
capability the API does not expose, that is a finding about the API, and
reaching past it into the server would hide it.

Correction to the design of record: §6.2 lists
``POST /harness/sessions/{id}/messages`` as the way to send a turn. It is not.
``/messages`` is a GET that reads the transcript; a turn is
``POST /harness/sessions/{id}/turns``. Verified against ``server/web/app.py``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional

#: Statuses that describe the request rather than the moment. Retrying one of
#: these is not patience, it is repetition.
_PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 405, 501})


class HarnessApiError(RuntimeError):
    """A call the driver cannot sensibly continue past.

    ``fatal`` marks the ones that will fail identically next time -- a bad
    token, an unknown host. The distinction exists because the first run of
    this driver spent its entire ten-iteration budget on one 401, writing ten
    identical rows. A loop that cannot tell a transient failure from a
    permanent one does not retry, it just repeats.
    """

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


@dataclass(frozen=True)
class HarnessApi:
    base_url: str
    token: str
    timeout_s: float = 30.0
    #: Creating a structured session spawns a CLI behind the request, so this
    #: cannot share the ordinary timeout. `tests/test_structured_e2e.py` says
    #: the same thing about its own client. A 30s ceiling timed out twice on a
    #: loaded hub while the session was created and the agent ran anyway --
    #: which is worse than a slow create, because the driver then had no id for
    #: a session that was already working.
    create_timeout_s: float = 300.0

    def _call(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> tuple[int, Any]:
        url = f"{self.base_url.rstrip('/')}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        timeout = (
            self.create_timeout_s if path.endswith("/sessions") else self.timeout_s
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode()
                return response.status, json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            try:
                return exc.code, json.loads(body) if body else None
            except json.JSONDecodeError:
                return exc.code, {"error": body[:400]}
        except OSError as exc:
            # Deliberately not fatal, and deliberately not "it did not happen".
            # A timed-out POST may well have been served; see `orphan_risk`.
            raise HarnessApiError(f"{method} {path}: {exc}") from exc

    def create_session(
        self,
        host_id: str,
        *,
        harness: str,
        cwd: str,
        command: Optional[list[str]] = None,
        prompt: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"harness": harness, "mode": "structured", "cwd": cwd}
        if command is not None:
            body["command"] = command
        if prompt is not None:
            body["prompt"] = prompt
        status, payload = self._call("POST", f"/harness/hosts/{host_id}/sessions", body)
        if status not in (200, 201) or not isinstance(payload, dict):
            raise HarnessApiError(
                f"create_session returned {status}: {payload}",
                fatal=status in _PERMANENT_STATUSES,
            )
        return payload

    def send_turn(self, session_id: str, text: str) -> dict[str, Any]:
        status, payload = self._call(
            "POST", f"/harness/sessions/{session_id}/turns", {"text": text}
        )
        if status not in (200, 201, 202) or not isinstance(payload, dict):
            raise HarnessApiError(f"send_turn returned {status}: {payload}")
        return payload

    def session(self, session_id: str) -> dict[str, Any]:
        """The session row, unwrapped.

        ``GET /harness/sessions/{id}`` answers with an envelope --
        ``{"session", "host", "events", "native_transcript"}`` -- not with the
        session itself. The list endpoint returns the rows flat, which is what
        made the difference easy to miss: the driver polled the envelope for
        ``status`` and ``awaiting``, got None for both on every poll, and sat
        there until its 30-minute turn timeout while the agent had long since
        finished. Found on the first live run, and only there: the fake in the
        tests returned the flat shape this code assumed, so the tests
        confirmed the assumption rather than the API.
        """
        status, payload = self._call("GET", f"/harness/sessions/{session_id}")
        if status != 200 or not isinstance(payload, dict):
            raise HarnessApiError(
                f"session returned {status}: {payload}",
                fatal=status in _PERMANENT_STATUSES,
            )
        session = payload.get("session")
        return session if isinstance(session, dict) else payload

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        status, payload = self._call("GET", f"/harness/sessions/{session_id}/messages")
        if status != 200:
            raise HarnessApiError(f"messages returned {status}: {payload}")
        if isinstance(payload, dict):
            return list(payload.get("messages") or [])
        return list(payload or [])

    def terminate(self, session_id: str) -> None:
        # Best effort by design: a session that has already exited is not an
        # error the loop should stop for, and leaving one running is worse than
        # a noisy log line.
        try:
            self._call("POST", f"/harness/sessions/{session_id}/terminate")
        except HarnessApiError:
            pass
