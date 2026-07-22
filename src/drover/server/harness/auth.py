from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
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
    re.escape(key).replace("_", r"[-_]")
    for key in sorted(_SECRET_QUERY_KEYS, key=len, reverse=True)
)
_URL_RE = re.compile(r"https?://[^\s)'\"]+")
_USER_CODE_RE = re.compile(r"\b[A-Z0-9]{4}(?:-[A-Z0-9]{4})+\b")
_AUTHORIZATION_RE = re.compile(r"(?i)(\bauthorization\s*:\s*)[^\r\n,;]+")
_JSON_SECRET_RE = re.compile(
    rf'(?i)("(?:{_SECRET_KEY_PATTERN})"\s*:\s*)'
    r'(?:"(?:\\.|[^"\\])*"|[^,\s}\]]+)'
)
_COLON_SECRET_RE = re.compile(
    rf"(?i)(?<![\"\w])((?:x[-_])?(?:{_SECRET_KEY_PATTERN}))(\s*:\s*)"
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
    redacted = _AUTHORIZATION_RE.sub(r"\1<redacted>", text)
    redacted = _JSON_SECRET_RE.sub(r'\1"<redacted>"', redacted)
    redacted = _COLON_SECRET_RE.sub(r"\1\2<redacted>", redacted)
    for key in sorted(_SECRET_QUERY_KEYS, key=len, reverse=True):
        redacted = re.sub(
            rf"(?i)({re.escape(key).replace('_', r'[-_]')}=)[^&\s]+",
            rf"\1<redacted>",
            redacted,
        )
    redacted = re.sub(r"(?i)client_secret(?==)", "client_<redacted>", redacted)
    return redacted


@dataclass(frozen=True)
class CommandAuthAdapter:
    harness: str
    status_command: list[str]
    login_command: list[str]

    def status(self) -> HarnessAuthStatus:
        try:
            result = subprocess.run(
                self.status_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=5,
                check=False,
            )
        except FileNotFoundError:
            return HarnessAuthStatus(self.harness, "unavailable", detail="CLI not found")
        except subprocess.TimeoutExpired:
            return HarnessAuthStatus(self.harness, "unknown", detail="status timed out")

        output = redact_auth_text(result.stdout or "").strip()
        if self.harness == "claude-code":
            return _parse_claude_status(output, result.returncode)
        if self.harness == "codex":
            return _parse_codex_status(output, result.returncode)
        if self.harness == "gemini":
            return _parse_gemini_status(output, result.returncode)
        return HarnessAuthStatus(self.harness, "unknown", detail=output or None)

    def command(self) -> list[str]:
        return self.login_command


def _parse_claude_status(output: str, returncode: int) -> HarnessAuthStatus:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return HarnessAuthStatus("claude-code", "unknown", detail=output or None)
    if returncode != 0:
        return HarnessAuthStatus("claude-code", "unauthenticated", detail=output or None)
    if not isinstance(data, dict):
        return HarnessAuthStatus("claude-code", "unknown", detail=output or None)
    if data.get("loggedIn") is True:
        return HarnessAuthStatus(
            "claude-code",
            "authenticated",
            label=data.get("email"),
            detail=data.get("subscriptionType") or data.get("authMethod"),
        )
    if data.get("loggedIn") is False:
        return HarnessAuthStatus("claude-code", "unauthenticated", detail=output or None)
    return HarnessAuthStatus("claude-code", "unknown", detail=output or None)


def _parse_codex_status(output: str, returncode: int) -> HarnessAuthStatus:
    lowered = output.lower()
    if "not logged in" in lowered or "logged out" in lowered or returncode != 0:
        return HarnessAuthStatus("codex", "unauthenticated", detail=output or None)
    if "logged in" in lowered:
        return HarnessAuthStatus("codex", "authenticated", detail=output or None)
    return HarnessAuthStatus("codex", "unknown", detail=output or None)


def _parse_gemini_status(output: str, returncode: int) -> HarnessAuthStatus:
    if os.environ.get("GEMINI_API_KEY"):
        return HarnessAuthStatus("gemini", "unknown", detail="GEMINI_API_KEY set")
    settings = Path.home() / ".gemini/settings.json"
    accounts = Path.home() / ".gemini/google_accounts.json"
    if settings.exists() or accounts.exists():
        return HarnessAuthStatus("gemini", "unknown", detail="Gemini config present")
    return HarnessAuthStatus("gemini", "unknown", detail=output or None)


def default_auth_adapters() -> dict[str, HarnessAuthAdapter]:
    adapters: dict[str, HarnessAuthAdapter] = {}
    claude = shutil.which("claude")
    if claude:
        adapters["claude-code"] = CommandAuthAdapter(
            "claude-code",
            [claude, "auth", "status", "--json"],
            [claude, "auth", "login"],
        )
    codex = shutil.which("codex")
    if codex:
        adapters["codex"] = CommandAuthAdapter(
            "codex",
            [codex, "login", "status"],
            [codex, "login", "--device-auth"],
        )
    gemini = shutil.which("gemini")
    if gemini:
        adapters["gemini"] = StaticAuthAdapter(
            "gemini",
            status_value=_parse_gemini_status("", 0),
        )
    return adapters


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
    cleanup_scheduled: bool = False

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
        self._flows_by_id: dict[str, _AuthFlow] = {}
        self._active_flow_ids: dict[str, str] = {}
        self._lock = threading.Lock()

    def status(self, harness: str) -> dict[str, Any]:
        with self._lock:
            self._discard_expired_flows()
        return self._adapter(harness).status().as_json()

    def start(self, harness: str) -> dict[str, Any]:
        with self._lock:
            self._discard_expired_flows()
            existing_id = self._active_flow_ids.get(harness)
            existing = self._flows_by_id.get(existing_id) if existing_id else None
            if existing is not None and not self._is_terminal(existing):
                return existing.snapshot().as_json()

            process = subprocess.Popen(
                self._adapter(harness).command(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            )
            flow = _AuthFlow(
                flow_id=str(uuid4()),
                harness=harness,
                process=process,
                started_at=time.time(),
                timeout_s=self._timeout_s,
            )
            self._flows_by_id[flow.flow_id] = flow
            self._active_flow_ids[harness] = flow.flow_id

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
                self._schedule_discard_locked(flow)
        return flow.snapshot().as_json()

    def _adapter(self, harness: str) -> HarnessAuthAdapter:
        try:
            return self._adapters[harness]
        except KeyError as exc:
            raise KeyError(f"unknown harness: {harness}") from exc

    def _flow(self, harness: str, flow_id: str) -> _AuthFlow:
        with self._lock:
            self._discard_expired_flows()
            flow = self._flows_by_id.get(flow_id)
        if flow is None or flow.harness != harness:
            raise KeyError(f"unknown auth flow: {harness}/{flow_id}")
        return flow

    @staticmethod
    def _is_terminal(flow: _AuthFlow) -> bool:
        with flow.lock:
            return flow.state in _TERMINAL_FLOW_STATES

    def _discard_expired_flows(self) -> None:
        now = time.time()
        for flow_id, flow in list(self._flows_by_id.items()):
            with flow.lock:
                completed_at = flow.completed_at
                should_discard = (
                    completed_at is not None
                    and now - completed_at >= self._retention_s
                )
            if should_discard:
                del self._flows_by_id[flow_id]
                if self._active_flow_ids.get(flow.harness) == flow_id:
                    del self._active_flow_ids[flow.harness]

    def _schedule_discard_locked(self, flow: _AuthFlow) -> None:
        if flow.cleanup_scheduled:
            return
        flow.cleanup_scheduled = True
        threading.Thread(
            target=self._discard_flow_after_retention,
            args=(flow.flow_id,),
            daemon=True,
        ).start()

    def _discard_flow_after_retention(self, flow_id: str) -> None:
        time.sleep(max(self._retention_s, 0))
        with self._lock:
            flow = self._flows_by_id.get(flow_id)
            if flow is None:
                return
            with flow.lock:
                if flow.completed_at is None:
                    return
                if time.time() - flow.completed_at < self._retention_s:
                    return
                del self._flows_by_id[flow_id]
                if self._active_flow_ids.get(flow.harness) == flow_id:
                    del self._active_flow_ids[flow.harness]

    def _consume_output(self, flow: _AuthFlow) -> None:
        try:
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
        except Exception:
            with flow.lock:
                if flow.state in _TERMINAL_FLOW_STATES:
                    return
                flow.state = "failed"
                flow.completed_at = time.time()
                flow.last_error = "authentication output read failed"
                if flow.process.poll() is None:
                    flow.process.terminate()
                self._schedule_discard_locked(flow)
            return

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
            self._schedule_discard_locked(flow)

    def _expire_flow(self, flow: _AuthFlow) -> None:
        try:
            remaining_s = max(flow.started_at + flow.timeout_s - time.time(), 0)
            flow.process.wait(timeout=remaining_s)
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
            self._schedule_discard_locked(flow)
