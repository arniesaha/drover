"""Bounded, host-local Codex account and rate-limit probe."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from queue import Empty, Queue
import subprocess
import threading
from time import monotonic
from typing import Any, Mapping, Sequence
from uuid import uuid4

from drover.server.harness.auth import redact_auth_text
from drover.server.providers.types import ProviderAccountSnapshot, ProviderUsageWindow

log = logging.getLogger(__name__)
_PROCESS_STOP_TIMEOUT_S = 0.5
_MAX_CAPTURED_STDERR_CHARS = 16_384


class _ProbeFailure(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class CodexUsageProbe:
    """Read the authenticated Codex account's native ChatGPT rate limits."""

    def __init__(
        self,
        command: Sequence[str] | None = None,
        timeout_s: float = 5.0,
    ):
        # No default command: the caller resolves the CLI through the user's
        # login shell. Spawning a bare "codex" would search this process's PATH,
        # which under launchd omits the prefix the CLI is installed in.
        self.command = tuple(command) if command else None
        self.timeout_s = timeout_s

    def read(self, *, host_id: str = "local") -> ProviderAccountSnapshot:
        observed_at = datetime.now(timezone.utc)
        process: subprocess.Popen[str] | None = None
        reader: threading.Thread | None = None
        stderr_reader: threading.Thread | None = None
        lines: Queue[str | None] = Queue()
        stderr_parts: list[str] = []
        try:
            if self.command is None:
                raise _ProbeFailure("cli_not_found")
            if self.timeout_s <= 0:
                raise _ProbeFailure("timeout")
            deadline = monotonic() + self.timeout_s
            try:
                process = subprocess.Popen(
                    self.command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            except FileNotFoundError:
                # Distinct from "unavailable", which is also the host-level
                # catch-all: the CLI is simply not where we were told it is.
                raise _ProbeFailure("cli_not_found") from None
            except OSError:
                raise _ProbeFailure("unavailable") from None

            if (
                process.stdin is None
                or process.stdout is None
                or process.stderr is None
            ):
                raise _ProbeFailure("process_error")
            reader = _stdout_reader(process.stdout, lines)
            stderr_reader = _stderr_reader(process.stderr, stderr_parts)
            initialize = self._request(
                process,
                lines,
                deadline,
                1,
                "initialize",
                {
                    "clientInfo": {
                        "name": "drover",
                        "title": "Drover",
                        "version": "0.1.0",
                    }
                },
            )
            if not isinstance(initialize, Mapping):
                raise _ProbeFailure("protocol_error")
            self._notify(process, "initialized", {})
            account_response = self._request(
                process,
                lines,
                deadline,
                2,
                "account/read",
                {"refreshToken": False},
            )
            rate_limit_response = self._request(
                process,
                lines,
                deadline,
                3,
                "account/rateLimits/read",
                None,
            )
            return _snapshot_from_responses(
                account_response,
                rate_limit_response,
                host_id=host_id,
                observed_at=observed_at,
            )
        except _ProbeFailure as exc:
            return _error_snapshot(host_id, observed_at, exc.category)
        except (TypeError, ValueError, OverflowError):
            return _error_snapshot(host_id, observed_at, "protocol_error")
        finally:
            if process is not None:
                _stop_process(process)
            if reader is not None:
                reader.join(timeout=_PROCESS_STOP_TIMEOUT_S)
            if stderr_reader is not None:
                stderr_reader.join(timeout=_PROCESS_STOP_TIMEOUT_S)
            stderr = "".join(stderr_parts)
            if stderr:
                # Stderr can contain CLI or auth diagnostics. It is never
                # returned in the API response and must be redacted before a
                # local diagnostic log sees it.
                log.debug("codex app-server probe stderr: %s", redact_auth_text(stderr))

    def _request(
        self,
        process: subprocess.Popen[str],
        lines: Queue[str | None],
        deadline: float,
        request_id: int,
        method: str,
        params: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        payload: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._write(process, payload)
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise _ProbeFailure("timeout")
            try:
                line = lines.get(timeout=remaining)
            except Empty:
                raise _ProbeFailure("timeout") from None
            if line is None:
                raise _ProbeFailure("process_error")
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                raise _ProbeFailure("protocol_error") from None
            if not isinstance(response, Mapping):
                raise _ProbeFailure("protocol_error")
            if response.get("id") != request_id:
                continue
            if "error" in response or not isinstance(response.get("result"), Mapping):
                raise _ProbeFailure("protocol_error")
            return response["result"]

    def _notify(
        self, process: subprocess.Popen[str], method: str, params: Mapping[str, Any]
    ) -> None:
        self._write(process, {"method": method, "params": params})

    @staticmethod
    def _write(process: subprocess.Popen[str], payload: Mapping[str, Any]) -> None:
        if process.stdin is None:
            raise _ProbeFailure("process_error")
        try:
            process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise _ProbeFailure("process_error") from None


def _stdout_reader(stream, lines: Queue[str | None]) -> threading.Thread:
    def read_lines() -> None:
        try:
            for line in iter(stream.readline, ""):
                lines.put(line)
        finally:
            lines.put(None)

    thread = threading.Thread(target=read_lines, daemon=True)
    thread.start()
    return thread


def _stderr_reader(stream, captured: list[str]) -> threading.Thread:
    def read_chunks() -> None:
        remaining = _MAX_CAPTURED_STDERR_CHARS
        for chunk in iter(lambda: stream.read(4096), ""):
            if remaining:
                captured.append(chunk[:remaining])
                remaining -= len(chunk)

    thread = threading.Thread(target=read_chunks, daemon=True)
    thread.start()
    return thread


def _stop_process(process: subprocess.Popen[str]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=_PROCESS_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_PROCESS_STOP_TIMEOUT_S)
    finally:
        if process.stdin is not None:
            process.stdin.close()


def _snapshot_from_responses(
    account_response: Mapping[str, Any],
    rate_limit_response: Mapping[str, Any],
    *,
    host_id: str,
    observed_at: datetime,
) -> ProviderAccountSnapshot:
    account = account_response.get("account")
    if not isinstance(account, Mapping):
        raise _ProbeFailure("protocol_error")
    account_label = _optional_text(account.get("email")) or "Codex"
    plan_label = _optional_text(account.get("planType"))
    rate_limits = rate_limit_response.get("rateLimits")
    if not isinstance(rate_limits, Mapping):
        raise _ProbeFailure("protocol_error")
    windows = tuple(
        window
        for kind in ("primary", "secondary")
        if (window := _usage_window(kind, rate_limits.get(kind))) is not None
    )
    return _snapshot(
        account_label=account_label,
        plan_label=plan_label,
        host_id=host_id,
        status="ok",
        observed_at=observed_at,
        windows=windows,
        error_category=None,
    )


def _usage_window(kind: str, value: Any) -> ProviderUsageWindow | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _ProbeFailure("protocol_error")
    used_percent = value.get("usedPercent")
    if isinstance(used_percent, bool) or not isinstance(used_percent, (int, float)):
        raise _ProbeFailure("protocol_error")
    reset = value.get("resetsAt")
    if reset is not None and (
        isinstance(reset, bool) or not isinstance(reset, (int, float))
    ):
        raise _ProbeFailure("protocol_error")
    duration = value.get("windowDurationMins")
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, int)
    ):
        raise _ProbeFailure("protocol_error")
    return ProviderUsageWindow(
        kind=kind,
        used_percent=float(used_percent),
        window_minutes=duration,
        resets_at=(
            datetime.fromtimestamp(reset, timezone.utc) if reset is not None else None
        ),
    )


def _error_snapshot(
    host_id: str, observed_at: datetime, error_category: str
) -> ProviderAccountSnapshot:
    return _snapshot(
        account_label="Codex",
        plan_label=None,
        host_id=host_id,
        status="error",
        observed_at=observed_at,
        windows=(),
        error_category=error_category,
    )


def _snapshot(
    *,
    account_label: str,
    plan_label: str | None,
    host_id: str,
    status: str,
    observed_at: datetime,
    windows: tuple[ProviderUsageWindow, ...],
    error_category: str | None,
) -> ProviderAccountSnapshot:
    fingerprint = {
        "provider": "openai",
        "account_label": account_label,
        "plan_label": plan_label,
        "host_id": host_id,
        "status": status,
        "windows": [
            {
                "kind": window.kind,
                "used_percent": window.used_percent,
                "window_minutes": window.window_minutes,
                "resets_at": window.resets_at.isoformat() if window.resets_at else None,
            }
            for window in windows
        ],
        "source": "codex-app-server",
        "error_category": error_category,
    }
    dedup_key = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ProviderAccountSnapshot(
        snapshot_id=str(uuid4()),
        dedup_key=dedup_key,
        provider="openai",
        account_label=account_label,
        plan_label=plan_label,
        host_id=host_id,
        status=status,  # type: ignore[arg-type]
        observed_at=observed_at,
        windows=windows,
        source="codex-app-server",
        error_category=error_category,
    )


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
