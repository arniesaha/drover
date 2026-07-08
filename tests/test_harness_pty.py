"""Tests for Meta Harness PTY sessions."""

from __future__ import annotations

import time

from drover.server.harness.pty import PtySessionManager


def _read_until(manager: PtySessionManager, session_id: str, needle: str) -> str:
    deadline = time.time() + 3
    chunks: list[str] = []
    while time.time() < deadline:
        chunks.append(
            manager.read(session_id, timeout_s=0.1).decode("utf-8", errors="ignore")
        )
        text = "".join(chunks)
        if needle in text:
            return text
    return "".join(chunks)


def test_pty_manager_runs_shell_command(tmp_path):
    manager = PtySessionManager()
    session = manager.start(
        session_id="pty-one",
        command=["/bin/sh", "-lc", "printf NEXUS_OK"],
        cwd=tmp_path,
    )
    try:
        assert session.pid > 0
        assert "NEXUS_OK" in _read_until(manager, "pty-one", "NEXUS_OK")
    finally:
        manager.terminate("pty-one")


def test_pty_manager_child_has_controlling_terminal(tmp_path):
    manager = PtySessionManager()
    command = [
        "/bin/sh",
        "-lc",
        'python3 -c \'import os; print("ISATTY", os.isatty(0)); print("PGRP", os.tcgetpgrp(0))\'',
    ]
    manager.start(session_id="pty-controlling", command=command, cwd=tmp_path)
    try:
        output = _read_until(manager, "pty-controlling", "PGRP")
        assert "ISATTY True" in output
        assert "PGRP" in output
    finally:
        manager.terminate("pty-controlling")


def test_pty_manager_writes_to_interactive_shell(tmp_path):
    manager = PtySessionManager()
    manager.start(session_id="pty-two", command=["/bin/sh"], cwd=tmp_path)
    try:
        manager.write("pty-two", "echo INTERACTIVE_OK\n")
        assert "INTERACTIVE_OK" in _read_until(manager, "pty-two", "INTERACTIVE_OK")
    finally:
        manager.terminate("pty-two")


def test_pty_manager_reaps_exited_process_and_rejects_late_write(tmp_path):
    manager = PtySessionManager()
    manager.start(
        session_id="pty-exit",
        command=["/bin/sh", "-lc", "printf BYE"],
        cwd=tmp_path,
    )

    assert "BYE" in _read_until(manager, "pty-exit", "BYE")
    deadline = time.time() + 3
    reaped = []
    while time.time() < deadline and not reaped:
        reaped = manager.reap_exited()
        time.sleep(0.05)

    assert [session.session_id for session in reaped] == ["pty-exit"]
    assert manager.list_sessions() == []
    try:
        manager.write("pty-exit", "echo TOO_LATE\n")
    except KeyError:
        pass
    else:
        raise AssertionError("write to exited PTY should be rejected cleanly")
