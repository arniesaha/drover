"""Tests for the CursorStore."""

import json
import multiprocessing
import time
from pathlib import Path

import pytest

from drover.collect.cursor import CursorLocked, CursorStore


def test_read_missing_returns_empty(tmp_path: Path) -> None:
    store = CursorStore(state_dir=tmp_path)
    assert store.read("claude_code") == {}


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    store = CursorStore(state_dir=tmp_path)
    payload = {
        "watermark_iso": "2026-05-09T01:00:00+00:00",
        "last_run_iso": "2026-05-09T01:01:00+00:00",
    }
    store.write("claude_code", payload)
    assert store.read("claude_code") == payload


def test_write_creates_state_dir(tmp_path: Path) -> None:
    state_dir = tmp_path / "nested" / "state"
    store = CursorStore(state_dir=state_dir)
    store.write("hermes", {"watermark_iso": "2026-05-09T00:00:00+00:00"})
    assert (state_dir / "hermes.cursor").exists()


def test_atomic_write_leaves_no_partial_file(tmp_path: Path) -> None:
    """If a .tmp file exists from a prior crashed write, read() must still return last good payload."""
    store = CursorStore(state_dir=tmp_path)
    store.write("openclaw", {"watermark_iso": "2026-05-09T00:00:00+00:00"})
    # simulate a crashed write: orphan .tmp with bogus content
    (tmp_path / "openclaw.cursor.tmp").write_text("CORRUPT_PARTIAL_WRITE")
    assert store.read("openclaw") == {"watermark_iso": "2026-05-09T00:00:00+00:00"}


def _hold_lock(
    state_dir: str, source: str, hold_seconds: float, ready_evt, release_evt
) -> None:
    store = CursorStore(state_dir=Path(state_dir))
    with store.lock(source):
        ready_evt.set()
        # Wait until parent says we can release
        release_evt.wait(timeout=hold_seconds + 5)


def test_concurrent_lock_raises(tmp_path: Path) -> None:
    """Second lock attempt on the same source must raise CursorLocked."""
    ctx = multiprocessing.get_context("fork")
    ready = ctx.Event()
    release = ctx.Event()
    proc = ctx.Process(
        target=_hold_lock,
        args=(str(tmp_path), "claude_code", 5.0, ready, release),
    )
    proc.start()
    try:
        assert ready.wait(timeout=5.0), "child failed to acquire lock"
        store = CursorStore(state_dir=tmp_path)
        with pytest.raises(CursorLocked):
            with store.lock("claude_code"):
                pass
    finally:
        release.set()
        proc.join(timeout=5.0)
        if proc.is_alive():
            proc.terminate()
            proc.join()


def test_lock_released_after_context_exit(tmp_path: Path) -> None:
    store = CursorStore(state_dir=tmp_path)
    with store.lock("hermes"):
        pass
    # second acquisition in same process must work fine
    with store.lock("hermes"):
        pass


def test_corrupt_cursor_file_returns_empty(tmp_path: Path) -> None:
    store = CursorStore(state_dir=tmp_path)
    (tmp_path / "claude_code.cursor").write_text("not valid json{{{")
    assert store.read("claude_code") == {}
