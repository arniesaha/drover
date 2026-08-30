"""One bounded, private subprocess boundary for every Pond command."""

from __future__ import annotations

import ctypes
import math
import os
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Mapping, Sequence

POND_VERSION = "0.16.3"

_POND_VERSION_TOKENS = ("pond", POND_VERSION)
_POND_RELEASE_COMMIT = "23c7d0e"
_POND_RELEASE_TARGETS = frozenset(
    {
        "aarch64-linux",
        "aarch64-macos",
        "x86_64-linux",
        "x86_64-windows",
    }
)
_MAX_CHILD_STREAM_BYTES = 32 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_POLL_SECONDS = 0.01
_SAMPLE_SECONDS = 0.05
_MAX_PROGRESS_CALLBACK_SECONDS = 1.0
_TERMINATE_GRACE_SECONDS = 0.25
_MAX_COMMAND_TIMEOUT_SECONDS = 1800.0
_SAMPLER_OUTPUT_BYTES = 1024 * 1024
_SAMPLER_TIMEOUT_SECONDS = 1.0
_PROC_FILE_BYTES = 64 * 1024
_MAX_PROC_ENTRIES = 131_072
_EXECUTABLE_COPY_CHUNK_BYTES = 1024 * 1024
_LABEL = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SWAP_USAGE = re.compile(rb"\bused\s*=\s*([0-9]+(?:\.[0-9]+)?)([KMGTP])\b")
_PROC_ROOT = Path("/proc")
_ERROR_CATEGORIES = frozenset(
    {
        "artifact",
        "binary",
        "environment",
        "resource",
        "size",
        "subprocess",
        "temporary",
        "timeout",
    }
)


class PondProcessError(ValueError):
    """A fixed, non-interpolated Pond process failure category."""

    def __init__(self, category: str) -> None:
        safe_category = category if category in _ERROR_CATEGORIES else "subprocess"
        self.category = safe_category
        super().__init__(safe_category)


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    max_rss_bytes: int
    max_physical_bytes: int
    max_swap_growth_bytes: int

    def __post_init__(self) -> None:
        for value, allow_zero in (
            (self.max_rss_bytes, False),
            (self.max_physical_bytes, False),
            (self.max_swap_growth_bytes, True),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < (0 if allow_zero else 1)
            ):
                raise PondProcessError("resource")


@dataclass(frozen=True, slots=True)
class ResourceSample:
    rss_bytes: int
    physical_bytes: int | None
    swap_used_bytes: int

    def __post_init__(self) -> None:
        values = (self.rss_bytes, self.swap_used_bytes)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise PondProcessError("resource")
        if self.physical_bytes is not None and (
            isinstance(self.physical_bytes, bool)
            or not isinstance(self.physical_bytes, int)
            or self.physical_bytes < 0
        ):
            raise PondProcessError("resource")


@dataclass(frozen=True, slots=True)
class PondProcessResult:
    returncode: int
    duration_ms: int
    peak_rss_bytes: int
    peak_physical_bytes: int | None
    swap_delta_bytes: int
    stdout_path: Path
    stderr_path: Path


@dataclass(frozen=True, slots=True, repr=False)
class PondResourceEvidence:
    """Aggregate-only resource maxima, with unknown physical use preserved."""

    peak_rss_bytes: int
    peak_physical_bytes: int | None
    swap_delta_bytes: int

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.peak_rss_bytes, self.swap_delta_bytes)
        ):
            raise PondProcessError("resource")
        if self.peak_physical_bytes is not None and (
            isinstance(self.peak_physical_bytes, bool)
            or not isinstance(self.peak_physical_bytes, int)
            or self.peak_physical_bytes < 0
        ):
            raise PondProcessError("resource")


def _process_resource_evidence(result: PondProcessResult) -> PondResourceEvidence:
    if type(result) is not PondProcessResult:
        raise PondProcessError("resource")
    return PondResourceEvidence(
        result.peak_rss_bytes,
        result.peak_physical_bytes,
        result.swap_delta_bytes,
    )


def _aggregate_resource_evidence(
    *evidence: PondResourceEvidence,
) -> PondResourceEvidence:
    if not evidence or any(
        type(value) is not PondResourceEvidence for value in evidence
    ):
        raise PondProcessError("resource")
    physical = tuple(value.peak_physical_bytes for value in evidence)
    return PondResourceEvidence(
        max(value.peak_rss_bytes for value in evidence),
        None if any(value is None for value in physical) else max(physical),
        max(value.swap_delta_bytes for value in evidence),
    )


def _executable_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


class _PinnedPondExecutable:
    __slots__ = (
        "_artifact_descriptor",
        "_artifact_directory_descriptor",
        "_artifact_directory_identity",
        "_artifact_identity",
        "_artifact_path",
        "_closed",
        "_descriptor",
        "_identity",
        "path",
    )

    def __init__(
        self,
        path: Path,
        descriptor: int,
        identity: tuple[int, ...],
    ) -> None:
        self.path = path
        self._descriptor = descriptor
        self._identity = identity
        self._artifact_descriptor = -1
        self._artifact_directory_descriptor = -1
        self._artifact_directory_identity: tuple[int, ...] | None = None
        self._artifact_identity: tuple[int, ...] | None = None
        self._artifact_path: Path | None = None
        self._closed = False

    def __enter__(self) -> _PinnedPondExecutable:
        self.require_same()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def require_same(self) -> None:
        try:
            if self._closed:
                raise ValueError
            opened = os.fstat(self._descriptor)
            lexical = os.stat(self.path, follow_symlinks=False)
            if (
                _executable_identity(opened) != self._identity
                or _executable_identity(lexical) != self._identity
                or not stat.S_ISREG(opened.st_mode)
                or not os.access(self.path, os.X_OK)
            ):
                raise ValueError
            if self._artifact_descriptor >= 0:
                if (
                    self._artifact_directory_descriptor < 0
                    or self._artifact_directory_identity is None
                    or self._artifact_identity is None
                    or self._artifact_path is None
                ):
                    raise ValueError
                self._require_artifact_directory_mode(0o700)
                artifact_opened = os.fstat(self._artifact_descriptor)
                artifact_lexical = os.stat(
                    self._artifact_path,
                    follow_symlinks=False,
                )
                if (
                    _executable_identity(artifact_opened) != self._artifact_identity
                    or _executable_identity(artifact_lexical) != self._artifact_identity
                    or not stat.S_ISREG(artifact_opened.st_mode)
                    or artifact_opened.st_uid != os.geteuid()
                    or stat.S_IMODE(artifact_opened.st_mode) != 0o500
                ):
                    raise ValueError
        except (OSError, TypeError, ValueError):
            raise PondProcessError("binary") from None

    def execution_path(self, directory: Path) -> Path:
        self.require_same()
        if self._artifact_descriptor < 0:
            self._create_execution_artifact(directory)
        self.require_same()
        if self._artifact_path is None:
            raise PondProcessError("binary")
        return self._artifact_path

    def begin_spawn(self) -> None:
        self.require_same()
        try:
            os.fchmod(self._artifact_directory_descriptor, 0o500)
            self._require_artifact_directory_mode(0o500)
        except (OSError, TypeError, ValueError):
            raise PondProcessError("binary") from None

    def finish_spawn(self) -> None:
        try:
            os.fchmod(self._artifact_directory_descriptor, 0o700)
            self._require_artifact_directory_mode(0o700)
        except (OSError, TypeError, ValueError):
            raise PondProcessError("binary") from None
        self.require_same()

    def _require_artifact_directory_mode(self, mode: int) -> None:
        if self._artifact_directory_identity is None or self._artifact_path is None:
            raise ValueError
        opened = os.fstat(self._artifact_directory_descriptor)
        lexical = os.stat(self._artifact_path.parent, follow_symlinks=False)
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_uid,
            opened.st_gid,
        )
        lexical_identity = (
            lexical.st_dev,
            lexical.st_ino,
            lexical.st_uid,
            lexical.st_gid,
        )
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened_identity != self._artifact_directory_identity
            or lexical_identity != opened_identity
            or stat.S_IMODE(opened.st_mode) != mode
            or stat.S_IMODE(lexical.st_mode) != mode
        ):
            raise ValueError

    def _create_execution_artifact(self, directory: Path) -> None:
        run_directory_descriptor = -1
        artifact_directory_descriptor = -1
        writer = -1
        reader = -1
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            run_directory_descriptor = os.open(directory, flags)
            directory_opened = os.fstat(run_directory_descriptor)
            directory_lexical = os.stat(directory, follow_symlinks=False)
            if (
                not stat.S_ISDIR(directory_opened.st_mode)
                or directory_opened.st_uid != os.geteuid()
                or stat.S_IMODE(directory_opened.st_mode) != 0o700
                or (directory_opened.st_dev, directory_opened.st_ino)
                != (directory_lexical.st_dev, directory_lexical.st_ino)
            ):
                raise ValueError
            artifact_directory_name = f".drover-pond-tool-{secrets.token_hex(16)}"
            os.mkdir(
                artifact_directory_name,
                0o700,
                dir_fd=run_directory_descriptor,
            )
            artifact_directory_descriptor = os.open(
                artifact_directory_name,
                flags,
                dir_fd=run_directory_descriptor,
            )
            os.fchmod(artifact_directory_descriptor, 0o700)
            artifact_directory_opened = os.fstat(artifact_directory_descriptor)
            artifact_directory_lexical = os.stat(
                artifact_directory_name,
                dir_fd=run_directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(artifact_directory_opened.st_mode)
                or artifact_directory_opened.st_uid != os.geteuid()
                or stat.S_IMODE(artifact_directory_opened.st_mode) != 0o700
                or (
                    artifact_directory_opened.st_dev,
                    artifact_directory_opened.st_ino,
                )
                != (
                    artifact_directory_lexical.st_dev,
                    artifact_directory_lexical.st_ino,
                )
            ):
                raise ValueError
            name = f".drover-pond-executable-{secrets.token_hex(16)}"
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                create_flags |= os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                create_flags |= os.O_CLOEXEC
            writer = os.open(
                name,
                create_flags,
                0o500,
                dir_fd=artifact_directory_descriptor,
            )
            os.fchmod(writer, 0o500)
            source = os.fstat(self._descriptor)
            if _executable_identity(source) != self._identity:
                raise ValueError
            offset = 0
            while offset < source.st_size:
                chunk = os.pread(
                    self._descriptor,
                    min(_EXECUTABLE_COPY_CHUNK_BYTES, source.st_size - offset),
                    offset,
                )
                if not chunk:
                    raise ValueError
                remaining = memoryview(chunk)
                while remaining:
                    written = os.write(writer, remaining)
                    if written <= 0:
                        raise ValueError
                    remaining = remaining[written:]
                offset += len(chunk)
            if os.pread(self._descriptor, 1, source.st_size):
                raise ValueError
            os.fsync(writer)
            if _executable_identity(os.fstat(self._descriptor)) != self._identity:
                raise ValueError
            read_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                read_flags |= os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                read_flags |= os.O_CLOEXEC
            reader = os.open(name, read_flags, dir_fd=artifact_directory_descriptor)
            artifact = os.fstat(reader)
            lexical = os.stat(
                name,
                dir_fd=artifact_directory_descriptor,
                follow_symlinks=False,
            )
            identity = _executable_identity(artifact)
            if (
                identity != _executable_identity(os.fstat(writer))
                or identity != _executable_identity(lexical)
                or not stat.S_ISREG(artifact.st_mode)
                or artifact.st_uid != os.geteuid()
                or stat.S_IMODE(artifact.st_mode) != 0o500
                or artifact.st_nlink != 1
                or artifact.st_size != source.st_size
            ):
                raise ValueError
            self._artifact_descriptor = reader
            self._artifact_directory_descriptor = artifact_directory_descriptor
            self._artifact_directory_identity = (
                artifact_directory_opened.st_dev,
                artifact_directory_opened.st_ino,
                artifact_directory_opened.st_uid,
                artifact_directory_opened.st_gid,
            )
            self._artifact_identity = identity
            self._artifact_path = directory / artifact_directory_name / name
            reader = -1
            artifact_directory_descriptor = -1
        except (OSError, TypeError, ValueError):
            raise PondProcessError("binary") from None
        finally:
            for descriptor in (
                reader,
                writer,
                artifact_directory_descriptor,
                run_directory_descriptor,
            ):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in (
            self._artifact_descriptor,
            self._artifact_directory_descriptor,
            self._descriptor,
        ):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _pin_pond_executable(binary: Path) -> _PinnedPondExecutable:
    """Retain one canonical executable descriptor and full stable identity."""
    descriptor = -1
    try:
        path = Path(binary).resolve(strict=True)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        lexical = os.stat(path, follow_symlinks=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise PondProcessError("binary") from None
    if (
        not path.is_absolute()
        or not stat.S_ISREG(metadata.st_mode)
        or _executable_identity(metadata) != _executable_identity(lexical)
        or not os.access(path, os.X_OK)
    ):
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise PondProcessError("binary")
    executable = _PinnedPondExecutable(
        path,
        descriptor,
        _executable_identity(metadata),
    )
    executable.require_same()
    return executable


def require_pinned_pond(binary: Path) -> Path:
    """Resolve and validate one executable by descriptor and canonical path."""
    with _pin_pond_executable(binary) as executable:
        return executable.path


def is_pinned_pond_version(tokens: tuple[str, ...]) -> bool:
    """Return whether Pond reported the one approved release identity."""
    return (
        len(tokens) == 4
        and tokens[:2] == _POND_VERSION_TOKENS
        and tokens[2] == f"({_POND_RELEASE_COMMIT}"
        and tokens[3].endswith(")")
        and tokens[3][:-1] in _POND_RELEASE_TARGETS
    )


def pond_child_environment(overrides: Mapping[str, str] | None) -> dict[str, str]:
    """Build a child environment without an inherited Pond storage selector."""
    try:
        child = dict(os.environ)
        if overrides is not None:
            child.update(overrides)
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or "\0" in key
            or "=" in key
            or "\0" in value
            for key, value in child.items()
        ):
            raise PondProcessError("environment")
        child.pop("POND_STORAGE_PATH", None)
        return child
    except PondProcessError:
        raise
    except (TypeError, ValueError):
        raise PondProcessError("environment") from None


def sample_process_group(process_group: int) -> ResourceSample:
    """Measure one Pond process group with bounded platform-specific reads."""
    if (
        isinstance(process_group, bool)
        or not isinstance(process_group, int)
        or process_group <= 0
    ):
        raise PondProcessError("resource")
    if sys.platform == "darwin":
        return _sample_darwin_process_group(process_group)
    if sys.platform.startswith("linux"):
        return _sample_linux_process_group(process_group)
    raise PondProcessError("resource")


def run_pond_process(
    binary: Path | _PinnedPondExecutable,
    arguments: Sequence[str],
    *,
    timeout_seconds: float,
    run_directory: Path,
    label: str,
    env: Mapping[str, str] | None = None,
    artifact_path: Path | None = None,
    resource_limits: ResourceLimits | None = None,
    resource_sampler: Callable[[int], ResourceSample] = sample_process_group,
    progress_callback: Callable[[], None] | None = None,
) -> PondProcessResult:
    """Run one pinned Pond command once, with bounded private evidence.

    A progress callback must bound its own blocking operations.
    """
    if type(binary) is _PinnedPondExecutable:
        return _run_pinned_pond_process(
            binary,
            arguments,
            timeout_seconds=timeout_seconds,
            run_directory=run_directory,
            label=label,
            env=env,
            artifact_path=artifact_path,
            resource_limits=resource_limits,
            resource_sampler=resource_sampler,
            progress_callback=progress_callback,
        )
    with _pin_pond_executable(binary) as executable:
        return _run_pinned_pond_process(
            executable,
            arguments,
            timeout_seconds=timeout_seconds,
            run_directory=run_directory,
            label=label,
            env=env,
            artifact_path=artifact_path,
            resource_limits=resource_limits,
            resource_sampler=resource_sampler,
            progress_callback=progress_callback,
        )


def _run_pinned_pond_process(
    binary: _PinnedPondExecutable,
    arguments: Sequence[str],
    *,
    timeout_seconds: float,
    run_directory: Path,
    label: str,
    env: Mapping[str, str] | None = None,
    artifact_path: Path | None = None,
    resource_limits: ResourceLimits | None = None,
    resource_sampler: Callable[[int], ResourceSample] = sample_process_group,
    progress_callback: Callable[[], None] | None = None,
) -> PondProcessResult:
    binary.require_same()
    arguments_tuple = _require_arguments(arguments)
    timeout = _require_timeout(timeout_seconds)
    directory = _require_run_directory(run_directory)
    executable = binary.execution_path(directory)
    safe_label = _require_label(label)
    child_environment = pond_child_environment(env)
    monitored_artifact = _require_artifact_path(artifact_path, directory)
    _check_artifact_during_run(monitored_artifact)
    stdout_path = directory / f"{safe_label}.stdout"
    stderr_path = directory / f"{safe_label}.stderr"
    started_at = time.monotonic()
    deadline = started_at + timeout

    with (
        _open_private_output(stdout_path) as stdout_file,
        _open_private_output(stderr_path) as stderr_file,
    ):
        process: subprocess.Popen[bytes] | None = None
        try:
            binary.require_same()
            try:
                binary.begin_spawn()
                process = subprocess.Popen(
                    (str(binary.path), *arguments_tuple),
                    executable=str(executable),
                    env=child_environment,
                    shell=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    umask=0o077,
                )
            finally:
                binary.finish_spawn()
        except PondProcessError:
            if process is not None:
                _stop_process(process, process.pid)
            raise
        except (OSError, TypeError, ValueError):
            if process is not None:
                _stop_process(process, process.pid)
            raise PondProcessError("subprocess") from None

        assert process is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process_group = process.pid
        selector: selectors.BaseSelector | None = None
        peak_rss = 0
        peak_physical: int | None = None
        physical_available = True
        initial_swap: int | None = None
        peak_swap = 0
        cleanup_required = True
        try:
            binary.require_same()
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ, stdout_file)
            selector.register(process.stderr, selectors.EVENT_READ, stderr_file)
            counts = {stdout_file: 0, stderr_file: 0}
            initial_sample = _take_resource_sample_before_deadline(
                resource_sampler,
                process_group,
                deadline,
            )
            peak_rss = initial_sample.rss_bytes
            peak_physical = initial_sample.physical_bytes
            physical_available = peak_physical is not None
            initial_swap = initial_sample.swap_used_bytes
            peak_swap = initial_swap
            _enforce_resource_limits(initial_sample, initial_swap, resource_limits)
            _call_progress_before_deadline(progress_callback, deadline)
            next_sample = time.monotonic() + _SAMPLE_SECONDS
            while selector.get_map() or not _leader_exited_without_reap(process):
                binary.require_same()
                _check_artifact_during_run(monitored_artifact)
                now = time.monotonic()
                if now >= next_sample:
                    sample = _take_resource_sample_before_deadline(
                        resource_sampler,
                        process_group,
                        deadline,
                    )
                    peak_rss = max(peak_rss, sample.rss_bytes)
                    if sample.physical_bytes is None:
                        peak_physical = None
                        physical_available = False
                    elif physical_available:
                        peak_physical = max(peak_physical or 0, sample.physical_bytes)
                    if initial_swap is None:
                        initial_swap = sample.swap_used_bytes
                        peak_swap = initial_swap
                    else:
                        peak_swap = max(peak_swap, sample.swap_used_bytes)
                    _enforce_resource_limits(sample, initial_swap, resource_limits)
                    _call_progress_before_deadline(progress_callback, deadline)
                    next_sample = time.monotonic() + _SAMPLE_SECONDS
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PondProcessError("timeout")
                wait_seconds = min(
                    remaining,
                    _POLL_SECONDS,
                    max(0.0, next_sample - time.monotonic()),
                )
                ready = selector.select(wait_seconds)
                for key, _ in ready:
                    sink = key.data
                    capacity = _MAX_CHILD_STREAM_BYTES - counts[sink]
                    chunk = os.read(key.fd, min(_CHUNK_BYTES, capacity + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if len(chunk) > capacity:
                        if capacity:
                            sink.write(chunk[:capacity])
                        raise PondProcessError("size")
                    sink.write(chunk)
                    counts[sink] += len(chunk)
            _check_artifact_during_run(monitored_artifact)
            _call_progress_before_deadline(progress_callback, deadline)
            returncode = _stop_process(process, process_group)
            cleanup_required = False
            binary.require_same()
            _check_artifact_during_run(monitored_artifact)
            return PondProcessResult(
                returncode=returncode,
                duration_ms=max(0, round((time.monotonic() - started_at) * 1000)),
                peak_rss_bytes=peak_rss,
                peak_physical_bytes=peak_physical,
                swap_delta_bytes=max(0, peak_swap - (initial_swap or 0)),
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        except PondProcessError:
            if cleanup_required:
                _stop_process(process, process_group)
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            if cleanup_required:
                _stop_process(process, process_group)
            raise PondProcessError("subprocess") from None
        except BaseException:
            if cleanup_required:
                _stop_process(process, process_group)
            raise
        finally:
            if selector is not None:
                selector.close()
            process.stdout.close()
            process.stderr.close()


def _require_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
    try:
        if isinstance(arguments, (str, bytes)):
            raise PondProcessError("subprocess")
        values = tuple(arguments)
    except (TypeError, ValueError):
        raise PondProcessError("subprocess") from None
    if any(not isinstance(value, str) or "\0" in value for value in values):
        raise PondProcessError("subprocess")
    return values


def _require_timeout(timeout_seconds: float) -> float:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > _MAX_COMMAND_TIMEOUT_SECONDS
    ):
        raise PondProcessError("timeout")
    return float(timeout_seconds)


def _require_run_directory(run_directory: Path) -> Path:
    try:
        path = Path(os.path.abspath(os.fspath(run_directory)))
        if path.exists():
            metadata = path.lstat()
        else:
            path.parent.resolve(strict=True)
            path.mkdir(mode=0o700)
            metadata = path.lstat()
        resolved = path.resolve(strict=True)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or resolved.stat().st_ino != metadata.st_ino
            or resolved.stat().st_dev != metadata.st_dev
        ):
            raise PondProcessError("temporary")
        return resolved
    except PondProcessError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise PondProcessError("temporary") from None


def _require_label(label: str) -> str:
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise PondProcessError("temporary")
    return label


def _require_artifact_path(path: Path | None, directory: Path) -> Path | None:
    if path is None:
        return None
    try:
        candidate = Path(os.path.abspath(os.fspath(path)))
        if candidate.parent.resolve(strict=True) != directory:
            raise PondProcessError("artifact")
        return directory / candidate.name
    except PondProcessError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise PondProcessError("artifact") from None


def _open_private_output(path: Path) -> BinaryIO:
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "wb", buffering=0)
    except OSError:
        raise PondProcessError("temporary") from None


def _check_artifact_during_run(path: Path | None) -> None:
    if path is None:
        return
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise PondProcessError("artifact") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise PondProcessError("artifact")
    if metadata.st_size > _MAX_ARTIFACT_BYTES:
        raise PondProcessError("size")


def _take_resource_sample(
    sampler: Callable[[int], ResourceSample], process_group: int
) -> ResourceSample:
    try:
        sample = sampler(process_group)
    except PondProcessError:
        raise
    except Exception:
        raise PondProcessError("resource") from None
    if not isinstance(sample, ResourceSample):
        raise PondProcessError("resource")
    return sample


def _take_resource_sample_before_deadline(
    sampler: Callable[[int], ResourceSample],
    process_group: int,
    deadline: float,
) -> ResourceSample:
    if time.monotonic() >= deadline:
        raise PondProcessError("timeout")
    try:
        sample = _take_resource_sample(sampler, process_group)
    except BaseException:
        if time.monotonic() >= deadline:
            raise PondProcessError("timeout") from None
        raise
    if time.monotonic() >= deadline:
        raise PondProcessError("timeout")
    return sample


def _call_progress_before_deadline(
    callback: Callable[[], None] | None,
    deadline: float,
) -> None:
    if callback is None:
        return
    started_at = time.monotonic()
    if started_at >= deadline:
        raise PondProcessError("timeout")
    try:
        callback()
    except BaseException:
        completed_at = time.monotonic()
        if (
            completed_at >= deadline
            or completed_at - started_at > _MAX_PROGRESS_CALLBACK_SECONDS
        ):
            raise PondProcessError("timeout") from None
        raise
    completed_at = time.monotonic()
    if (
        completed_at >= deadline
        or completed_at - started_at > _MAX_PROGRESS_CALLBACK_SECONDS
    ):
        raise PondProcessError("timeout")


def _enforce_resource_limits(
    sample: ResourceSample,
    initial_swap: int,
    limits: ResourceLimits | None,
) -> None:
    if limits is None:
        return
    if (
        sample.rss_bytes > limits.max_rss_bytes
        or sample.physical_bytes is None
        or sample.physical_bytes > limits.max_physical_bytes
        or max(0, sample.swap_used_bytes - initial_swap) > limits.max_swap_growth_bytes
    ):
        raise PondProcessError("resource")


def _leader_exited_without_reap(process: subprocess.Popen[bytes]) -> bool:
    """Observe leader exit while retaining its PID/PGID reservation."""
    if process.returncode is not None:
        return True
    try:
        status = os.waitid(
            os.P_PID,
            process.pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
    except (AttributeError, ChildProcessError, OSError):
        raise PondProcessError("subprocess") from None
    return status is not None


def _stop_process(process: subprocess.Popen[bytes], process_group: int) -> int:
    if process.returncode is not None:
        if _process_group_exists(process_group):
            raise PondProcessError("subprocess")
        return process.returncode
    _signal_process_group(process, process_group, signal.SIGTERM)
    deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
    while _process_group_exists(process_group) and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
    if _process_group_exists(process_group):
        _signal_process_group(process, process_group, signal.SIGKILL)
    try:
        returncode = process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_process_group(process, process_group, signal.SIGKILL)
        try:
            returncode = process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except (OSError, subprocess.SubprocessError):
            raise PondProcessError("subprocess") from None
    except OSError:
        raise PondProcessError("subprocess") from None
    deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
    while _process_group_exists(process_group) and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
    if _process_group_exists(process_group):
        raise PondProcessError("subprocess")
    return returncode


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, AttributeError):
        return True
    return True


def _signal_process_group(
    process: subprocess.Popen[bytes], process_group: int, signal_number: int
) -> None:
    try:
        os.killpg(process_group, signal_number)
    except (OSError, AttributeError):
        try:
            os.kill(process.pid, signal_number)
        except OSError:
            pass


def _sample_linux_process_group(process_group: int) -> ResourceSample:
    rss_bytes = 0
    try:
        with os.scandir(_PROC_ROOT) as entries:
            for index, entry in enumerate(entries):
                if index >= _MAX_PROC_ENTRIES:
                    raise PondProcessError("resource")
                if not entry.name.isdigit() or not entry.is_dir(follow_symlinks=False):
                    continue
                stat_bytes = _read_bounded_file(Path(entry.path) / "stat")
                closing_parenthesis = stat_bytes.rfind(b")")
                if closing_parenthesis < 0:
                    continue
                fields = stat_bytes[closing_parenthesis + 1 :].split()
                if len(fields) < 3:
                    continue
                try:
                    member_group = int(fields[2])
                except ValueError:
                    continue
                if member_group != process_group:
                    continue
                status = _read_bounded_file(Path(entry.path) / "status")
                rss_bytes += _linux_status_rss(status)
        swap_used = _linux_swap_used(_read_bounded_file(_PROC_ROOT / "meminfo"))
        return ResourceSample(rss_bytes, None, swap_used)
    except PondProcessError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise PondProcessError("resource") from None


def _read_bounded_file(path: Path) -> bytes:
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return b""
    except OSError:
        raise PondProcessError("resource") from None
    try:
        data = os.read(descriptor, _PROC_FILE_BYTES + 1)
    except OSError:
        raise PondProcessError("resource") from None
    finally:
        os.close(descriptor)
    if len(data) > _PROC_FILE_BYTES:
        raise PondProcessError("resource")
    return data


def _linux_status_rss(status: bytes) -> int:
    for line in status.splitlines():
        if line.startswith(b"VmRSS:"):
            fields = line.split()
            if len(fields) != 3 or fields[2] != b"kB":
                raise PondProcessError("resource")
            try:
                value = int(fields[1])
            except ValueError:
                raise PondProcessError("resource") from None
            if value < 0:
                raise PondProcessError("resource")
            return value * 1024
    return 0


def _linux_swap_used(meminfo: bytes) -> int:
    values: dict[bytes, int] = {}
    for line in meminfo.splitlines():
        if line.startswith((b"SwapTotal:", b"SwapFree:")):
            fields = line.split()
            if len(fields) != 3 or fields[2] != b"kB":
                raise PondProcessError("resource")
            try:
                values[fields[0]] = int(fields[1])
            except ValueError:
                raise PondProcessError("resource") from None
    if set(values) != {b"SwapTotal:", b"SwapFree:"}:
        raise PondProcessError("resource")
    used = values[b"SwapTotal:"] - values[b"SwapFree:"]
    if used < 0:
        raise PondProcessError("resource")
    return used * 1024


def _sample_darwin_process_group(process_group: int) -> ResourceSample:
    output = _bounded_command_output(("/bin/ps", "-axo", "pid=,pgid=,rss="))
    pids: list[int] = []
    rss_bytes = 0
    try:
        for line in output.splitlines():
            fields = line.split()
            if len(fields) != 3:
                raise PondProcessError("resource")
            pid, member_group, rss_kib = (int(field) for field in fields)
            if pid <= 0 or member_group <= 0 or rss_kib < 0:
                raise PondProcessError("resource")
            if member_group == process_group:
                pids.append(pid)
                rss_bytes += rss_kib * 1024
    except (TypeError, ValueError):
        raise PondProcessError("resource") from None
    swap_output = _bounded_command_output(("/usr/sbin/sysctl", "-n", "vm.swapusage"))
    swap_used = _darwin_swap_used(swap_output)
    physical = _darwin_physical_footprint(tuple(pids))
    return ResourceSample(rss_bytes, physical, swap_used)


def _bounded_command_output(command: tuple[str, ...]) -> bytes:
    if not command or not Path(command[0]).is_absolute():
        raise PondProcessError("resource")
    try:
        process = subprocess.Popen(
            command,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            umask=0o077,
        )
    except (OSError, TypeError, ValueError):
        raise PondProcessError("resource") from None
    assert process.stdout is not None
    chunks: list[bytes] = []
    count = 0
    deadline = time.monotonic() + _SAMPLER_TIMEOUT_SECONDS
    reaped = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PondProcessError("resource")
            ready, _, _ = _select_readable(process.stdout, remaining)
            if not ready:
                raise PondProcessError("resource")
            chunk = os.read(
                process.stdout.fileno(),
                min(_CHUNK_BYTES, _SAMPLER_OUTPUT_BYTES - count + 1),
            )
            if not chunk:
                break
            count += len(chunk)
            if count > _SAMPLER_OUTPUT_BYTES:
                raise PondProcessError("resource")
            chunks.append(chunk)
        while not _sampler_leader_exited_without_reap(process):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PondProcessError("resource")
            time.sleep(min(_POLL_SECONDS, remaining))
        returncode = _stop_sampler_process(process)
        reaped = True
        if returncode != 0:
            raise PondProcessError("resource")
        return b"".join(chunks)
    except PondProcessError:
        raise
    except (OSError, subprocess.SubprocessError):
        raise PondProcessError("resource") from None
    finally:
        try:
            if not reaped:
                _stop_sampler_process(process)
        finally:
            process.stdout.close()


def _select_readable(
    stream: BinaryIO, timeout: float
) -> tuple[list[BinaryIO], list[BinaryIO], list[BinaryIO]]:
    import select

    return select.select([stream], [], [], timeout)


def _sampler_leader_exited_without_reap(process: subprocess.Popen[bytes]) -> bool:
    try:
        status = os.waitid(
            os.P_PID,
            process.pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
    except (AttributeError, ChildProcessError, OSError):
        raise PondProcessError("resource") from None
    return status is not None


def _stop_sampler_process(process: subprocess.Popen[bytes]) -> int:
    if process.returncode is not None:
        if _process_group_exists(process.pid):
            raise PondProcessError("resource")
        return process.returncode
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            os.kill(process.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        returncode = process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            try:
                os.kill(process.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            returncode = process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except (OSError, subprocess.SubprocessError):
            raise PondProcessError("resource") from None
    except OSError:
        raise PondProcessError("resource") from None
    deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
    while _process_group_exists(process.pid) and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
    if _process_group_exists(process.pid):
        raise PondProcessError("resource")
    return returncode


def _darwin_swap_used(output: bytes) -> int:
    match = _SWAP_USAGE.search(output)
    if match is None:
        raise PondProcessError("resource")
    number = float(match.group(1))
    exponent = {b"K": 1, b"M": 2, b"G": 3, b"T": 4, b"P": 5}[match.group(2)]
    value = number * (1024**exponent)
    if not math.isfinite(value) or value < 0:
        raise PondProcessError("resource")
    return int(value)


class _RUsageInfoV2(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64)
        for name in (
            "ri_user_time",
            "ri_system_time",
            "ri_pkg_idle_wkups",
            "ri_interrupt_wkups",
            "ri_pageins",
            "ri_wired_size",
            "ri_resident_size",
            "ri_phys_footprint",
            "ri_proc_start_abstime",
            "ri_proc_exit_abstime",
            "ri_child_user_time",
            "ri_child_system_time",
            "ri_child_pkg_idle_wkups",
            "ri_child_interrupt_wkups",
            "ri_child_pageins",
            "ri_child_elapsed_abstime",
            "ri_diskio_bytesread",
            "ri_diskio_byteswritten",
        )
    ]


def _darwin_physical_footprint(pids: tuple[int, ...]) -> int | None:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pid_rusage = libproc.proc_pid_rusage
        proc_pid_rusage.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(_RUsageInfoV2),
        ]
        proc_pid_rusage.restype = ctypes.c_int
    except (AttributeError, OSError):
        return None
    total = 0
    measured = 0
    for pid in pids:
        info = _RUsageInfoV2()
        if proc_pid_rusage(pid, 2, ctypes.byref(info)) == 0:
            total += int(info.ri_phys_footprint)
            measured += 1
    if measured != len(pids):
        return None
    return total
