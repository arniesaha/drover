"""Bounded live-state gates and a same-host lock for Pond backups."""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from drover.config import (
    DroverConfig,
    default_token_file,
    resolve_api_token_env,
)
from drover.server.archive.inventory import _open_nofollow_path

_PREFLIGHT_ERROR = "archive backup preflight failed"
_LOCAL_CHANGED_ERROR = "archive backup local changed"
_RESOURCE_ERROR = "archive backup resource limit"
_DROPPED_METRIC = b"drover_harness_dropped_events_total"
_HARNESS_PORT = 7081
_MINIMUM_SAMPLES = 30
_MAX_P95_MS = 100.0
_MAX_SWAP_GROWTH_BYTES = 512 * 1024**2
_CAPTURE_BUDGET_SECONDS = 0.8
_HTTP_SLICE_SECONDS = 0.15
_SAMPLE_INTERVAL_SECONDS = 0.05
_MAX_HEALTH_BYTES = 4 * 1024
_MAX_READY_BYTES = 64 * 1024
_MAX_METRICS_BYTES = 1024 * 1024
_MAX_HARNESS_BYTES = 64 * 1024
_MAX_TOKEN_BYTES = 4 * 1024
_MAX_TOOL_BYTES = 64 * 1024
_TOOL_CHUNK_BYTES = 16 * 1024
_TOOL_POLL_SECONDS = 0.01
_TOOL_REAP_SECONDS = 0.075
_MAX_HOST_ID_CHARS = 1024
_MAX_PROCESS_TOKEN_BYTES = 1024
_MAX_COUNTER = 2**63 - 1
_LOCK_NAME = ".backup.lock"
_LSOF_PATH = "/usr/sbin/lsof" if sys.platform == "darwin" else "/usr/bin/lsof"
_SYSCTL_PATH = "/usr/sbin/sysctl"
_SWAP_USAGE = re.compile(rb"\bused\s*=\s*([0-9]+(?:\.[0-9]+)?)([KMGTP])\b")


class BackupRuntimeError(ValueError):
    """One fixed public runtime failure category."""


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeIdentity:
    """Opaque identities for the three local listeners that must not restart."""

    hub: str = field(repr=False)
    harnessd: str = field(repr=False)
    pond: str = field(repr=False)

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value
            for value in _runtime_identity_values(self)
        ):
            raise BackupRuntimeError(_PREFLIGHT_ERROR)


@dataclass(frozen=True, slots=True)
class RuntimeEvidence:
    """Aggregate-only health evidence safe for a private receipt."""

    health_samples: int
    health_p95_ms: float


@dataclass(frozen=True, slots=True, repr=False)
class _RuntimeSnapshot:
    identity: RuntimeIdentity = field(repr=False)
    host_id: str = field(repr=False)
    dropped_events: int = field(repr=False)
    healthy: bool = field(repr=False)
    health_latency_ms: float = field(repr=False)
    swap_used_bytes: int = field(repr=False)


class _RuntimeProbe(Protocol):
    def capture(self) -> Any: ...

    def wait_for_next_sample(self) -> None: ...


_HttpGet = Callable[[str, Mapping[str, str], float, int], tuple[int, bytes]]
_ListenerIdentity = Callable[[int, float], str]
_CommandOutput = Callable[[tuple[str, ...], float], bytes]
_ProcessStart = Callable[[int, float], str]
_Clock = Callable[[], float]
_SwapUsed = Callable[[float], int]
_Wait = Callable[[float], None]


def _runtime_identity_values(identity: RuntimeIdentity) -> tuple[str, str, str]:
    return (identity.hub, identity.harnessd, identity.pond)


class RuntimeGuard:
    """Keep one backup bound to the same healthy local runtime."""

    def __init__(
        self,
        source: DroverConfig | _RuntimeProbe,
        *,
        minimum_samples: int = _MINIMUM_SAMPLES,
        max_p95_ms: float = _MAX_P95_MS,
        max_swap_growth_bytes: int = _MAX_SWAP_GROWTH_BYTES,
        http_get: _HttpGet | None = None,
        listener_identity: _ListenerIdentity | None = None,
        command_output: _CommandOutput | None = None,
        process_start: _ProcessStart | None = None,
        clock: _Clock = time.monotonic,
        swap_used: _SwapUsed | None = None,
        wait: _Wait = time.sleep,
    ) -> None:
        if (
            isinstance(minimum_samples, bool)
            or not isinstance(minimum_samples, int)
            or minimum_samples < 1
            or isinstance(max_p95_ms, bool)
            or not isinstance(max_p95_ms, (int, float))
            or not math.isfinite(max_p95_ms)
            or max_p95_ms <= 0
            or isinstance(max_swap_growth_bytes, bool)
            or not isinstance(max_swap_growth_bytes, int)
            or max_swap_growth_bytes < 0
        ):
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        self._minimum_samples = minimum_samples
        self._max_p95_ms = float(max_p95_ms)
        self._max_swap_growth_bytes = max_swap_growth_bytes
        self._baseline: Any | None = None
        self._latencies: list[float] = []
        self._state = "new"
        if callable(getattr(source, "capture", None)):
            if any(
                seam is not None
                for seam in (
                    http_get,
                    listener_identity,
                    command_output,
                    process_start,
                    swap_used,
                )
            ):
                raise BackupRuntimeError(_PREFLIGHT_ERROR)
            self._probe = source
        else:
            try:
                self._probe = _LiveRuntimeProbe(
                    source,
                    http_get=http_get,
                    listener_identity=listener_identity,
                    command_output=command_output,
                    process_start=process_start,
                    clock=clock,
                    swap_used=swap_used,
                    wait=wait,
                )
            except BackupRuntimeError:
                raise
            except Exception:
                raise BackupRuntimeError(_PREFLIGHT_ERROR) from None

    def capture_baseline(self) -> None:
        """Capture one healthy identity and fill the initial health window."""
        if self._state != "new":
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        self._state = "capturing"
        try:
            baseline = self._capture()
            if not baseline.healthy:
                raise BackupRuntimeError(_PREFLIGHT_ERROR)
            self._baseline = baseline
            self._latencies = [baseline.health_latency_ms]
            while len(self._latencies) < self._minimum_samples:
                self._wait_for_next_sample()
                self._sample_current()
        except BackupRuntimeError:
            self._state = "failed"
            raise
        self._state = "active"

    def sample(self) -> None:
        """Take one bounded progress-callback sample."""
        if self._state != "active":
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        try:
            self._sample_current()
        except BackupRuntimeError:
            self._state = "failed"
            raise

    def finish(self) -> RuntimeEvidence:
        """Take a final sample and enforce the strict health percentile."""
        if self._state != "active":
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        try:
            self._sample_current()
            if len(self._latencies) < self._minimum_samples:
                raise BackupRuntimeError(_PREFLIGHT_ERROR)
            p95 = percentile(self._latencies, 95.0)
            if p95 >= self._max_p95_ms:
                raise BackupRuntimeError(_RESOURCE_ERROR)
            evidence = RuntimeEvidence(len(self._latencies), p95)
        except BackupRuntimeError:
            self._state = "failed"
            raise
        self._state = "finished"
        return evidence

    def _capture(self) -> Any:
        try:
            snapshot = self._probe.capture()
            _validate_snapshot(snapshot)
            return snapshot
        except BackupRuntimeError:
            raise
        except Exception:
            raise BackupRuntimeError(_PREFLIGHT_ERROR) from None

    def _wait_for_next_sample(self) -> None:
        try:
            self._probe.wait_for_next_sample()
        except BackupRuntimeError:
            raise
        except Exception:
            raise BackupRuntimeError(_PREFLIGHT_ERROR) from None

    def _sample_current(self) -> None:
        baseline = self._baseline
        if baseline is None:
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        current = self._capture()
        if (
            current.identity != baseline.identity
            or current.host_id != baseline.host_id
            or current.dropped_events != baseline.dropped_events
        ):
            raise BackupRuntimeError(_LOCAL_CHANGED_ERROR)
        if not current.healthy:
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        if (
            current.swap_used_bytes - baseline.swap_used_bytes
            > self._max_swap_growth_bytes
        ):
            raise BackupRuntimeError(_RESOURCE_ERROR)
        self._latencies.append(current.health_latency_ms)


class BackupLock:
    """A same-host single-flight lock, not cross-host writer election."""

    def __init__(self, receipt_directory: str | os.PathLike[str]) -> None:
        self._path = Path(receipt_directory) / _LOCK_NAME
        self._descriptor: int | None = None

    def __enter__(self) -> BackupLock:
        if self._descriptor is not None:
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        descriptor = open_private_lock(self._path)
        try:
            acquire_nonblocking_flock(descriptor)
            _require_same_lock(self._path, descriptor)
        except BaseException:
            try:
                release_flock(descriptor)
            except BackupRuntimeError:
                pass
            finally:
                os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        try:
            release_flock(descriptor)
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass


class _LiveRuntimeProbe:
    def __init__(
        self,
        config: Any,
        *,
        http_get: _HttpGet | None,
        listener_identity: _ListenerIdentity | None,
        command_output: _CommandOutput | None,
        process_start: _ProcessStart | None,
        clock: _Clock,
        swap_used: _SwapUsed | None,
        wait: _Wait,
    ) -> None:
        try:
            self._config = config
            self._pond_port = _loopback_pond_port(config)
            self._hub_host = _local_hub_host(config)
            self._hub_port = _valid_port(config.metrics_http_port)
            self._hub_root = _http_root(self._hub_host, self._hub_port)
            self._harness_root = _http_root("127.0.0.1", _HARNESS_PORT)
            self._clock = clock
            self._wait = wait
            self._http_get = http_get or _bounded_http_get
            self._command_output = command_output or (
                lambda command, deadline: _bounded_command_output(
                    command, deadline, clock=self._clock
                )
            )
            self._process_start = process_start or _native_process_start
            self._listener_identity = listener_identity or (
                lambda port, deadline: _listener_process_identity(
                    port, deadline, self._command_output, self._process_start
                )
            )
            self._swap_used = swap_used or (
                lambda deadline: _system_swap_used(deadline, self._command_output)
            )
            self._resolved_token: str | None = None
        except BackupRuntimeError:
            raise
        except (AttributeError, TypeError, ValueError):
            raise BackupRuntimeError(_PREFLIGHT_ERROR) from None

    def capture(self) -> _RuntimeSnapshot:
        try:
            deadline = self._clock() + _CAPTURE_BUDGET_SECONDS
            headers = self._hub_headers()
            health_started = self._clock()
            hub_health = self._request(
                f"{self._hub_root}/healthz", headers, _MAX_HEALTH_BYTES, deadline
            )
            if hub_health.strip() != b"ok":
                raise BackupRuntimeError(_PREFLIGHT_ERROR)
            ready = _json_object(
                self._request(
                    f"{self._hub_root}/readyz", headers, _MAX_READY_BYTES, deadline
                )
            )
            if ready.get("ready") is not True:
                raise BackupRuntimeError(_PREFLIGHT_ERROR)
            dropped = _parse_dropped_events(
                self._request(
                    f"{self._hub_root}/metrics",
                    headers,
                    _MAX_METRICS_BYTES,
                    deadline,
                )
            )
            harness = _json_object(
                self._request(
                    f"{self._harness_root}/healthz",
                    {},
                    _MAX_HARNESS_BYTES,
                    deadline,
                )
            )
            host_id = _host_id(harness)
            health_latency_ms = max(0.0, (self._clock() - health_started) * 1000.0)
            identity = RuntimeIdentity(
                hub=self._listener(self._hub_port, deadline),
                harnessd=self._listener(_HARNESS_PORT, deadline),
                pond=self._listener(self._pond_port, deadline),
            )
            swap_used = self._swap_used(deadline)
            _remaining(deadline, self._clock)
            snapshot = _RuntimeSnapshot(
                identity=identity,
                host_id=host_id,
                dropped_events=dropped,
                healthy=True,
                health_latency_ms=health_latency_ms,
                swap_used_bytes=swap_used,
            )
            _validate_snapshot(snapshot)
            return snapshot
        except BackupRuntimeError:
            raise
        except Exception:
            raise BackupRuntimeError(_PREFLIGHT_ERROR) from None

    def wait_for_next_sample(self) -> None:
        try:
            self._wait(_SAMPLE_INTERVAL_SECONDS)
        except Exception:
            raise BackupRuntimeError(_PREFLIGHT_ERROR) from None

    def _hub_headers(self) -> Mapping[str, str]:
        if not self._config.auth_enabled:
            return {}
        if self._resolved_token is None:
            self._resolved_token = _resolve_local_api_token(self._config)
        return {"Authorization": f"Bearer {self._resolved_token}"}

    def _request(
        self,
        url: str,
        headers: Mapping[str, str],
        maximum: int,
        deadline: float,
    ) -> bytes:
        remaining = _remaining(deadline, self._clock)
        timeout = min(_HTTP_SLICE_SECONDS, remaining)
        status, body = self._http_get(url, headers, timeout, maximum)
        _remaining(deadline, self._clock)
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or status != 200
            or not isinstance(body, bytes)
            or len(body) > maximum
        ):
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        return body

    def _listener(self, port: int, deadline: float) -> str:
        _remaining(deadline, self._clock)
        identity = self._listener_identity(port, deadline)
        _remaining(deadline, self._clock)
        if not isinstance(identity, str) or not identity:
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        return identity


def _validate_snapshot(snapshot: Any) -> None:
    try:
        if type(snapshot.identity) is not RuntimeIdentity:
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        if (
            not isinstance(snapshot.host_id, str)
            or not snapshot.host_id
            or len(snapshot.host_id) > _MAX_HOST_ID_CHARS
            or snapshot.host_id.strip() != snapshot.host_id
        ):
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        if (
            isinstance(snapshot.dropped_events, bool)
            or not isinstance(snapshot.dropped_events, int)
            or not 0 <= snapshot.dropped_events <= _MAX_COUNTER
            or type(snapshot.healthy) is not bool
            or isinstance(snapshot.health_latency_ms, bool)
            or not isinstance(snapshot.health_latency_ms, (int, float))
            or not math.isfinite(snapshot.health_latency_ms)
            or snapshot.health_latency_ms < 0
            or isinstance(snapshot.swap_used_bytes, bool)
            or not isinstance(snapshot.swap_used_bytes, int)
            or snapshot.swap_used_bytes < 0
        ):
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
    except AttributeError:
        raise BackupRuntimeError(_PREFLIGHT_ERROR) from None


def percentile(values: list[float], percentile_value: float) -> float:
    """Return the nearest-rank percentile for finite nonnegative samples."""
    if (
        not values
        or isinstance(percentile_value, bool)
        or not isinstance(percentile_value, (int, float))
        or not math.isfinite(percentile_value)
        or not 0 < percentile_value <= 100
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in values
        )
    ):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    ordered = sorted(float(value) for value in values)
    rank = math.ceil((float(percentile_value) / 100.0) * len(ordered))
    return ordered[rank - 1]


def open_private_lock(path: Path) -> int:
    """Open/create one private regular lock beneath a pinned private directory."""
    directory_descriptor = -1
    lock_descriptor = -1
    keep_lock_descriptor = False
    try:
        lock_path = Path(path)
        directory = lock_path.parent
        if (
            lock_path.name != _LOCK_NAME
            or not directory.is_absolute()
            or directory.resolve(strict=True) != directory
        ):
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        directory_descriptor = _open_nofollow_path(
            directory, flags=_directory_open_flags()
        )
        directory_metadata = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(directory_metadata.st_mode) != 0o700
        ):
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lock_descriptor = os.open(_LOCK_NAME, flags, 0o600, dir_fd=directory_descriptor)
        lock_metadata = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.geteuid()
            or lock_metadata.st_nlink != 1
        ):
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        os.fchmod(lock_descriptor, 0o600)
        lock_metadata = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.geteuid()
            or lock_metadata.st_nlink != 1
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
        ):
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        _require_same_directory(directory, directory_metadata)
        keep_lock_descriptor = True
        return lock_descriptor
    except BackupRuntimeError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise BackupRuntimeError(_PREFLIGHT_ERROR) from None
    finally:
        if directory_descriptor >= 0:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass
        if lock_descriptor >= 0 and not keep_lock_descriptor:
            try:
                os.close(lock_descriptor)
            except OSError:
                pass


def acquire_nonblocking_flock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        raise BackupRuntimeError(_PREFLIGHT_ERROR) from None


def release_flock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        raise BackupRuntimeError(_PREFLIGHT_ERROR) from None


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid)


def _require_same_directory(directory: Path, expected: os.stat_result) -> None:
    descriptor = -1
    try:
        descriptor = _open_nofollow_path(directory, flags=_directory_open_flags())
        current = os.fstat(descriptor)
    except (OSError, TypeError, ValueError):
        raise BackupRuntimeError(_PREFLIGHT_ERROR) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if _identity(current) != _identity(expected):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)


def _require_same_lock(path: Path, expected_descriptor: int) -> None:
    current_descriptor = -1
    try:
        current_descriptor = _open_nofollow_path(path)
        expected = os.fstat(expected_descriptor)
        current = os.fstat(current_descriptor)
    except (OSError, TypeError, ValueError):
        raise BackupRuntimeError(_PREFLIGHT_ERROR) from None
    finally:
        if current_descriptor >= 0:
            os.close(current_descriptor)
    if (
        (expected.st_dev, expected.st_ino) != (current.st_dev, current.st_ino)
        or not stat.S_ISREG(current.st_mode)
        or expected.st_nlink != 1
        or current.st_nlink != 1
        or current.st_uid != os.geteuid()
        or stat.S_IMODE(current.st_mode) != 0o600
    ):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)


def _loopback_pond_port(config: Any) -> int:
    archive = config.archive
    if archive.enabled is not True or not isinstance(archive.base_url, str):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    value = archive.base_url
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
        port = 80 if parsed_port is None else parsed_port
        host = parsed.hostname
        loopback = host == "localhost" or (
            host is not None and ipaddress.ip_address(host).is_loopback
        )
    except ValueError:
        raise BackupRuntimeError(_PREFLIGHT_ERROR) from None
    if (
        parsed.scheme != "http"
        or not loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    return _valid_port(port)


def _valid_port(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    return value


def _local_hub_host(config: Any) -> str:
    host = str(config.server_metrics_host or "").strip()
    if not host or host in {"0.0.0.0", "::", "*"}:
        return "127.0.0.1"
    if host == "localhost":
        return host
    try:
        ipaddress.ip_address(host)
    except ValueError:
        raise BackupRuntimeError(_PREFLIGHT_ERROR) from None
    return host


def _http_root(host: str, port: int) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    return f"http://{rendered_host}:{port}"


def _resolve_local_api_token(config: Any) -> str:
    token = resolve_api_token_env() or str(config.auth_api_token).strip()
    if not token:
        descriptor = -1
        try:
            descriptor = _open_nofollow_path(default_token_file())
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size > _MAX_TOKEN_BYTES
            ):
                raise BackupRuntimeError(_PREFLIGHT_ERROR)
            data = os.read(descriptor, _MAX_TOKEN_BYTES + 1)
            after = os.fstat(descriptor)
            if len(data) > _MAX_TOKEN_BYTES or _token_file_identity(
                before
            ) != _token_file_identity(after):
                raise BackupRuntimeError(_PREFLIGHT_ERROR)
            token = data.decode("utf-8").strip()
        except BackupRuntimeError:
            raise
        except (OSError, UnicodeDecodeError, ValueError):
            raise BackupRuntimeError(_PREFLIGHT_ERROR) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    if (
        not token
        or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES
        or any(ord(character) < 33 or ord(character) > 126 for character in token)
    ):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    return token


def _token_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _bounded_http_get(
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
) -> tuple[int, bytes]:
    import http.client

    connection: http.client.HTTPConnection | None = None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        port = _valid_port(parsed.port or 80)
        deadline = time.monotonic() + timeout_seconds
        connection = http.client.HTTPConnection(
            parsed.hostname, port, timeout=timeout_seconds
        )
        connection.request("GET", parsed.path, headers=dict(headers))
        response = connection.getresponse()
        content_length = response.getheader("Content-Length")
        if content_length is not None and int(content_length) > max_response_bytes:
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        body = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackupRuntimeError(_PREFLIGHT_ERROR)
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(16 * 1024, max_response_bytes - len(body) + 1))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > max_response_bytes:
                raise BackupRuntimeError(_PREFLIGHT_ERROR)
        return response.status, bytes(body)
    except BackupRuntimeError:
        raise
    except (OSError, TypeError, ValueError):
        raise BackupRuntimeError(_PREFLIGHT_ERROR) from None
    finally:
        if connection is not None:
            connection.close()


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise BackupRuntimeError(_PREFLIGHT_ERROR) from None
    if type(value) is not dict:
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    return value


def _host_id(harness: Mapping[str, Any]) -> str:
    host_id = harness.get("host_id")
    if harness.get("ok") is not True or not isinstance(host_id, str):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    if (
        not host_id
        or len(host_id) > _MAX_HOST_ID_CHARS
        or host_id.strip() != host_id
        or any(ord(character) < 32 for character in host_id)
    ):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    return host_id


def _parse_dropped_events(body: bytes) -> int:
    samples: list[list[bytes]] = []
    for line in body.splitlines():
        fields = line.split()
        if not fields:
            continue
        name = fields[0]
        if name == _DROPPED_METRIC or name.startswith(_DROPPED_METRIC + b"{"):
            samples.append(fields)
    if len(samples) != 1:
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    fields = samples[0]
    if fields[0] != _DROPPED_METRIC or len(fields) not in {2, 3}:
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    if not fields[1].isdigit() or (len(fields) == 3 and not fields[2].isdigit()):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    value = int(fields[1])
    if value > _MAX_COUNTER:
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    return value


def _listener_process_identity(
    port: int,
    deadline: float,
    command_output: _CommandOutput,
    process_start: _ProcessStart,
) -> str:
    checked_port = _valid_port(port)
    lsof = (
        _LSOF_PATH,
        "-nP",
        "-a",
        f"-iTCP:{checked_port}",
        "-sTCP:LISTEN",
        "-t",
    )
    first_pids = _parse_listener_pids(command_output(lsof, deadline))
    if len(first_pids) != 1:
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    pid = first_pids[0]
    first_start = _validated_process_start(pid, deadline, process_start)
    second_pids = _parse_listener_pids(command_output(lsof, deadline))
    second_start = _validated_process_start(pid, deadline, process_start)
    if second_pids != (pid,) or first_start != second_start:
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    private_material = f"{pid}\0{first_start}".encode("utf-8")
    return hashlib.sha256(private_material).hexdigest()


def _parse_listener_pids(output: bytes) -> tuple[int, ...]:
    if not isinstance(output, bytes) or len(output) > _MAX_TOOL_BYTES:
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    pids: set[int] = set()
    for line in output.splitlines():
        if not line.isdigit():
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        pid = int(line)
        if pid <= 0:
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        pids.add(pid)
    return tuple(sorted(pids))


def _validated_process_start(
    pid: int, deadline: float, process_start: _ProcessStart
) -> str:
    token = process_start(pid, deadline)
    if (
        not isinstance(token, str)
        or not token
        or len(token.encode("utf-8")) > _MAX_PROCESS_TOKEN_BYTES
        or "\x00" in token
    ):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    return token


class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("pbi_rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _native_process_start(pid: int, deadline: float) -> str:
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or time.monotonic() >= deadline
    ):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    if sys.platform == "darwin":
        token = _darwin_process_start(pid)
    elif sys.platform.startswith("linux"):
        token = _linux_process_start(pid)
    else:
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    if time.monotonic() >= deadline:
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    return token


def _darwin_process_start(pid: int) -> str:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = libproc.proc_pidinfo
        proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        proc_pidinfo.restype = ctypes.c_int
        info = _ProcBSDInfo()
        size = ctypes.sizeof(info)
        written = proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
    except (AttributeError, OSError, TypeError, ValueError):
        raise BackupRuntimeError(_PREFLIGHT_ERROR) from None
    if (
        written != size
        or info.pbi_pid != pid
        or info.pbi_start_tvsec <= 0
        or info.pbi_start_tvusec >= 1_000_000
    ):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    return f"{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"


def _linux_process_start(pid: int) -> str:
    descriptor = -1
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(f"/proc/{pid}/stat", flags)
        data = os.read(descriptor, _MAX_PROCESS_TOKEN_BYTES + 1)
    except OSError:
        raise BackupRuntimeError(_PREFLIGHT_ERROR) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _parse_linux_process_start(pid, data)


def _parse_linux_process_start(pid: int, data: bytes) -> str:
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(data, bytes)
        or len(data) > _MAX_PROCESS_TOKEN_BYTES
    ):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    opening_parenthesis = data.find(b" (")
    closing_parenthesis = data.rfind(b")")
    fields = (
        data[closing_parenthesis + 1 :].split()
        if closing_parenthesis > opening_parenthesis >= 1
        else []
    )
    embedded_pid = data[:opening_parenthesis]
    if (
        not embedded_pid.isdigit()
        or int(embedded_pid) != pid
        or len(fields) < 20
        or not fields[19].isdigit()
        or int(fields[19]) <= 0
    ):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    return fields[19].decode("ascii")


def _bounded_command_output(
    command: tuple[str, ...], deadline: float, *, clock: _Clock
) -> bytes:
    if (
        not command
        or not isinstance(command[0], str)
        or not Path(command[0]).is_absolute()
        or any(
            not isinstance(argument, str) or "\x00" in argument for argument in command
        )
    ):
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    chunks: list[bytes] = []
    count = 0
    try:
        _remaining(deadline, clock)
        process = subprocess.Popen(
            command,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            umask=0o077,
        )
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = _remaining(deadline, clock)
            ready = selector.select(min(_TOOL_POLL_SECONDS, remaining))
            if not ready:
                continue
            for key, _ in ready:
                chunk = os.read(
                    key.fd,
                    min(_TOOL_CHUNK_BYTES, _MAX_TOOL_BYTES - count + 1),
                )
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                count += len(chunk)
                if count > _MAX_TOOL_BYTES:
                    raise BackupRuntimeError(_PREFLIGHT_ERROR)
                chunks.append(chunk)
        remaining = _remaining(deadline, clock)
        returncode = process.wait(timeout=remaining)
        if returncode != 0:
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        return b"".join(chunks)
    except BackupRuntimeError:
        raise
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        raise BackupRuntimeError(_PREFLIGHT_ERROR) from None
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            _reap_tool(process)
            if process.stdout is not None:
                process.stdout.close()


def _reap_tool(process: subprocess.Popen[bytes]) -> None:
    process.poll()
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=_TOOL_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=_TOOL_REAP_SECONDS)
        except (OSError, subprocess.SubprocessError):
            raise BackupRuntimeError(_PREFLIGHT_ERROR) from None
    except OSError:
        raise BackupRuntimeError(_PREFLIGHT_ERROR) from None


def _system_swap_used(deadline: float, command_output: _CommandOutput) -> int:
    if sys.platform == "darwin":
        output = command_output((_SYSCTL_PATH, "-n", "vm.swapusage"), deadline)
        match = _SWAP_USAGE.search(output)
        if match is None:
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        value = float(match.group(1)) * (
            1024 ** {b"K": 1, b"M": 2, b"G": 3, b"T": 4, b"P": 5}[match.group(2)]
        )
        if not math.isfinite(value) or value < 0:
            raise BackupRuntimeError(_PREFLIGHT_ERROR)
        return int(value)
    if sys.platform.startswith("linux"):
        return _linux_swap_used(_read_proc_meminfo(deadline))
    raise BackupRuntimeError(_PREFLIGHT_ERROR)


def _read_proc_meminfo(deadline: float) -> bytes:
    if time.monotonic() >= deadline:
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    descriptor = -1
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open("/proc/meminfo", flags)
        data = os.read(descriptor, _MAX_TOOL_BYTES + 1)
    except OSError:
        raise BackupRuntimeError(_PREFLIGHT_ERROR) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > _MAX_TOOL_BYTES or time.monotonic() >= deadline:
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    return data


def _linux_swap_used(meminfo: bytes) -> int:
    values: dict[bytes, int] = {}
    for line in meminfo.splitlines():
        if line.startswith((b"SwapTotal:", b"SwapFree:")):
            fields = line.split()
            if len(fields) != 3 or fields[2] != b"kB" or not fields[1].isdigit():
                raise BackupRuntimeError(_PREFLIGHT_ERROR)
            values[fields[0]] = int(fields[1])
    if set(values) != {b"SwapTotal:", b"SwapFree:"}:
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    used = values[b"SwapTotal:"] - values[b"SwapFree:"]
    if used < 0:
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    return used * 1024


def _remaining(deadline: float, clock: _Clock) -> float:
    remaining = deadline - clock()
    if not math.isfinite(remaining) or remaining <= 0:
        raise BackupRuntimeError(_PREFLIGHT_ERROR)
    return remaining
