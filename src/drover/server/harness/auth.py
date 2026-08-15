from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field, replace
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
from typing import Any, Iterator, Protocol, Sequence
from uuid import uuid4

import codecs

from .pty import make_controlling_tty_preexec, resize_pty

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
# Hyphenated one-time codes. Group widths vary by provider -- codex issues
# `P1KY-AS6MU` (4 then 5), so pinning every group to four characters silently
# dropped the code and left the device page with nothing to enter.
_USER_CODE_GROUPS = r"[A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8})+"
_USER_CODE_RE = re.compile(rf"\b{_USER_CODE_GROUPS}\b")
_USER_CODE_CONTEXT_RE = re.compile(
    rf"(?i)\b(?:pairing|user|device)\s+code\s*:\s*({_USER_CODE_GROUPS})\b"
)
# Terminal control sequences. Login CLIs colourise their URLs and codes, and
# claude wraps its URL in an OSC-8 hyperlink; left in place the trailing reset
# is captured as part of the URL itself.
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ESCAPE_RE = re.compile(r"\x1b[@-Z\\-_]")
# Query parameters whose value is a fixed flag rather than a credential.
# `claude auth login` sets `code=true` to ask for a pasteable code; redacting
# it produced a URL that opened but could never yield one.
# Bare fragment, not a compiled pattern: it is spliced into a larger regex
# that already carries the case-insensitive flag, and an inline `(?i)` is
# only legal at the very start of an expression.
_NON_SECRET_CODE_VALUE = r"(?:true|false|0|1)(?=[&\s]|$)"
_CODE_FLAG_RE = re.compile(r"(?i)\A(?:true|false|0|1)\Z")
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
    r'("[^"]*"|\'[^\']*\'|[^&\s,}]+)'
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
_PTY_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")
_TERMINAL_FLOW_STATES = {"authenticated", "failed", "expired", "cancelled"}
_PROCESS_STOP_TIMEOUT_S = 0.2


class AuthFlowLaunchError(RuntimeError):
    """Authentication CLI could not be started on this host."""


class TerminalSignInRequired(RuntimeError):
    """This harness can only be signed in from an interactive terminal.

    Raised instead of launching a flow that is structurally incapable of
    succeeding -- agy has no login subcommand, only a full-screen TUI.
    """


class AuthFlowInputError(RuntimeError):
    """Typed input could not be delivered to the authentication CLI."""


@dataclass(frozen=True)
class HarnessAuthStatus:
    harness: str
    state: str
    label: str | None = None
    detail: str | None = None
    # How a client should sign this harness in: "flow" drives the managed
    # start/poll/input lifecycle, "terminal" means hand the user a PTY
    # session because the CLI only authenticates through its own TUI.
    sign_in: str = "flow"

    def as_json(self, *, host_id: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "harness": self.harness,
            "state": self.state,
            "label": self.label,
            "detail": self.detail,
            "sign_in": self.sign_in,
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
    # True when the CLI is running on a PTY and can still be typed into, so
    # the client knows whether to offer a "paste your code" field.
    supports_input: bool = False

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
            "supports_input": self.supports_input,
        }
        if host_id is not None:
            data["host_id"] = host_id
        return data


class HarnessAuthAdapter(Protocol):
    harness: str
    # Whether the login CLI must be given a controlling terminal.
    requires_pty: bool
    # "flow" or "terminal" -- see HarnessAuthStatus.sign_in.
    sign_in: str

    def status(self) -> HarnessAuthStatus: ...

    def command(self) -> list[str]: ...


@dataclass(frozen=True)
class StaticAuthAdapter:
    harness: str
    status_value: HarnessAuthStatus | None = None
    start_command: list[str] | None = None
    requires_pty: bool = False
    sign_in: str = "flow"

    def status(self) -> HarnessAuthStatus:
        if self.status_value is not None:
            return replace(self.status_value, sign_in=self.sign_in)
        return HarnessAuthStatus(
            self.harness,
            "unavailable",
            detail=f"auth is not supported for {self.harness}",
            sign_in=self.sign_in,
        )

    def command(self) -> list[str]:
        if not self.start_command:
            raise RuntimeError(f"auth is not supported for {self.harness}")
        return self.start_command


def _redact_assignment(match: re.Match[str]) -> str:
    """Redact ``key=value``, sparing the OAuth ``code`` request flag.

    Everything reaching this is a candidate secret, so the exemption is kept
    as narrow as it can be: only the literal name ``code``, and only when its
    whole value is a boolean. A real authorization code never looks like this.
    """
    key, separator, value = match.group(1), match.group(2), match.group(3)
    if key.lower() == "code" and _CODE_FLAG_RE.match(value):
        return match.group(0)
    return f"{key}{separator}<redacted>"


def redact_auth_text(text: str) -> str:
    redacted = _URL_USERINFO_RE.sub(r"\1<redacted>@", text)
    redacted = _AUTHORIZATION_RE.sub(r"\1<redacted>", redacted)
    redacted = _BEARER_TOKEN_RE.sub(r"\1<redacted>", redacted)
    redacted = _JWT_RE.sub("<redacted>", redacted)
    redacted = _PROVIDER_TOKEN_RE.sub("<redacted>", redacted)
    redacted = _JSON_SECRET_RE.sub(r'\1"<redacted>"', redacted)
    redacted = _COLON_SECRET_RE.sub(r"\1\2<redacted>", redacted)
    redacted = _SPACED_ASSIGN_SECRET_RE.sub(_redact_assignment, redacted)
    redacted = _ENV_SPACE_SECRET_RE.sub(r"\1\2<redacted>", redacted)
    redacted = _KEY_SPACE_SECRET_RE.sub(r"\1\2<redacted>", redacted)
    redacted = _PHRASE_SPACE_SECRET_RE.sub(r"\1\2<redacted>", redacted)
    for key in sorted(_SECRET_QUERY_KEYS, key=len, reverse=True):
        name = re.escape(key).replace("_", r"[-_]")
        # `code` is the one key that carries a flag as often as a secret.
        # Skip the boolean literals and redact everything else, so a real
        # authorization code in a callback URL is still hidden.
        guard = f"(?!{_NON_SECRET_CODE_VALUE})" if key == "code" else ""
        redacted = re.sub(
            rf"(?i)({name}=){guard}[^&\s]+",
            r"\1<redacted>",
            redacted,
        )
    redacted = re.sub(r"(?i)client_secret(?==)", "client_<redacted>", redacted)
    return redacted


def strip_ansi(text: str) -> str:
    """Drop terminal control sequences from a line of CLI output.

    OSC sequences go first: an OSC-8 hyperlink encloses the URL it points
    at, so removing the CSI colour codes inside it before the wrapper would
    leave the target stranded as bare text next to its visible duplicate.
    """
    stripped = _OSC_RE.sub("", text)
    stripped = _CSI_RE.sub("", stripped)
    return _ESCAPE_RE.sub("", stripped)


def _extract_safe_user_code(text: str) -> str | None:
    match = _USER_CODE_CONTEXT_RE.search(text)
    return match.group(1) if match is not None else None


@dataclass(frozen=True)
class CommandAuthAdapter:
    harness: str
    status_command: list[str]
    login_command: list[str]
    requires_pty: bool = False
    sign_in: str = "flow"

    def status(self) -> HarnessAuthStatus:
        return replace(self._probe(), sign_in=self.sign_in)

    def _probe(self) -> HarnessAuthStatus:
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


def _spawn_on_pty(command: Sequence[str]) -> tuple[subprocess.Popen[Any], int]:
    """Start a login CLI with a controlling terminal it can prompt on.

    Sized generously in columns so the CLI does not hard-wrap the authorize
    URL: a wrapped URL arrives as several lines and the extractor would only
    ever capture the first fragment.
    """
    master_fd, slave_fd = os.openpty()
    resize_pty(slave_fd, rows=24, cols=400)
    try:
        process = subprocess.Popen(
            list(command),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=make_controlling_tty_preexec(slave_fd),
        )
    except BaseException:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)
    return process, master_fd


def _iter_pty_lines(master_fd: int) -> Iterator[str]:
    """Yield decoded lines from a PTY master until the child's side closes.

    A PTY is a stream of bytes with no EOF until the last slave descriptor
    goes away, at which point macOS and Linux both surface EIO rather than an
    empty read -- so that is the loop's terminating condition, not `not data`.
    Carriage returns are treated as line breaks because terminal output uses
    CRLF and TUIs repaint with bare CRs.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    while True:
        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        pending += decoder.decode(chunk)
        parts = _PTY_LINE_BREAK_RE.split(pending)
        pending = parts.pop()
        for part in parts:
            yield part
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending


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

    if binary == "dsh":
        installed = Path.home() / ".local/share/deepseek-harness/node_modules/.bin/dsh"
        if installed.exists() and os.access(installed, os.X_OK):
            return str(installed)
        return None

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
        # `claude auth login` renders its "Paste code here" prompt through
        # ink, which needs a terminal: on a pipe it prints the authorize URL
        # and then hangs forever without ever prompting.
        adapters["claude-code"] = CommandAuthAdapter(
            "claude-code",
            _command_with_args(claude, "auth", "status", "--json"),
            _command_with_args(claude, "auth", "login"),
            requires_pty=True,
        )
    codex = _resolve_login_command("codex", shell=shell)
    if codex is not None:
        # The device-code flow is line-oriented and needs no terminal; the
        # user finishes it entirely in a browser.
        adapters["codex"] = CommandAuthAdapter(
            "codex",
            _command_with_args(codex, "login", "status"),
            _command_with_args(codex, "login", "--device-auth"),
        )
    agy = _resolve_login_command("agy", shell=shell)
    if agy is not None:
        # agy exposes no login subcommand -- signing in means driving the
        # full-screen TUI the bare binary opens, so there is nothing here a
        # managed flow could scrape or answer.
        adapters["agy"] = CommandAuthAdapter(
            "agy",
            _command_with_args(agy, "--version"),
            list(agy),
            requires_pty=True,
            sign_in="terminal",
        )
    return adapters


@dataclass
class _AuthFlow:
    flow_id: str
    harness: str
    # `Popen[Any]`: PTY-backed flows are byte-mode (their streams are the
    # terminal, not pipes), pipe-backed ones are text-mode.
    process: subprocess.Popen[Any]
    pgid: int
    started_at: float
    timeout_s: float
    # Write end of the child's terminal, when it was given one. Its presence
    # is what makes a flow answerable: pipe-backed flows have no way to
    # deliver a pasted code.
    master_fd: int | None = None
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
                supports_input=(
                    self.master_fd is not None
                    and self.state not in _TERMINAL_FLOW_STATES
                ),
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
            adapter = self._adapter(harness)
            if getattr(adapter, "sign_in", "flow") == "terminal":
                raise TerminalSignInRequired(
                    f"{harness} can only be signed in from a terminal session"
                )
            existing_id = self._active_flow_ids.get(harness)
            existing = self._flows_by_id.get(existing_id) if existing_id else None
            if existing is not None and not self._is_terminal(existing):
                return existing.snapshot().as_json()

            master_fd: int | None = None
            try:
                if getattr(adapter, "requires_pty", False):
                    # Closes its own terminal if the spawn fails, so there is
                    # nothing to clean up here.
                    process, master_fd = _spawn_on_pty(adapter.command())
                else:
                    process = subprocess.Popen(
                        adapter.command(),
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
                master_fd=master_fd,
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

    def send_input(self, harness: str, flow_id: str, text: str) -> dict[str, Any]:
        """Type a line into a running login CLI, e.g. a pasted OAuth code.

        Never logged or retained: the text goes straight to the terminal and
        whatever the CLI echoes back travels the same redaction path as the
        rest of its output.
        """
        flow = self._flow(harness, flow_id)
        with flow.lock:
            if flow.state in _TERMINAL_FLOW_STATES:
                raise AuthFlowInputError(
                    f"authentication flow is no longer accepting input: {flow.state}"
                )
            if flow.master_fd is None:
                raise AuthFlowInputError(f"{harness} sign-in does not accept input")
            payload = text.rstrip("\r\n") + "\r"
            try:
                os.write(flow.master_fd, payload.encode("utf-8"))
            except OSError as exc:
                raise AuthFlowInputError("could not deliver input") from exc
        # Snapshot outside the flow lock: `snapshot()` takes it, and it is a
        # plain non-reentrant Lock.
        return flow.snapshot().as_json()

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
    def _stop_process(process: subprocess.Popen[Any], pgid: int) -> None:
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

    @staticmethod
    def _output_lines(flow: _AuthFlow) -> Iterator[str]:
        if flow.master_fd is not None:
            return _iter_pty_lines(flow.master_fd)
        assert flow.process.stdout is not None
        return iter(flow.process.stdout)

    def _consume_output(self, flow: _AuthFlow) -> None:
        try:
            self._read_until_exit(flow)
        finally:
            # The reader owns the terminal: it is the only thread that can
            # close the master safely, once nothing is blocked reading it.
            self._release_pty(flow)

    @staticmethod
    def _release_pty(flow: _AuthFlow) -> None:
        with flow.lock:
            master_fd, flow.master_fd = flow.master_fd, None
        if master_fd is not None:
            with suppress(OSError):
                os.close(master_fd)

    def _read_until_exit(self, flow: _AuthFlow) -> None:
        try:
            for raw_line in self._output_lines(flow):
                raw_line = strip_ansi(raw_line).rstrip()
                # A terminal emits far more blank lines than a pipe does --
                # CRLF pairs and TUI repaints -- and an empty message would
                # replace the URL the user still needs to see.
                if not raw_line:
                    continue
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
