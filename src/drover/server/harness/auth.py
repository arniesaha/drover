from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import subprocess
import threading
import time
from typing import Any, Protocol
from uuid import uuid4


_SECRET_QUERY_KEYS = {
    "token",
    "code",
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "api_key",
    "key",
    "secret",
}
_SECRET_KEY_PATTERN = "|".join(
    re.escape(key) for key in sorted(_SECRET_QUERY_KEYS, key=len, reverse=True)
)
_URL_RE = re.compile(r"https?://[^\s)'\"]+")
_USER_CODE_RE = re.compile(r"\b[A-Z0-9]{4}(?:-[A-Z0-9]{4})+\b")
_BEARER_RE = re.compile(r"(?i)(\bauthorization\s*:\s*bearer\s+)[^\s,;]+")
_JSON_SECRET_RE = re.compile(
    rf'(?i)("(?:{_SECRET_KEY_PATTERN})"\s*:\s*)"[^"]*"'
)
_COLON_SECRET_RE = re.compile(
    rf"(?i)(?<![\"\w])({_SECRET_KEY_PATTERN})(\s*:\s*)"
    r'(?:"[^"]*"|\'[^\']*\'|[^\s,}]+)'
)
_TERMINAL_FLOW_STATES = {"authenticated", "failed", "expired", "cancelled"}


@dataclass(frozen=True)
class HarnessAuthStatus:
    harness: str
    state: str
    label: str | None = None
    detail: str | None = None

    def as_json(self, *, host_id: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "harness": self.harness,
            "state": self.state,
            "label": self.label,
            "detail": self.detail,
        }
        if host_id is not None:
            data["host_id"] = host_id
        return data


@dataclass(frozen=True)
class HarnessAuthFlowSnapshot:
    flow_id: str
    harness: str
    state: str
    login_url: str | None = None
    device_code: str | None = None
    user_code: str | None = None
    message: str | None = None
    expires_at: str | None = None
    last_error: str | None = None

    def as_json(self, *, host_id: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "flow_id": self.flow_id,
            "harness": self.harness,
            "state": self.state,
            "login_url": self.login_url,
            "device_code": self.device_code,
            "user_code": self.user_code,
            "message": self.message,
            "expires_at": self.expires_at,
            "last_error": self.last_error,
        }
        if host_id is not None:
            data["host_id"] = host_id
        return data


class HarnessAuthAdapter(Protocol):
    harness: str

    def status(self) -> HarnessAuthStatus: ...

    def command(self) -> list[str]: ...


@dataclass(frozen=True)
class StaticAuthAdapter:
    harness: str
    status_value: HarnessAuthStatus | None = None
    start_command: list[str] | None = None

    def status(self) -> HarnessAuthStatus:
        return self.status_value or HarnessAuthStatus(
            self.harness,
            "unavailable",
            detail=f"auth is not supported for {self.harness}",
        )

    def command(self) -> list[str]:
        if not self.start_command:
            raise RuntimeError(f"auth is not supported for {self.harness}")
        return self.start_command


def redact_auth_text(text: str) -> str:
    redacted = _BEARER_RE.sub(r"\1<redacted>", text)
    redacted = _JSON_SECRET_RE.sub(r'\1"<redacted>"', redacted)
    redacted = _COLON_SECRET_RE.sub(r"\1\2<redacted>", redacted)
    for key in sorted(_SECRET_QUERY_KEYS, key=len, reverse=True):
        redacted = re.sub(
            rf"(?i)({re.escape(key)}=)[^&\s]+",
            rf"\1<redacted>",
            redacted,
        )
    redacted = re.sub(r"(?i)client_secret(?==)", "client_<redacted>", redacted)
    return redacted


@dataclass
class _AuthFlow:
    flow_id: str
    harness: str
    process: subprocess.Popen[str]
    started_at: float
    timeout_s: float
    state: str = "starting"
    login_url: str | None = None
    user_code: str | None = None
    device_code: str | None = None
    message: str | None = None
    last_error: str | None = None
    output_tail: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    completed_at: float | None = None

    def snapshot(self) -> HarnessAuthFlowSnapshot:
        with self.lock:
            expires_at = datetime.fromtimestamp(
                self.started_at + self.timeout_s,
                timezone.utc,
            ).isoformat()
            return HarnessAuthFlowSnapshot(
                flow_id=self.flow_id,
                harness=self.harness,
                state=self.state,
                login_url=self.login_url,
                device_code=self.device_code,
                user_code=self.user_code,
                message=self.message,
                expires_at=expires_at,
                last_error=self.last_error,
            )


class AuthFlowManager:
    def __init__(
        self,
        adapters: dict[str, HarnessAuthAdapter],
        *,
        timeout_s: float = 600,
        retention_s: float = 600,
    ) -> None:
        self._adapters = adapters
        self._timeout_s = timeout_s
        self._retention_s = retention_s
        self._flows: dict[str, _AuthFlow] = {}
        self._lock = threading.Lock()

    def status(self, harness: str) -> dict[str, Any]:
        with self._lock:
            self._discard_expired_flows()
        return self._adapter(harness).status().as_json()

    def start(self, harness: str) -> dict[str, Any]:
        with self._lock:
            self._discard_expired_flows()
            existing = self._flows.get(harness)
            if existing is not None and not self._is_terminal(existing):
                return existing.snapshot().as_json()

            process = subprocess.Popen(
                self._adapter(harness).command(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            flow = _AuthFlow(
                flow_id=str(uuid4()),
                harness=harness,
                process=process,
                started_at=time.time(),
                timeout_s=self._timeout_s,
            )
            self._flows[harness] = flow

        threading.Thread(target=self._consume_output, args=(flow,), daemon=True).start()
        threading.Thread(target=self._expire_flow, args=(flow,), daemon=True).start()
        return flow.snapshot().as_json()

    def snapshot(self, harness: str, flow_id: str) -> dict[str, Any]:
        return self._flow(harness, flow_id).snapshot().as_json()

    def cancel(self, harness: str, flow_id: str) -> dict[str, Any]:
        flow = self._flow(harness, flow_id)
        with flow.lock:
            if flow.state not in _TERMINAL_FLOW_STATES:
                flow.state = "cancelled"
                flow.completed_at = time.time()
                flow.message = "authentication flow cancelled"
                if flow.process.poll() is None:
                    flow.process.terminate()
        return flow.snapshot().as_json()

    def _adapter(self, harness: str) -> HarnessAuthAdapter:
        try:
            return self._adapters[harness]
        except KeyError as exc:
            raise KeyError(f"unknown harness: {harness}") from exc

    def _flow(self, harness: str, flow_id: str) -> _AuthFlow:
        with self._lock:
            self._discard_expired_flows()
            flow = self._flows.get(harness)
        if flow is None or flow.flow_id != flow_id:
            raise KeyError(f"unknown auth flow: {harness}/{flow_id}")
        return flow

    @staticmethod
    def _is_terminal(flow: _AuthFlow) -> bool:
        with flow.lock:
            return flow.state in _TERMINAL_FLOW_STATES

    def _discard_expired_flows(self) -> None:
        now = time.time()
        for harness, flow in list(self._flows.items()):
            with flow.lock:
                completed_at = flow.completed_at
                should_discard = (
                    completed_at is not None
                    and now - completed_at >= self._retention_s
                )
            if should_discard:
                del self._flows[harness]

    def _consume_output(self, flow: _AuthFlow) -> None:
        assert flow.process.stdout is not None
        for raw_line in flow.process.stdout:
            line = redact_auth_text(raw_line.rstrip())
            with flow.lock:
                if flow.state in _TERMINAL_FLOW_STATES:
                    continue
                flow.output_tail.append(line)
                del flow.output_tail[:-20]
                flow.message = line
                url = _URL_RE.search(line)
                if url is not None and flow.login_url is None:
                    flow.login_url = url.group(0)
                user_code = _USER_CODE_RE.search(line)
                if user_code is not None and flow.user_code is None:
                    flow.user_code = user_code.group(0)
                flow.state = "waiting_for_user"

        return_code = flow.process.wait()
        with flow.lock:
            if flow.state in _TERMINAL_FLOW_STATES:
                return
            flow.completed_at = time.time()
            if return_code == 0:
                flow.state = "authenticated"
            else:
                flow.state = "failed"
                flow.last_error = f"authentication process exited with code {return_code}"

    def _expire_flow(self, flow: _AuthFlow) -> None:
        try:
            flow.process.wait(timeout=flow.timeout_s)
            return
        except subprocess.TimeoutExpired:
            pass

        with flow.lock:
            if flow.state in _TERMINAL_FLOW_STATES:
                return
            flow.state = "expired"
            flow.completed_at = time.time()
            flow.last_error = "authentication flow expired"
            if flow.process.poll() is None:
                flow.process.terminate()
