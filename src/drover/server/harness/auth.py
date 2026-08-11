from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import threading
import time
from typing import Any, Protocol, Sequence
from uuid import uuid4

_SECRET_QUERY_KEYS = {
    "authorization",
    "bearer",
    "cookie",
    "token",
    "code",
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "api_key",
    "credential",
    "credentials",
    "jwt",
    "key",
    "password",
    "passwd",
    "secret",
    "session_token",
    "set_cookie",
}
_SECRET_KEY_PATTERN = "|".join(
    re.escape(key).replace("_", r"[-_]")
    for key in sorted(_SECRET_QUERY_KEYS, key=len, reverse=True)
)
_SPACE_SECRET_KEYS = _SECRET_QUERY_KEYS - {"code"}
_SPACE_SECRET_KEY_PATTERN = "|".join(
    re.escape(key).replace("_", r"[-_]")
    for key in sorted(_SPACE_SECRET_KEYS, key=len, reverse=True)
)
_SECRET_KEY_NAME_RE = (
    r"[A-Z0-9][A-Z0-9_-]*"
    r"(?:AUTHORIZATION|BEARER|COOKIE|TOKEN|CODE|ACCESS[-_]TOKEN|REFRESH[-_]TOKEN|"
    r"ID[-_]TOKEN|CLIENT[-_]SECRET|API[-_]KEY|CREDENTIALS?|JWT|KEY|PASSWD|PASSWORD|"
    r"SECRET|SESSION[-_]TOKEN|SET[-_]COOKIE)"
    r"[A-Z0-9_-]*"
)
_URL_RE = re.compile(r"https?://[^\s)'\"]+")
_USER_CODE_RE = re.compile(r"\b[A-Z0-9]{4}(?:-[A-Z0-9]{4})+\b")
_USER_CODE_CONTEXT_RE = re.compile(
    r"(?i)\b(?:pairing|user|device)\s+code\s*:\s*([A-Z0-9]{4}(?:-[A-Z0-9]{4})+)\b"
)
_AUTHORIZATION_RE = re.compile(r"(?i)(\bauthorization\s*:\s*)[^\r\n,;]+")
_BEARER_TOKEN_RE = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=:-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_JSON_SECRET_RE = re.compile(
    rf'(?i)("(?:{_SECRET_KEY_PATTERN}|{_SECRET_KEY_NAME_RE})"\s*:\s*)'
    r'(?:"(?:\\.|[^"\\])*"|[^,\s}\]]+)'
)
_COLON_SECRET_RE = re.compile(
    rf"(?i)(?<![\"\w])((?:x[-_])?(?:{_SECRET_KEY_PATTERN}|{_SECRET_KEY_NAME_RE}))(\s*:\s*)"
    r'(?:"[^"]*"|\'[^\']*\'|[^\s,}]+)'
)
_SPACED_ASSIGN_SECRET_RE = re.compile(
    rf"(?i)(?<![\"\w])((?:x[-_])?(?:{_SECRET_KEY_PATTERN}|{_SECRET_KEY_NAME_RE}))(\s*=\s*)"
    r'(?:"[^"]*"|\'[^\']*\'|[^&\s,}]+)'
)
_ENV_SPACE_SECRET_RE = re.compile(
    rf"(?<![\"\w])((?:X[-_])?{_SECRET_KEY_NAME_RE})([ \t]+)"
    r'(?:"[^"]*"|\'[^\']*\'|[^\s,;}]+)'
)
_KEY_SPACE_SECRET_RE = re.compile(
    rf"(?i)(?<![\"\w])((?:x[-_])?(?:{_SPACE_SECRET_KEY_PATTERN}))([ \t]+)"
    r'(?:"[^"]*"|\'[^\']*\'|[^\s,;}]+)'
)
_PHRASE_SPACE_SECRET_RE = re.compile(
    r"(?i)(?<![\"\w])((?:access|refresh|id|oauth|session|authorization)\s+token|"
    r"api\s+key|client\s+secret|bearer\s+token|credentials?|password|passwd)"
    r"([ \t]+)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_PROVIDER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-(?:(?:ant-(?:api|oat)\d*|proj|svcacct)-[A-Za-z0-9_-]{16,}|[A-Za-z0-9_-]{20,})|"
    r"AIza[A-Za-z0-9_-]{20,}|ya29\.[A-Za-z0-9._-]{10,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r")(?![A-Za-z0-9_-])"
)
_URL_USERINFO_RE = re.compile(r"(?i)(https?://)[^/\s?#@]+@")
_TERMINAL_FLOW_STATES = {"authenticated", "failed", "expired", "cancelled"}
_PROCESS_STOP_TIMEOUT_S = 0.2


class AuthFlowLaunchError(RuntimeError):
    """Authentication CLI could not be started on this host."""


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
    redacted = _URL_USERINFO_RE.sub(r"\1<redacted>@", text)
    redacted = _AUTHORIZATION_RE.sub(r"\1<redacted>", redacted)
    redacted = _BEARER_TOKEN_RE.sub(r"\1<redacted>", redacted)
    redacted = _JWT_RE.sub("<redacted>", redacted)
    redacted = _PROVIDER_TOKEN_RE.sub("<redacted>", redacted)
    redacted = _JSON_SECRET_RE.sub(r'\1"<redacted>"', redacted)
    redacted = _COLON_SECRET_RE.sub(r"\1\2<redacted>", redacted)
    redacted = _SPACED_ASSIGN_SECRET_RE.sub(r"\1\2<redacted>", redacted)
    redacted = _ENV_SPACE_SECRET_RE.sub(r"\1\2<redacted>", redacted)
    redacted = _KEY_SPACE_SECRET_RE.sub(r"\1\2<redacted>", redacted)
    redacted = _PHRASE_SPACE_SECRET_RE.sub(r"\1\2<redacted>", redacted)
    for key in sorted(_SECRET_QUERY_KEYS, key=len, reverse=True):
        redacted = re.sub(
            rf"(?i)({re.escape(key).replace('_', r'[-_]')}=)[^&\s]+",
            rf"\1<redacted>",
            redacted,
        )
    redacted = re.sub(r"(?i)client_secret(?==)", "client_<redacted>", redacted)
    return redacted


def _extract_safe_user_code(text: str) -> str | None:
    match = _USER_CODE_CONTEXT_RE.search(text)
    return match.group(1) if match is not None else None


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
        except OSError:
            return HarnessAuthStatus(
                self.harness, "unavailable", detail="CLI not runnable"
            )
        except subprocess.TimeoutExpired:
            return HarnessAuthStatus(self.harness, "unknown", detail="status timed out")

        output = redact_auth_text(result.stdout or "").strip()
        if self.harness == "claude-code":
            return _parse_claude_status(output, result.returncode)
        if self.harness == "codex":
            return _parse_codex_status(output, result.returncode)
        if self.harness == "agy":
            return _parse_agy_status(output, result.returncode)
        return HarnessAuthStatus(self.harness, "unknown", detail=output or None)

    def command(self) -> list[str]:
        return self.login_command


def _parse_claude_status(output: str, returncode: int) -> HarnessAuthStatus:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return HarnessAuthStatus("claude-code", "unknown", detail=output or None)
    if returncode != 0:
        return HarnessAuthStatus(
            "claude-code", "unauthenticated", detail=output or None
        )
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
        return HarnessAuthStatus(
            "claude-code", "unauthenticated", detail=output or None
        )
    return HarnessAuthStatus("claude-code", "unknown", detail=output or None)


def _parse_codex_status(output: str, returncode: int) -> HarnessAuthStatus:
    lowered = output.lower()
    if "not logged in" in lowered or "logged out" in lowered or returncode != 0:
        return HarnessAuthStatus("codex", "unauthenticated", detail=output or None)
    if "logged in" in lowered:
        return HarnessAuthStatus("codex", "authenticated", detail=output or None)
    return HarnessAuthStatus("codex", "unknown", detail=output or None)


def _parse_agy_status(
    output: str, returncode: int, *, home: Path | None = None
) -> HarnessAuthStatus:
    """Read sign-in from agy's own state, not from ``agy --version``.

    The probe command is ``--version``, which exits 0 whenever the binary is
    installed -- signed in or not -- so its return code says nothing about
    authentication. agy keeps its credentials in ``~/.gemini``: an account
    address in ``google_accounts.json`` and the OAuth blob beside it. A
    missing address is reported as ``unknown`` rather than
    ``unauthenticated``, because agy may store identity somewhere this has
    not seen.
    """
    root = home or Path.home()
    if returncode != 0:
        return HarnessAuthStatus("agy", "unavailable", detail=output or None)
    account_label = None
    try:
        raw = json.loads((root / ".gemini/google_accounts.json").read_text())
        if isinstance(raw, dict):
            if isinstance(raw.get("active"), str) and raw["active"].strip():
                account_label = raw["active"].strip()
            elif (
                isinstance(raw.get("old"), list)
                and raw["old"]
                and isinstance(raw["old"][0], str)
                and raw["old"][0].strip()
            ):
                account_label = raw["old"][0].strip()
    except (OSError, ValueError):
        account_label = None
    if account_label:
        return HarnessAuthStatus(
            "agy", "authenticated", label=account_label, detail="Antigravity CLI"
        )
    if (root / ".gemini/oauth_creds.json").exists() or (
        root / ".gemini/antigravity-cli/antigravity-oauth-token"
    ).exists():
        return HarnessAuthStatus("agy", "authenticated", detail="Antigravity CLI")
    return HarnessAuthStatus("agy", "unknown", detail=output or None)


def default_login_shell() -> str:
    for candidate in ("/bin/zsh", "/bin/bash", "/bin/sh"):
        if Path(candidate).exists():
            return candidate
    return "/bin/sh"


def resolve_executable(binary: str, *, login_shell: str) -> str | None:
    if binary.startswith("/") and Path(binary).exists():
        return binary
    try:
        result = subprocess.run(
            [login_shell, "-lc", f"command -v {shlex.quote(binary)}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    executable = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if result.returncode != 0 or not executable:
        return _resolve_known_versioned_cli(binary)
    return executable


def _resolve_known_versioned_cli(binary: str) -> str | None:
    if binary == "claude":
        versions_dir = Path.home() / ".local/share/claude/versions"
        if not versions_dir.is_dir():
            return None
        candidates = [
            path
            for path in versions_dir.iterdir()
            if path.is_file() and os.access(path, os.X_OK)
        ]
        candidates.sort(key=lambda path: _version_key(path.name), reverse=True)
        return str(candidates[0]) if candidates else None

    nvm_versions = Path.home() / ".nvm/versions/node"
    if not nvm_versions.is_dir():
        return None
    candidates = [
        executable
        for version_dir in nvm_versions.iterdir()
        for executable in [version_dir / "bin" / binary]
        if executable.exists() and os.access(executable, os.X_OK)
    ]
    candidates.sort(key=lambda path: _version_key(path.parents[1].name), reverse=True)
    return str(candidates[0]) if candidates else None


def executable_path_prefix(executable: str) -> str | None:
    raw_path = Path(executable)
    for path in (raw_path, raw_path.resolve()):
        parts = path.parts
        for index in range(len(parts) - 3):
            if parts[index : index + 3] == (".nvm", "versions", "node"):
                version_root = Path(*parts[: index + 4])
                bin_dir = version_root / "bin"
                if bin_dir.is_dir():
                    return str(bin_dir)
    if _uses_env_node(raw_path):
        return _latest_nvm_node_bin()
    return None


def _uses_env_node(path: Path) -> bool:
    try:
        first_line = path.read_text(errors="ignore").splitlines()[0]
    except (IndexError, OSError):
        return False
    return first_line.startswith("#!") and "env node" in first_line


def _latest_nvm_node_bin() -> str | None:
    nvm_versions = Path.home() / ".nvm/versions/node"
    if not nvm_versions.is_dir():
        return None
    candidates = [
        version_dir / "bin"
        for version_dir in nvm_versions.iterdir()
        if (version_dir / "bin" / "node").exists()
    ]
    candidates.sort(key=lambda path: _version_key(path.parent.name), reverse=True)
    return str(candidates[0]) if candidates else None


def _version_key(version: str) -> tuple[int | str, ...]:
    return tuple(
        int(part) if part.isdigit() else part
        for part in version.replace("-", ".").split(".")
    )


def _resolve_login_command(binary: str, *, shell: str | None) -> list[str] | None:
    login_shell = shell or default_login_shell()
    executable = resolve_executable(binary, login_shell=login_shell)
    if executable is None:
        return None
    shell_command = "exec " + shlex.quote(executable)
    if path_prefix := executable_path_prefix(executable):
        shell_command = (
            f"export PATH={shlex.quote(path_prefix)}{os.pathsep}$PATH; {shell_command}"
        )
    return [login_shell, "-lc", shell_command]


def _command_with_args(command: Sequence[str], *args: str) -> list[str]:
    expanded = list(command)
    if len(expanded) >= 3 and expanded[1] == "-lc":
        expanded[2] += " " + " ".join(shlex.quote(arg) for arg in args)
        return expanded
    return [*expanded, *args]


def default_auth_adapters(*, shell: str | None = None) -> dict[str, HarnessAuthAdapter]:
    adapters: dict[str, HarnessAuthAdapter] = {}
    claude = _resolve_login_command("claude", shell=shell)
    if claude is not None:
        adapters["claude-code"] = CommandAuthAdapter(
            "claude-code",
            _command_with_args(claude, "auth", "status", "--json"),
            _command_with_args(claude, "auth", "login"),
        )
    codex = _resolve_login_command("codex", shell=shell)
    if codex is not None:
        adapters["codex"] = CommandAuthAdapter(
            "codex",
            _command_with_args(codex, "login", "status"),
            _command_with_args(codex, "login", "--device-auth"),
        )
    agy = _resolve_login_command("agy", shell=shell)
    if agy is not None:
        adapters["agy"] = CommandAuthAdapter(
            "agy",
            _command_with_args(agy, "--version"),
            list(agy),
        )
    return adapters


@dataclass
class _AuthFlow:
    flow_id: str
    harness: str
    process: subprocess.Popen[str]
    pgid: int
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

            try:
                process = subprocess.Popen(
                    self._adapter(harness).command(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    bufsize=1,
                    start_new_session=True,
                )
            except OSError as exc:
                raise AuthFlowLaunchError(
                    "authentication command could not start"
                ) from exc
            flow = _AuthFlow(
                flow_id=str(uuid4()),
                harness=harness,
                process=process,
                pgid=process.pid,
                started_at=time.time(),
                timeout_s=self._timeout_s,
            )
            self._flows_by_id[flow.flow_id] = flow
            self._active_flow_ids[harness] = flow.flow_id

        threading.Thread(target=self._consume_output, args=(flow,), daemon=True).start()
        threading.Thread(
            target=self._stop_descendants_after_leader_exit,
            args=(flow,),
            daemon=True,
        ).start()
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
                self._stop_process(flow.process, flow.pgid)
                self._schedule_discard_locked(flow)
        return flow.snapshot().as_json()

    def close_all(self) -> None:
        with self._lock:
            flows = list(self._flows_by_id.values())
        for flow in flows:
            with flow.lock:
                if flow.state not in _TERMINAL_FLOW_STATES:
                    flow.state = "cancelled"
                    flow.completed_at = time.time()
                    flow.message = "authentication flow closed"
                    self._schedule_discard_locked(flow)
            self._stop_process(flow.process, flow.pgid)

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
                    completed_at is not None and now - completed_at >= self._retention_s
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

    @staticmethod
    def _stop_process(process: subprocess.Popen[str], pgid: int) -> None:
        def send_group(sig: int) -> bool:
            try:
                os.killpg(pgid, sig)
                return True
            except ProcessLookupError:
                return True
            except OSError:
                return False

        try:
            if not send_group(signal.SIGTERM):
                if process.poll() is None:
                    process.terminate()
        except OSError:
            return
        try:
            process.wait(timeout=_PROCESS_STOP_TIMEOUT_S)
            send_group(signal.SIGKILL)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if not send_group(signal.SIGKILL) and process.poll() is None:
                process.kill()
        except OSError:
            return
        try:
            process.wait(timeout=_PROCESS_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return

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

    def _stop_descendants_after_leader_exit(self, flow: _AuthFlow) -> None:
        flow.process.wait()
        self._stop_process(flow.process, flow.pgid)

    def _consume_output(self, flow: _AuthFlow) -> None:
        try:
            assert flow.process.stdout is not None
            for raw_line in flow.process.stdout:
                raw_line = raw_line.rstrip()
                safe_user_code = _extract_safe_user_code(raw_line)
                line = redact_auth_text(raw_line)
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
                    if safe_user_code is not None and flow.user_code is None:
                        flow.user_code = safe_user_code
                    elif user_code is not None and flow.user_code is None:
                        flow.user_code = user_code.group(0)
                    flow.state = "waiting_for_user"
        except Exception:
            with flow.lock:
                if flow.state in _TERMINAL_FLOW_STATES:
                    return
                flow.state = "failed"
                flow.completed_at = time.time()
                flow.last_error = "authentication output read failed"
                self._stop_process(flow.process, flow.pgid)
                self._schedule_discard_locked(flow)
            return

        return_code = flow.process.wait()
        status: HarnessAuthStatus | None = None
        if return_code == 0:
            try:
                status = self._adapter(flow.harness).status()
            except Exception as exc:
                status = HarnessAuthStatus(
                    flow.harness,
                    "unknown",
                    detail=redact_auth_text(str(exc)) or "status check failed",
                )
        with flow.lock:
            if flow.state in _TERMINAL_FLOW_STATES:
                return
            flow.completed_at = time.time()
            if (
                return_code == 0
                and status is not None
                and status.state == "authenticated"
            ):
                flow.state = "authenticated"
            elif return_code == 0 and status is not None:
                flow.state = "failed"
                diagnostic = (
                    f"authentication status is {status.state} after successful login"
                )
                if status.detail:
                    diagnostic += f": {status.detail}"
                flow.last_error = redact_auth_text(diagnostic)
            else:
                flow.state = "failed"
                flow.last_error = (
                    f"authentication process exited with code {return_code}"
                )
            self._schedule_discard_locked(flow)

    def _expire_flow(self, flow: _AuthFlow) -> None:
        remaining_s = max(flow.started_at + flow.timeout_s - time.time(), 0)
        time.sleep(remaining_s)

        with flow.lock:
            if flow.state in _TERMINAL_FLOW_STATES:
                return
            flow.state = "expired"
            flow.completed_at = time.time()
            flow.last_error = "authentication flow expired"
            self._stop_process(flow.process, flow.pgid)
            self._schedule_discard_locked(flow)
