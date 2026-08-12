"""Tests for the claude-code harness summarizer backend.

Every test drives the backend through an injected runner. Nothing here
launches a real ``claude`` process: the CLI's wire shape is pinned by the
envelope fixtures below, captured from ``claude -p --output-format json``.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time

import pytest

from drover.server.summarizer.backends import BackendError, BackendReadinessError
from drover.server.summarizer.backends.harness import (
    DEFAULT_MAX_CONCURRENCY,
    ClaudeCodeBackend,
)

SUMMARY = {
    "summary_md": "the agent fixed the retry cap",
    "next_steps_md": "deploy it",
    "open_questions": ["does the streak reset?"],
}


def _envelope(result_text: str, *, is_error: bool = False, subtype: str = "success"):
    return json.dumps(
        {
            "type": "result",
            "subtype": subtype,
            "is_error": is_error,
            "result": result_text,
            "session_id": "2db88e58-d93b-486e-8823-a1e44e9d413f",
            "total_cost_usd": 0.0021,
        }
    )


def _runner_returning(stdout: str, *, returncode: int = 0, stderr: str = ""):
    calls: list[dict] = []

    def run(command, *, prompt, timeout_s, cwd, env):
        calls.append(
            {
                "command": command,
                "prompt": prompt,
                "timeout_s": timeout_s,
                "cwd": cwd,
                "env": env,
            }
        )
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _backend(runner, **kwargs) -> ClaudeCodeBackend:
    return ClaudeCodeBackend(_runner=runner, **kwargs)


# --- generation ---------------------------------------------------------------


def test_summarize_parses_the_json_result_out_of_the_cli_envelope() -> None:
    backend = _backend(_runner_returning(_envelope(json.dumps(SUMMARY))))

    out = backend.summarize("prompt")

    assert out["summary_md"] == "the agent fixed the retry cap"
    assert out["open_questions"] == ["does the streak reset?"]


def test_summarize_tolerates_a_fenced_json_result() -> None:
    fenced = "```json\n" + json.dumps(SUMMARY) + "\n```"
    backend = _backend(_runner_returning(_envelope(fenced)))

    assert backend.summarize("prompt")["next_steps_md"] == "deploy it"


def test_summarize_validates_the_configured_required_keys() -> None:
    backend = _backend(
        _runner_returning(_envelope(json.dumps({"recap": "still working"}))),
        required_keys=("recap",),
        optional_keys=(),
    )

    assert backend.summarize("prompt") == {"recap": "still working"}


def test_missing_required_key_is_a_backend_error() -> None:
    # The default session-summary schema is deliberately salvageable (see
    # client._normalize_summary_fields), so this pins the strict case: a
    # schema whose required key cannot be defaulted.
    backend = _backend(
        _runner_returning(_envelope(json.dumps({"summary_md": "wrong schema"}))),
        required_keys=("recap",),
        optional_keys=(),
    )

    with pytest.raises(BackendError) as excinfo:
        backend.summarize("prompt")
    assert not isinstance(excinfo.value, BackendReadinessError)
    assert "claude-code" in str(excinfo.value)


def test_non_json_result_text_is_a_backend_error() -> None:
    backend = _backend(_runner_returning(_envelope("I could not summarize that.")))

    with pytest.raises(BackendError, match="claude-code"):
        backend.summarize("prompt")


def test_non_json_stdout_is_a_backend_error() -> None:
    backend = _backend(_runner_returning("not an envelope at all"))

    with pytest.raises(BackendError, match="claude-code"):
        backend.summarize("prompt")


def test_generic_cli_failure_is_a_backend_error() -> None:
    backend = _backend(
        _runner_returning("", returncode=1, stderr="unexpected token in settings")
    )

    with pytest.raises(BackendError) as excinfo:
        backend.summarize("prompt")
    assert not isinstance(excinfo.value, BackendReadinessError)


# --- readiness (retryable) ----------------------------------------------------


def test_missing_cli_is_a_readiness_error() -> None:
    def run(command, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", command[0])

    with pytest.raises(BackendReadinessError, match="claude-code"):
        _backend(run).summarize("prompt")


def test_timeout_is_a_readiness_error() -> None:
    def run(command, *, timeout_s, **_kwargs):
        raise subprocess.TimeoutExpired(command, timeout_s)

    with pytest.raises(BackendReadinessError, match="timed out"):
        _backend(run, timeout_s=42.0).summarize("prompt")


def test_login_failure_is_a_readiness_error() -> None:
    backend = _backend(
        _runner_returning(
            "", returncode=1, stderr="Invalid API key · Please run /login"
        )
    )

    with pytest.raises(BackendReadinessError, match="claude-code"):
        backend.summarize("prompt")


def test_usage_limit_result_is_a_readiness_error() -> None:
    backend = _backend(
        _runner_returning(
            _envelope(
                "Claude AI usage limit reached",
                is_error=True,
                subtype="error_during_execution",
            )
        )
    )

    with pytest.raises(BackendReadinessError, match="claude-code"):
        backend.summarize("prompt")


# --- invocation shape ---------------------------------------------------------


def test_prompt_goes_on_stdin_and_never_into_argv() -> None:
    runner = _runner_returning(_envelope(json.dumps(SUMMARY)))
    _backend(runner, timeout_s=90.0).summarize("a very private transcript")

    call = runner.calls[0]  # type: ignore[attr-defined]
    assert call["prompt"] == "a very private transcript"
    assert "a very private transcript" not in call["command"]
    assert call["timeout_s"] == 90.0


def test_command_runs_headless_json_with_tools_disabled() -> None:
    runner = _runner_returning(_envelope(json.dumps(SUMMARY)))
    _backend(runner, model="haiku").summarize("prompt")

    command = runner.calls[0]["command"]  # type: ignore[attr-defined]
    assert command[1:] == [
        "-p",
        "--output-format",
        "json",
        "--model",
        "haiku",
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--no-session-persistence",
        "--system-prompt",
        ClaudeCodeBackend.SYSTEM_PROMPT,
    ]


def test_child_environment_drops_ambient_claude_variables(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("DROVER_TEST_KEEP", "keep-me")
    runner = _runner_returning(_envelope(json.dumps(SUMMARY)))

    _backend(runner).summarize("prompt")

    env = runner.calls[0]["env"]  # type: ignore[attr-defined]
    assert "CLAUDECODE" not in env
    assert env["DROVER_TEST_KEEP"] == "keep-me"


# --- concurrency --------------------------------------------------------------


def test_backends_share_one_process_wide_invocation_gate() -> None:
    first = ClaudeCodeBackend()
    second = ClaudeCodeBackend()

    assert first.gate is second.gate
    assert DEFAULT_MAX_CONCURRENCY == 2


def test_concurrent_summarize_calls_never_exceed_the_gate() -> None:
    gate = threading.BoundedSemaphore(2)
    live = 0
    peak = 0
    lock = threading.Lock()

    def run(command, **_kwargs):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return subprocess.CompletedProcess(
            command, 0, _envelope(json.dumps(SUMMARY)), ""
        )

    backends = [_backend(run, gate=gate) for _ in range(6)]
    threads = [threading.Thread(target=b.summarize, args=("p",)) for b in backends]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert peak == 2


def test_waiting_forever_for_a_slot_is_a_readiness_error() -> None:
    class _FullGate:
        def acquire(self, timeout=None):
            return False

        def release(self):  # pragma: no cover - never reached
            raise AssertionError("released a slot that was never acquired")

    def run(command, **_kwargs):  # pragma: no cover - never reached
        raise AssertionError("ran the CLI without a slot")

    with pytest.raises(BackendReadinessError, match="invocation slot"):
        _backend(run, gate=_FullGate()).summarize("prompt")


def test_gate_is_released_when_the_cli_fails() -> None:
    gate = threading.BoundedSemaphore(1)
    backend = _backend(_runner_returning("junk"), gate=gate)

    with pytest.raises(BackendError):
        backend.summarize("prompt")

    assert gate.acquire(blocking=False) is True
