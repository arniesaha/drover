"""Default-off startup sampling and bounded, credential-safe attribution."""

import io
import os
import subprocess
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from drover.server import startup_diagnostics as diagnostics


@pytest.mark.parametrize("value", [None, "", "0"])
def test_default_diagnostic_has_no_io_or_thread(monkeypatch, value):
    monkeypatch.delenv(diagnostics.STACK_INTERVAL_ENV, raising=False)
    if value is not None:
        monkeypatch.setenv(diagnostics.STACK_INTERVAL_ENV, value)
    dump = Mock()
    timer = Mock()
    monkeypatch.setattr(diagnostics.faulthandler, "dump_traceback_later", dump)
    monkeypatch.setattr(diagnostics.threading, "Timer", timer)
    stream = io.StringIO()
    monkeypatch.setattr(diagnostics.sys, "__stderr__", stream)
    assert diagnostics.arm_startup_diagnostics() is None
    dump.assert_not_called()
    timer.assert_not_called()
    assert stream.getvalue() == ""


@pytest.mark.parametrize("value", ["-1", "31", "nan", "credential-payload"])
def test_invalid_interval_is_ignored_without_echo(monkeypatch, value):
    monkeypatch.setenv(diagnostics.STACK_INTERVAL_ENV, value)
    stream = io.StringIO()
    monkeypatch.setattr(diagnostics.sys, "__stderr__", stream)
    dump = Mock()
    monkeypatch.setattr(diagnostics.faulthandler, "dump_traceback_later", dump)
    assert diagnostics.arm_startup_diagnostics() is None
    dump.assert_not_called()
    assert "invalid_stack_interval_ignored" in stream.getvalue()
    assert "credential-payload" not in stream.getvalue()


def test_opt_in_timer_window_and_close_are_idempotent(monkeypatch):
    monkeypatch.setenv(diagnostics.STACK_INTERVAL_ENV, "15")
    stream = io.StringIO()
    monkeypatch.setattr(diagnostics.sys, "__stderr__", stream)
    dump, cancel, timer, register = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(diagnostics.faulthandler, "dump_traceback_later", dump)
    monkeypatch.setattr(diagnostics.faulthandler, "cancel_dump_traceback_later", cancel)
    monkeypatch.setattr(diagnostics.threading, "Timer", timer)
    monkeypatch.setattr(diagnostics.atexit, "register", register)
    armed = diagnostics.arm_startup_diagnostics()
    assert armed is not None
    dump.assert_called_once_with(15, repeat=True, file=stream, exit=False)
    assert timer.call_args.args == (75.0, armed.close)
    assert timer.return_value.daemon is True
    timer.return_value.start.assert_called_once()
    register.assert_called_once_with(armed.close)
    assert "phase=module_import" in stream.getvalue()
    assert f"pid={os.getpid()}" in stream.getvalue()
    assert repr(sys.executable) in stream.getvalue()
    # Automatic window expiry and command cleanup can both call close.
    timer.call_args.args[1]()
    armed.close()
    cancel.assert_called_once()
    timer.return_value.cancel.assert_called_once()


@pytest.mark.parametrize("stderr", [None, io.StringIO()])
def test_unusable_stderr_never_stops_startup(monkeypatch, stderr):
    monkeypatch.setenv(diagnostics.STACK_INTERVAL_ENV, "15")
    monkeypatch.setattr(diagnostics.sys, "__stderr__", stderr)
    assert diagnostics.arm_startup_diagnostics() is None


def test_opt_in_sampler_observes_hang_without_exiting_process():
    code = (
        "from drover.server.startup_diagnostics import arm_startup_diagnostics\n"
        "import time\n"
        "armed = arm_startup_diagnostics()\n"
        "def blocked_preimport_fixture(): time.sleep(1.2)\n"
        "blocked_preimport_fixture()\n"
        "armed.close()\n"
        "print('fixture completed')\n"
    )
    env = {**os.environ, diagnostics.STACK_INTERVAL_ENV: "1"}
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0
    assert "blocked_preimport_fixture" in result.stderr
    assert "Timeout" in result.stderr
    assert "diagnostic_window_closed" in result.stderr
    assert "fixture completed" in result.stdout


def test_server_arms_sampler_before_heavy_imports():
    code = (
        "import builtins, time\n"
        "original_import = builtins.__import__\n"
        "def blocked_server_import_fixture(name, *args, **kwargs):\n"
        "    if name == 'duckdb':\n"
        "        time.sleep(1.2)\n"
        "        raise RuntimeError('synthetic stop before server construction')\n"
        "    return original_import(name, *args, **kwargs)\n"
        "builtins.__import__ = blocked_server_import_fixture\n"
        "try:\n"
        "    import drover.server.__main__\n"
        "except RuntimeError:\n"
        "    print('fixture stopped before server construction')\n"
    )
    env = {**os.environ, diagnostics.STACK_INTERVAL_ENV: "1"}
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0
    assert "phase=module_import" in result.stderr
    assert "blocked_server_import_fixture" in result.stderr
    assert "Timeout" in result.stderr
    assert "diagnostic_window_closed" in result.stderr


def test_pg_bootstrap_failure_logs_no_query_or_row_payload(caplog):
    from drover.server.postgres_schema import bootstrap_postgres_control_store

    con = Mock()
    con.execute.side_effect = [None, ValueError("secret-row-and-query-payload"), None]
    store = SimpleNamespace(
        config=SimpleNamespace(schema="fixture", statement_timeout_seconds=5),
        connection=lambda: nullcontext(con),
    )
    with pytest.raises(ValueError):
        bootstrap_postgres_control_store(store)
    assert "ValueError" in caplog.text
    assert "secret-row-and-query-payload" not in caplog.text
    assert con.execute.call_args.args == ("ROLLBACK",)


def test_phase_logs_include_process_and_runtime_identity(caplog):
    from drover.server.__main__ import _startup_phase

    with caplog.at_level("INFO"):
        with _startup_phase("identity_probe"):
            pass
    for record in caplog.records:
        if "identity_probe" in record.message:
            assert f"pid={os.getpid()}" in record.message
            assert repr(sys.executable) in record.message
            assert "version=" in record.message
