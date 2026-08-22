"""The data volume can become unreadable while the process still starts.

Captured live on 2026-08-22 (#265): macOS began denying launchd-spawned
processes access to the external volume holding ``~/.drover``. Every daemon
blocked forever inside the ``open()`` of its own config, alive at three file
descriptors with no CPU and no log line. ``KeepAlive`` cannot help a process
that never dies, and a health check reads "running".

A FIFO reproduces that exact syscall: opening one for reading blocks until a
writer appears, which is never here.
"""

import os
import threading
import time
from pathlib import Path

import pytest

from drover.config import ConfigUnreadable, read_config_text


def _blocking_path(tmp_path: Path) -> Path:
    fifo = tmp_path / "config.toml"
    os.mkfifo(fifo)
    return fifo


def test_unreadable_config_gives_up_instead_of_blocking_forever(tmp_path):
    started = time.monotonic()
    with pytest.raises(ConfigUnreadable):
        read_config_text(_blocking_path(tmp_path), timeout_seconds=0.5)
    assert time.monotonic() - started < 5, "the point is that it returns"


def test_the_error_names_the_path_so_the_log_line_is_actionable(tmp_path):
    path = _blocking_path(tmp_path)
    with pytest.raises(ConfigUnreadable) as caught:
        read_config_text(path, timeout_seconds=0.5)
    assert str(path) in str(caught.value)


def test_a_readable_config_is_returned_unchanged(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[server]\nmetrics_host = "127.0.0.1"\n')
    assert read_config_text(path, timeout_seconds=5) == path.read_text()


def test_a_missing_config_still_raises_FileNotFoundError(tmp_path):
    # The absent-file case is not this failure and must keep its own error:
    # callers distinguish "not configured yet" from "cannot be read".
    with pytest.raises(FileNotFoundError):
        read_config_text(tmp_path / "nope.toml", timeout_seconds=5)


def test_the_abandoned_reader_never_blocks_process_exit(tmp_path):
    # The worker stays stuck on the FIFO for as long as the FIFO exists, so
    # nothing may be left holding the process open: the whole point is to exit
    # and be restarted. A raw `_thread` worker is never registered with
    # `threading`, so it cannot be joined and cannot delay shutdown.
    before = threading.active_count()
    with pytest.raises(ConfigUnreadable):
        read_config_text(_blocking_path(tmp_path), timeout_seconds=0.5)
    leaked = [t for t in threading.enumerate() if not t.daemon]
    assert len(leaked) <= before, "an abandoned reader must not be joinable"
