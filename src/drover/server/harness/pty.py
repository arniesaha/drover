"""PTY-backed local process sessions for Drover Meta Harness."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import select
import shlex
import signal
import subprocess
from contextlib import suppress
from typing import Mapping


@dataclass(frozen=True)
class PtySession:
    session_id: str
    command: tuple[str, ...]
    cwd: Path | None
    pid: int


@dataclass
class _RunningPty:
    public: PtySession
    process: subprocess.Popen[bytes]
    master_fd: int


class PtySessionManager:
    """Owns local PTY processes for one harnessd instance."""

    def __init__(self) -> None:
        self._sessions: dict[str, _RunningPty] = {}

    def start(
        self,
        *,
        session_id: str,
        command: str | list[str] | tuple[str, ...],
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        rows: int = 24,
        cols: int = 80,
    ) -> PtySession:
        if session_id in self._sessions:
            raise ValueError(f"PTY session already exists: {session_id}")
        argv = _normalize_command(command)
        cwd_path = Path(cwd).expanduser() if cwd else None
        if cwd_path is not None and not cwd_path.is_dir():
            raise FileNotFoundError(f"cwd does not exist: {cwd_path}")

        child_env = {**os.environ, **dict(env or {})}
        child_env.setdefault("TERM", "xterm-256color")
        child_env.setdefault("COLORTERM", "truecolor")

        master_fd, slave_fd = os.openpty()
        _resize_pty(slave_fd, rows=rows, cols=cols)
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=str(cwd_path) if cwd_path else None,
                env=child_env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=_make_child_preexec(slave_fd),
            )
        finally:
            os.close(slave_fd)
        os.set_blocking(master_fd, False)
        public = PtySession(
            session_id=session_id,
            command=argv,
            cwd=cwd_path,
            pid=process.pid,
        )
        self._sessions[session_id] = _RunningPty(
            public=public,
            process=process,
            master_fd=master_fd,
        )
        return public

    def get(self, session_id: str) -> PtySession | None:
        self.reap_exited()
        running = self._sessions.get(session_id)
        return running.public if running else None

    def list_sessions(self) -> list[PtySession]:
        self.reap_exited()
        return [running.public for running in self._sessions.values()]

    def is_alive(self, session_id: str) -> bool:
        running = self._sessions.get(session_id)
        if running is None:
            return False
        if running.process.poll() is None:
            return True
        self._cleanup_running(session_id, running)
        return False

    def write(self, session_id: str, data: str | bytes) -> None:
        running = self._require(session_id)
        if running.process.poll() is not None:
            self._cleanup_running(session_id, running)
            raise KeyError(f"unknown PTY session: {session_id}")
        payload = data.encode("utf-8") if isinstance(data, str) else data
        try:
            os.write(running.master_fd, payload)
        except OSError as exc:
            self._cleanup_running(session_id, running)
            raise KeyError(f"unknown PTY session: {session_id}") from exc

    def resize(self, session_id: str, *, rows: int, cols: int) -> None:
        running = self._require(session_id)
        if running.process.poll() is not None:
            self._cleanup_running(session_id, running)
            raise KeyError(f"unknown PTY session: {session_id}")
        _resize_pty(running.master_fd, rows=rows, cols=cols)

    def read(
        self,
        session_id: str,
        *,
        max_bytes: int = 4096,
        timeout_s: float = 0.1,
    ) -> bytes:
        running = self._require(session_id)
        if running.process.poll() is not None:
            self._cleanup_running(session_id, running)
            return b""
        readable, _, _ = select.select([running.master_fd], [], [], timeout_s)
        if not readable:
            return b""
        try:
            return os.read(running.master_fd, max_bytes)
        except BlockingIOError:
            return b""
        except OSError:
            if running.process.poll() is not None:
                self._cleanup_running(session_id, running)
            return b""

    def terminate(self, session_id: str, *, timeout_s: float = 2.0) -> bool:
        running = self._sessions.pop(session_id, None)
        if running is None:
            return False
        with suppress(OSError):
            os.close(running.master_fd)
        _terminate_process(running.process, timeout_s=timeout_s)
        return True

    def reap_exited(self) -> list[PtySession]:
        """Drop exited PTYs from the live inventory and return their public records."""
        reaped: list[PtySession] = []
        for session_id, running in list(self._sessions.items()):
            if running.process.poll() is not None:
                reaped.append(running.public)
                self._cleanup_running(session_id, running)
        return reaped

    def close_all(self) -> None:
        for session_id in list(self._sessions):
            self.terminate(session_id)

    def _require(self, session_id: str) -> _RunningPty:
        running = self._sessions.get(session_id)
        if running is None:
            raise KeyError(f"unknown PTY session: {session_id}")
        return running

    def _cleanup_running(self, session_id: str, running: _RunningPty) -> None:
        current = self._sessions.get(session_id)
        if current is not running:
            return
        self._sessions.pop(session_id, None)
        with suppress(OSError):
            os.close(running.master_fd)


def _terminate_process(process: subprocess.Popen[bytes], *, timeout_s: float) -> None:
    if process.poll() is not None:
        return
    _signal_process(process, signal.SIGTERM)
    try:
        process.wait(timeout=timeout_s)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_process(process, signal.SIGKILL)
    process.wait(timeout=timeout_s)


def _signal_process(process: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except (PermissionError, ProcessLookupError):
        pass
    try:
        process.send_signal(sig)
    except ProcessLookupError:
        return


def _make_child_preexec(slave_fd: int):
    def preexec() -> None:
        os.setsid()
        try:
            import fcntl
            import termios

            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        except Exception:
            return

    return preexec


def _normalize_command(command: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(command, str):
        argv = tuple(shlex.split(command))
    else:
        argv = tuple(command)
    if not argv:
        raise ValueError("command must not be empty")
    return argv


def _resize_pty(fd: int, *, rows: int, cols: int) -> None:
    try:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        return
