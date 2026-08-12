"""claude-code harness backend — summaries from the CLI already on the box.

Why a CLI and not the API: this host has no Anthropic API key, but it does
have an authenticated ``claude`` install that the harness daemon already
drives, and whose quota is already monitored. The backend it replaces was a
7B local model that could not hold the response schema (1,306 "missing
required keys" and 473 "not JSON" failures in one log), and paid a ~5GB model
reload per job on a 16GB host.

The invocation is deliberately the smallest one that can answer:

* ``-p --output-format json`` — one headless turn, one JSON envelope out.
* ``--tools ""`` — summarizing is pure text work, so the CLI gets no tools
  and cannot touch the filesystem, the network, or a shell.
* ``--strict-mcp-config`` + ``--setting-sources ""`` — no MCP servers, no
  user/project settings, hooks or memory files. This is what keeps the call
  cheap: measured at 203 input tokens for a short prompt.
* ``--no-session-persistence`` — 677 summaries a day should not leave 677
  resumable transcripts on disk.
* ``--system-prompt`` — replaces the (large) default Claude Code system
  prompt with one line about returning JSON.

The prompt itself goes in on stdin, never in argv: transcripts are long and
argv is both size-limited and visible in ``ps``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
from typing import Callable, Optional

from drover.server.harness.structured.claude import child_env, resolve_binary
from drover.server.summarizer.backends.types import BackendError, BackendReadinessError
from drover.server.summarizer.client import (
    SummarizerClientError,
    parse_summary_response,
)

log = logging.getLogger("drover.summarizer.backends.harness")

DEFAULT_HARNESS_MODEL = "haiku"
# One wedged CLI must not hold a worker forever. Measured cost of a short
# headless turn is ~4s; a full session transcript on Haiku is well inside
# this, so anything past it is a stuck process, not a slow answer.
DEFAULT_TIMEOUT_S = 180.0
# Each invocation is a node process of a few hundred MB. Two at a time is
# what a 16GB host can hold beside DuckDB without going back into the swap
# storm this backend exists to end. Peak load is ~82 jobs/hour, so two
# concurrent invocations is throughput to spare, not a bottleneck.
DEFAULT_MAX_CONCURRENCY = 2
# Waiting for a slot is not a failed generation; it is queueing. Give up
# only when the wait itself is longer than a whole invocation would take.
_GATE_WAIT_S = DEFAULT_TIMEOUT_S

_REQUIRED_KEYS = ("summary_md", "next_steps_md", "open_questions")
_OPTIONAL_KEYS = ("last_user_prompt", "last_assistant")

# Markers that mean "the CLI could not work right now" rather than "the model
# answered badly": auth, quota and upstream capacity. These become readiness
# errors so a retry can succeed, and the job's retry budget bounds how many
# times we bother.
_READINESS_MARKERS = (
    "/login",
    "invalid api key",
    "authentication_error",
    "unauthorized",
    "oauth",
    "credit balance",
    "usage limit",
    "rate limit",
    "rate_limit",
    "overloaded",
    "service unavailable",
    "econnreset",
    "etimedout",
    "network error",
    "fetch failed",
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %.0f", name, raw, default)
        return default
    return value if value > 0 else default


# Process-wide, not per-backend: the summarizer, brief, active-brief and live
# recap workers each hold their own backend instance and run on their own
# threads. Bounding them individually would bound nothing.
_INVOCATION_GATE = threading.BoundedSemaphore(
    _env_int("DROVER_SUMMARIZER_HARNESS_CONCURRENCY", DEFAULT_MAX_CONCURRENCY)
)


def _run_cli(
    command: list[str],
    *,
    prompt: str,
    timeout_s: float,
    cwd: Optional[str],
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    """Run one headless CLI turn, killing the whole process group on timeout."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # The CLI spawns children; killing only the parent leaves them
        # holding the memory this backend is supposed to bound.
        try:
            os.killpg(os.getpgid(process.pid), 9)
        except (OSError, ProcessLookupError):
            process.kill()
        process.communicate()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _looks_retryable(*texts: str) -> bool:
    haystack = " ".join(text.lower() for text in texts if text)
    return any(marker in haystack for marker in _READINESS_MARKERS)


class ClaudeCodeBackend:
    """LLMBackend implementation backed by the local ``claude`` CLI."""

    name = "claude-code"
    SYSTEM_PROMPT = (
        "You summarize coding-agent sessions. Reply with exactly one JSON "
        "object matching the requested keys. No prose, no code fences."
    )

    def __init__(
        self,
        *,
        model: str = DEFAULT_HARNESS_MODEL,
        binary: Optional[str] = None,
        timeout_s: Optional[float] = None,
        cwd: Optional[str] = None,
        required_keys: Optional[tuple] = None,
        optional_keys: Optional[tuple] = None,
        gate: Optional[threading.Semaphore] = None,
        _runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    ) -> None:
        self.cli_model = model
        self.model = f"claude-code/{model}"
        self.binary = binary
        self.timeout_s = (
            timeout_s
            if timeout_s is not None
            else _env_float("DROVER_SUMMARIZER_HARNESS_TIMEOUT_S", DEFAULT_TIMEOUT_S)
        )
        # A neutral working directory: no project files, no CLAUDE.md, and
        # nothing of the server's own cwd in scope.
        self.cwd = cwd or tempfile.gettempdir()
        self.required_keys = (
            required_keys if required_keys is not None else _REQUIRED_KEYS
        )
        self.optional_keys = (
            optional_keys if optional_keys is not None else _OPTIONAL_KEYS
        )
        self.gate = gate or _INVOCATION_GATE
        self._runner = _runner or _run_cli

    def command(self) -> list[str]:
        return [
            resolve_binary(self.binary) or "claude",
            "-p",
            "--output-format",
            "json",
            "--model",
            self.cli_model,
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--no-session-persistence",
            "--system-prompt",
            self.SYSTEM_PROMPT,
        ]

    def summarize(self, prompt: str) -> dict:
        completed = self._invoke(prompt)
        return self._parse(completed)

    # -- invocation --------------------------------------------------------

    def _invoke(self, prompt: str) -> subprocess.CompletedProcess:
        if not self.gate.acquire(timeout=_GATE_WAIT_S):
            raise BackendReadinessError(
                "claude-code readiness: no invocation slot free after "
                f"{_GATE_WAIT_S:.0f}s"
            )
        try:
            return self._runner(
                self.command(),
                prompt=prompt,
                timeout_s=self.timeout_s,
                cwd=self.cwd,
                env=child_env(),
            )
        except FileNotFoundError as e:
            raise BackendReadinessError(
                f"claude-code readiness: CLI not found on PATH ({e})"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise BackendReadinessError(
                f"claude-code readiness: invocation timed out after "
                f"{self.timeout_s:.0f}s"
            ) from e
        except OSError as e:
            raise BackendReadinessError(
                f"claude-code readiness: CLI could not be launched ({e})"
            ) from e
        finally:
            self.gate.release()

    # -- parsing -----------------------------------------------------------

    def _parse(self, completed: subprocess.CompletedProcess) -> dict:
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if completed.returncode != 0:
            detail = (stderr or stdout)[:300].strip()
            if _looks_retryable(stderr, stdout):
                raise BackendReadinessError(
                    f"claude-code readiness: CLI exited {completed.returncode}: {detail}"
                )
            raise BackendError(
                f"claude-code: CLI exited {completed.returncode}: {detail}"
            )

        try:
            envelope = json.loads(stdout)
        except ValueError as e:
            raise BackendError(
                f"claude-code: CLI output is not JSON ({e}); raw={stdout[:200]!r}"
            ) from e
        if not isinstance(envelope, dict):
            raise BackendError(
                f"claude-code: CLI output is not an object: {type(envelope).__name__}"
            )

        result = envelope.get("result")
        text = result if isinstance(result, str) else ""
        if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
            detail = (text or json.dumps(envelope))[:300]
            if _looks_retryable(text, str(envelope.get("subtype"))):
                raise BackendReadinessError(
                    f"claude-code readiness: turn failed ({envelope.get('subtype')}): {detail}"
                )
            raise BackendError(
                f"claude-code: turn failed ({envelope.get('subtype')}): {detail}"
            )
        if not text:
            raise BackendError("claude-code: CLI returned an empty result")

        try:
            return parse_summary_response(
                text,
                required_keys=tuple(self.required_keys),
                optional_keys=tuple(self.optional_keys),
            )
        except SummarizerClientError as e:
            raise BackendError(f"claude-code: {e}") from e
