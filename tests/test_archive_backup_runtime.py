"""Live-state and same-host single-flight gates for Pond backups."""

from __future__ import annotations

import ctypes
import dataclasses
import http.server
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping

import pytest

from drover.config import ArchiveConfig, default_config
from drover.server.archive import backup_runtime as runtime_module
from drover.server.archive.backup_runtime import (
    BackupLock,
    BackupRuntimeError,
    RuntimeGuard,
    RuntimeIdentity,
)

_PRIVATE_HOST = "private-host-identifier"
_PRIVATE_TOKEN = "private-api-token"
_MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def _clear_external_api_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DROVER_API_TOKEN", raising=False)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    identity: RuntimeIdentity
    host_id: str
    dropped_events: int
    healthy: bool
    health_latency_ms: float
    swap_used_bytes: int


class _FakeRuntimeProbe:
    def __init__(self, samples: list[_Snapshot]) -> None:
        self._samples = iter(samples)
        self.waits = 0

    def capture(self) -> _Snapshot:
        return next(self._samples)

    def wait_for_next_sample(self) -> None:
        self.waits += 1


class _Clock:
    def __init__(self) -> None:
        self.value = 10.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass(frozen=True, slots=True)
class _HTTPReply:
    status: int
    body: bytes
    delay_seconds: float = 0.0


@contextmanager
def _loopback_http_server(
    replies: list[_HTTPReply],
) -> Iterator[tuple[str, list[tuple[str, str | None]]]]:
    pending = list(replies)
    requests: list[tuple[str, str | None]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append((self.path, self.headers.get("Authorization")))
            reply = pending.pop(0)
            if reply.delay_seconds:
                time.sleep(reply.delay_seconds)
            self.send_response(reply.status)
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            try:
                self.wfile.write(reply.body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_path(path: Path, timeout_seconds: float = 1.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            value = path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            value = ""
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("test process did not publish its identity")


def _wait_for_process_group_exit(
    process_group: int, timeout_seconds: float = 1.0
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_group_exists(process_group):
            return True
        time.sleep(0.01)
    return not _process_group_exists(process_group)


def _identity(
    *, hub: str = "hub-a", harnessd: str = "harness-a", pond: str = "pond-a"
) -> RuntimeIdentity:
    return RuntimeIdentity(hub=hub, harnessd=harnessd, pond=pond)


def _snapshot(
    *,
    identity: RuntimeIdentity | None = None,
    host_id: str = _PRIVATE_HOST,
    dropped_events: int = 7,
    healthy: bool = True,
    latency_ms: float = 8.0,
    swap_used_bytes: int = 100 * _MIB,
) -> _Snapshot:
    return _Snapshot(
        identity=identity or _identity(),
        host_id=host_id,
        dropped_events=dropped_events,
        healthy=healthy,
        health_latency_ms=latency_ms,
        swap_used_bytes=swap_used_bytes,
    )


def _drover_config(
    *,
    pond_url: str = "http://127.0.0.1:9123",
    api_token: str = _PRIVATE_TOKEN,
):
    archive = ArchiveConfig(
        enabled=True,
        base_url=pond_url,
        timeout_seconds=1.0,
        search_limit=5,
        context_before=2,
        context_after=2,
        max_context_chars=24_000,
        max_response_bytes=64_000,
    )
    return dataclasses.replace(
        default_config(),
        archive=archive,
        metrics_http_port=7080,
        server_metrics_host="127.0.0.1",
        auth_enabled=True,
        auth_api_token=api_token,
    )


def _http_fake(
    clock: _Clock,
    *,
    token: str = _PRIVATE_TOKEN,
    metrics: bytes = b"drover_harness_dropped_events_total 7\n",
    host_id: str = _PRIVATE_HOST,
) -> Callable[[str, Mapping[str, str], float, int], tuple[int, bytes]]:
    def get(
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, bytes]:
        if not 0 < timeout_seconds < 1.0 or max_response_bytes <= 0:
            raise AssertionError("unbounded HTTP request")
        clock.advance(0.001)
        hub = "http://127.0.0.1:7080"
        if url == f"{hub}/healthz":
            if headers.get("Authorization") != f"Bearer {token}":
                raise AssertionError("hub health was not authorized")
            return 200, b"ok\n"
        if url == f"{hub}/readyz":
            if headers.get("Authorization") != f"Bearer {token}":
                raise AssertionError("hub readiness was not authorized")
            return 200, json.dumps({"ready": True}).encode("utf-8")
        if url == f"{hub}/metrics":
            if headers.get("Authorization") != f"Bearer {token}":
                raise AssertionError("hub metrics were not authorized")
            return 200, metrics
        if url == "http://127.0.0.1:7081/healthz":
            if "Authorization" in headers:
                raise AssertionError("harness health must remain unauthenticated")
            return 200, json.dumps({"ok": True, "host_id": host_id}).encode("utf-8")
        raise AssertionError("unexpected local health endpoint")

    return get


def _listener_fake(port: int, deadline: float) -> str:
    if deadline <= 0 or port not in {7080, 7081, 9123}:
        raise AssertionError("invalid listener lookup")
    return {7080: "hub-process", 7081: "harness-process", 9123: "pond-process"}[port]


def _live_guard(
    *,
    config=None,
    clock: _Clock | None = None,
    metrics: bytes = b"drover_harness_dropped_events_total 7\n",
    token: str = _PRIVATE_TOKEN,
    listener_identity: Callable[[int, float], str] = _listener_fake,
    command_output=None,
    process_start=None,
    swap_used: Callable[[float], int] | None = None,
    minimum_samples: int = 1,
    max_p95_ms: float = 100.0,
    max_swap_growth_bytes: int = 512 * _MIB,
) -> RuntimeGuard:
    active_clock = clock or _Clock()
    return RuntimeGuard(
        config or _drover_config(api_token=token),
        minimum_samples=minimum_samples,
        max_p95_ms=max_p95_ms,
        max_swap_growth_bytes=max_swap_growth_bytes,
        http_get=_http_fake(active_clock, token=token, metrics=metrics),
        listener_identity=listener_identity,
        command_output=command_output,
        process_start=process_start,
        clock=active_clock,
        swap_used=swap_used or (lambda _deadline: 100 * _MIB),
        wait=lambda seconds: active_clock.advance(seconds),
    )


def test_backup_lock_is_private_nonblocking_and_reusable_after_release(
    tmp_path: Path,
) -> None:
    receipt_directory = tmp_path / "private-receipts"
    receipt_directory.mkdir(mode=0o700)
    private_path_fragment = str(receipt_directory)

    with BackupLock(receipt_directory):
        lock_path = receipt_directory / ".backup.lock"
        assert stat.S_ISREG(lock_path.lstat().st_mode)
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        with pytest.raises(
            BackupRuntimeError, match=r"^archive backup preflight failed$"
        ) as raised:
            with BackupLock(receipt_directory):
                pass
        assert private_path_fragment not in str(raised.value)

    with BackupLock(receipt_directory):
        pass


@pytest.mark.parametrize("unsafe", ["directory_mode", "lock_symlink", "ancestor"])
def test_backup_lock_rejects_unsafe_paths_without_disclosing_them(
    tmp_path: Path, unsafe: str
) -> None:
    receipt_directory = tmp_path / "private-receipts"
    receipt_directory.mkdir(mode=0o700)
    target = tmp_path / "private-target"
    target.write_text("do not touch", encoding="utf-8")
    target.chmod(0o600)
    candidate = receipt_directory
    if unsafe == "directory_mode":
        receipt_directory.chmod(0o755)
    elif unsafe == "lock_symlink":
        (receipt_directory / ".backup.lock").symlink_to(target)
    else:
        alias = tmp_path / "private-alias"
        alias.symlink_to(receipt_directory, target_is_directory=True)
        candidate = alias

    with pytest.raises(
        BackupRuntimeError, match=r"^archive backup preflight failed$"
    ) as raised:
        with BackupLock(candidate):
            pass

    assert str(candidate) not in str(raised.value)
    assert target.read_text(encoding="utf-8") == "do not touch"


def test_backup_lock_rejects_a_hard_link_without_mutating_the_source(
    tmp_path: Path,
) -> None:
    receipt_directory = tmp_path / "private-receipts"
    receipt_directory.mkdir(mode=0o700)
    unrelated = tmp_path / "unrelated-private-file"
    unrelated.write_bytes(b"unrelated private content")
    unrelated.chmod(0o640)
    lock_path = receipt_directory / ".backup.lock"
    os.link(unrelated, lock_path)
    expected_mode = stat.S_IMODE(unrelated.stat().st_mode)
    expected_content = unrelated.read_bytes()
    failure: BackupRuntimeError | None = None

    try:
        with BackupLock(receipt_directory):
            pass
    except BackupRuntimeError as error:
        failure = error

    assert stat.S_IMODE(unrelated.stat().st_mode) == expected_mode
    assert unrelated.read_bytes() == expected_content
    assert unrelated.stat().st_nlink == 2
    assert failure is not None
    assert str(failure) == "archive backup preflight failed"


def test_backup_lock_releases_after_the_protected_body_raises(tmp_path: Path) -> None:
    receipt_directory = tmp_path / "private-receipts"
    receipt_directory.mkdir(mode=0o700)

    with pytest.raises(RuntimeError, match="body failed"):
        with BackupLock(receipt_directory):
            raise RuntimeError("body failed")

    with BackupLock(receipt_directory):
        pass


def test_runtime_identity_repr_never_discloses_process_values() -> None:
    identity = _identity(
        hub="private-hub-pid-start",
        harnessd="private-harness-pid-start",
        pond="private-pond-pid-start",
    )

    encoded = repr(identity)

    assert "private-hub" not in encoded
    assert "private-harness" not in encoded
    assert "private-pond" not in encoded


def test_runtime_guard_collects_thirty_baseline_samples_and_a_final_sample() -> None:
    probe = _FakeRuntimeProbe([_snapshot() for _ in range(31)])
    guard = RuntimeGuard(probe)

    guard.capture_baseline()
    evidence = guard.finish()

    assert probe.waits == 29
    assert evidence.health_samples == 31
    assert evidence.health_p95_ms == 8.0


def test_runtime_guard_refuses_host_identity_before_baseline_capture() -> None:
    guard = RuntimeGuard(_FakeRuntimeProbe([_snapshot()]), minimum_samples=1)

    with pytest.raises(BackupRuntimeError, match=r"^archive backup preflight failed$"):
        guard.baseline_host_id()


def test_runtime_guard_returns_private_host_identity_only_after_baseline_capture() -> (
    None
):
    guard = RuntimeGuard(
        _FakeRuntimeProbe([_snapshot(), _snapshot()]), minimum_samples=1
    )

    guard.capture_baseline()

    assert guard.baseline_host_id() == _PRIVATE_HOST
    assert _PRIVATE_HOST not in repr(guard)


@pytest.mark.parametrize(
    "changed",
    ["host", "hub", "harnessd", "pond", "dropped"],
)
def test_runtime_guard_detects_every_private_identity_change_without_exposure(
    changed: str,
) -> None:
    private_new_value = f"private-new-{changed}"
    current = {
        "identity": _identity(),
        "host_id": _PRIVATE_HOST,
        "dropped_events": 7,
    }
    if changed == "host":
        current["host_id"] = private_new_value
    elif changed == "dropped":
        current["dropped_events"] = 8
    else:
        identities = {"hub": "hub-a", "harnessd": "harness-a", "pond": "pond-a"}
        identities[changed] = private_new_value
        current["identity"] = _identity(**identities)
    guard = RuntimeGuard(
        _FakeRuntimeProbe([_snapshot(), _snapshot(**current)]), minimum_samples=1
    )
    guard.capture_baseline()

    with pytest.raises(
        BackupRuntimeError, match=r"^archive backup local changed$"
    ) as raised:
        guard.sample()

    assert private_new_value not in str(raised.value)
    assert _PRIVATE_HOST not in str(raised.value)


def test_runtime_guard_rejects_unhealthy_baselines_and_samples() -> None:
    baseline = RuntimeGuard(
        _FakeRuntimeProbe([_snapshot(healthy=False)]), minimum_samples=1
    )
    with pytest.raises(BackupRuntimeError, match=r"^archive backup preflight failed$"):
        baseline.capture_baseline()

    sample = RuntimeGuard(
        _FakeRuntimeProbe([_snapshot(), _snapshot(healthy=False)]), minimum_samples=1
    )
    sample.capture_baseline()
    with pytest.raises(BackupRuntimeError, match=r"^archive backup preflight failed$"):
        sample.sample()


def test_runtime_guard_enforces_swap_growth_above_the_inclusive_limit() -> None:
    allowed = RuntimeGuard(
        _FakeRuntimeProbe(
            [_snapshot(swap_used_bytes=100), _snapshot(swap_used_bytes=110)]
        ),
        minimum_samples=1,
        max_swap_growth_bytes=10,
    )
    allowed.capture_baseline()
    allowed.sample()

    breached = RuntimeGuard(
        _FakeRuntimeProbe(
            [_snapshot(swap_used_bytes=100), _snapshot(swap_used_bytes=111)]
        ),
        minimum_samples=1,
        max_swap_growth_bytes=10,
    )
    breached.capture_baseline()
    with pytest.raises(BackupRuntimeError, match=r"^archive backup resource limit$"):
        breached.sample()


def test_runtime_guard_uses_nearest_rank_p95_and_allows_one_upper_tail_sample() -> None:
    latencies = [1.0] * 18 + [99.0, 1_000.0]
    guard = RuntimeGuard(
        _FakeRuntimeProbe([_snapshot(latency_ms=latency) for latency in latencies]),
        minimum_samples=1,
        max_p95_ms=100.0,
    )
    guard.capture_baseline()
    for _ in range(18):
        guard.sample()

    evidence = guard.finish()

    assert evidence.health_samples == 20
    assert evidence.health_p95_ms == 99.0


def test_runtime_guard_requires_p95_to_be_strictly_below_the_limit() -> None:
    guard = RuntimeGuard(
        _FakeRuntimeProbe([_snapshot(latency_ms=100.0), _snapshot(latency_ms=100.0)]),
        minimum_samples=1,
        max_p95_ms=100.0,
    )
    guard.capture_baseline()

    with pytest.raises(BackupRuntimeError, match=r"^archive backup resource limit$"):
        guard.finish()


def test_runtime_guard_finish_takes_a_change_detecting_final_sample() -> None:
    guard = RuntimeGuard(
        _FakeRuntimeProbe(
            [
                _snapshot(),
                _snapshot(identity=_identity(pond="private-replacement")),
            ]
        ),
        minimum_samples=1,
    )
    guard.capture_baseline()

    with pytest.raises(
        BackupRuntimeError, match=r"^archive backup local changed$"
    ) as raised:
        guard.finish()

    assert "private-replacement" not in str(raised.value)


def test_runtime_guard_state_machine_rejects_out_of_order_or_repeated_calls() -> None:
    before = RuntimeGuard(_FakeRuntimeProbe([_snapshot()]), minimum_samples=1)
    for operation in (before.sample, before.finish):
        with pytest.raises(
            BackupRuntimeError, match=r"^archive backup preflight failed$"
        ):
            operation()

    repeated_capture = RuntimeGuard(_FakeRuntimeProbe([_snapshot()]), minimum_samples=1)
    repeated_capture.capture_baseline()
    with pytest.raises(BackupRuntimeError, match=r"^archive backup preflight failed$"):
        repeated_capture.capture_baseline()

    repeated_finish = RuntimeGuard(
        _FakeRuntimeProbe([_snapshot(), _snapshot()]), minimum_samples=1
    )
    repeated_finish.capture_baseline()
    repeated_finish.finish()
    for operation in (repeated_finish.sample, repeated_finish.finish):
        with pytest.raises(
            BackupRuntimeError, match=r"^archive backup preflight failed$"
        ):
            operation()


@pytest.mark.parametrize(
    "pond_url",
    [
        "http://127.0.0.1:9123",
        "http://localhost:9123",
        "http://[::1]:9123",
    ],
)
def test_live_runtime_uses_only_the_required_local_surfaces_and_is_lazy(
    pond_url: str,
) -> None:
    clock = _Clock()
    contacted: list[str] = []
    http = _http_fake(clock)

    def recording_http(
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, bytes]:
        contacted.append(url)
        return http(url, headers, timeout_seconds, max_response_bytes)

    guard = RuntimeGuard(
        _drover_config(pond_url=pond_url),
        minimum_samples=1,
        http_get=recording_http,
        listener_identity=_listener_fake,
        clock=clock,
        swap_used=lambda _deadline: 100 * _MIB,
        wait=lambda seconds: clock.advance(seconds),
    )
    assert contacted == []

    guard.capture_baseline()

    assert contacted == [
        "http://127.0.0.1:7080/healthz",
        "http://127.0.0.1:7080/readyz",
        "http://127.0.0.1:7080/metrics",
        "http://127.0.0.1:7081/healthz",
    ]


@pytest.mark.parametrize(
    "pond_url",
    [
        "http://127.0.0.1:0",
        "http://localhost:0",
        "http://[::1]:0",
    ],
)
def test_live_runtime_rejects_an_explicit_zero_pond_port(
    pond_url: str,
) -> None:
    with pytest.raises(
        BackupRuntimeError, match=r"^archive backup preflight failed$"
    ) as raised:
        RuntimeGuard(_drover_config(pond_url=pond_url), minimum_samples=1)

    assert pond_url not in str(raised.value)


@pytest.mark.parametrize(
    ("pond_url", "expected_port"),
    [
        ("http://127.0.0.1:9137", 9137),
        ("http://localhost", 80),
        ("http://[::1]", 80),
    ],
)
def test_live_runtime_accepts_an_explicit_port_and_defaults_only_an_omitted_port(
    pond_url: str, expected_port: int
) -> None:
    clock = _Clock()
    listener_ports: list[int] = []

    def listener_identity(port: int, _deadline: float) -> str:
        listener_ports.append(port)
        return f"process-{len(listener_ports)}"

    guard = RuntimeGuard(
        _drover_config(pond_url=pond_url),
        minimum_samples=1,
        http_get=_http_fake(clock),
        listener_identity=listener_identity,
        clock=clock,
        swap_used=lambda _deadline: 100 * _MIB,
        wait=lambda seconds: clock.advance(seconds),
    )

    guard.capture_baseline()

    assert listener_ports == [7080, 7081, expected_port]


def test_live_runtime_rejects_a_nonloopback_pond_url_without_contacting_it() -> None:
    config = _drover_config()
    private_url = "http://192.0.2.10:9123"
    object.__setattr__(config.archive, "base_url", private_url)
    contacted = False

    def forbidden_http(*_args, **_kwargs):
        nonlocal contacted
        contacted = True
        raise AssertionError("network must not be contacted")

    with pytest.raises(
        BackupRuntimeError, match=r"^archive backup preflight failed$"
    ) as raised:
        RuntimeGuard(config, http_get=forbidden_http)

    assert contacted is False
    assert private_url not in str(raised.value)


@pytest.mark.parametrize("source", ["environment", "config", "file"])
def test_live_runtime_preserves_the_existing_local_api_token_resolution_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    environment_token = "private-environment-token"
    config_token = "private-config-token"
    file_token = "private-file-token"
    token_file = tmp_path / "private-token-file"
    token_file.write_text(file_token, encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setattr(runtime_module, "default_token_file", lambda: token_file)
    if source == "environment":
        monkeypatch.setenv("DROVER_API_TOKEN", environment_token)
        expected = environment_token
    elif source == "config":
        monkeypatch.delenv("DROVER_API_TOKEN", raising=False)
        expected = config_token
    else:
        monkeypatch.delenv("DROVER_API_TOKEN", raising=False)
        config_token = ""
        expected = file_token
    clock = _Clock()
    guard = RuntimeGuard(
        _drover_config(api_token=config_token),
        minimum_samples=1,
        http_get=_http_fake(clock, token=expected),
        listener_identity=_listener_fake,
        clock=clock,
        swap_used=lambda _deadline: 100 * _MIB,
        wait=lambda seconds: clock.advance(seconds),
    )

    guard.capture_baseline()


@pytest.mark.parametrize(
    "metrics",
    [
        b"",
        b"drover_harness_dropped_events_total NaN\n",
        b"drover_harness_dropped_events_total 7.5\n",
        (
            b"drover_harness_dropped_events_total 7\n"
            b'drover_harness_dropped_events_total{host="private"} 7\n'
        ),
        b'drover_harness_dropped_events_total{host="private"} 7\n',
    ],
)
def test_live_runtime_rejects_missing_malformed_or_ambiguous_dropped_metrics(
    metrics: bytes,
) -> None:
    guard = _live_guard(metrics=metrics)

    with pytest.raises(
        BackupRuntimeError, match=r"^archive backup preflight failed$"
    ) as raised:
        guard.capture_baseline()

    assert "private" not in str(raised.value)


def test_live_runtime_rejects_a_listener_pid_reuse_without_exposing_it() -> None:
    clock = _Clock()
    process_starts = {101: "hub-start", 102: "harness-start", 103: "pond-start-a"}
    pond_start_reads = 0

    def command_output(command: tuple[str, ...], deadline: float) -> bytes:
        nonlocal pond_start_reads
        if deadline <= clock():
            raise AssertionError("expired command deadline")
        if not Path(command[0]).is_absolute():
            raise AssertionError("tool path must be absolute")
        if "-iTCP:" in " ".join(command):
            port = int(next(part for part in command if part.startswith("-iTCP:"))[6:])
            pid = {7080: 101, 7081: 102, 9123: 103}[port]
            return f"{pid}\n".encode()

        raise AssertionError("only absolute listener discovery may spawn a tool")

    def process_start(pid: int, deadline: float) -> str:
        nonlocal pond_start_reads
        if deadline <= clock():
            raise AssertionError("expired process-start deadline")
        if pid == 103:
            pond_start_reads += 1
            if pond_start_reads > 2:
                process_starts[103] = "pond-start-b"
        return process_starts[pid]

    guard = _live_guard(
        clock=clock,
        listener_identity=None,
        command_output=command_output,
        process_start=process_start,
    )
    guard.capture_baseline()

    with pytest.raises(
        BackupRuntimeError, match=r"^archive backup local changed$"
    ) as raised:
        guard.sample()

    for private_value in ("103", "pond-start-a", "pond-start-b"):
        assert private_value not in str(raised.value)


@pytest.mark.parametrize("owners", [b"", b"101\n202\n"])
def test_live_runtime_fails_closed_on_zero_or_multiple_listener_owners(
    owners: bytes,
) -> None:
    clock = _Clock()

    def command_output(command: tuple[str, ...], _deadline: float) -> bytes:
        if "-iTCP:" in " ".join(command):
            return owners
        return b"private-start-token\n"

    guard = _live_guard(
        clock=clock,
        listener_identity=None,
        command_output=command_output,
    )

    with pytest.raises(
        BackupRuntimeError, match=r"^archive backup preflight failed$"
    ) as raised:
        guard.capture_baseline()

    assert "101" not in str(raised.value)
    assert "202" not in str(raised.value)
    assert "private-start-token" not in str(raised.value)


def test_live_runtime_rejects_a_start_token_change_during_listener_discovery() -> None:
    clock = _Clock()
    reads = 0

    def command_output(command: tuple[str, ...], _deadline: float) -> bytes:
        if not Path(command[0]).is_absolute() or "-iTCP:" not in " ".join(command):
            raise AssertionError("unexpected OS command")
        return b"101\n"

    def process_start(_pid: int, _deadline: float) -> str:
        nonlocal reads
        reads += 1
        return "private-start-a" if reads == 1 else "private-start-b"

    guard = _live_guard(
        clock=clock,
        listener_identity=None,
        command_output=command_output,
        process_start=process_start,
    )

    with pytest.raises(
        BackupRuntimeError, match=r"^archive backup preflight failed$"
    ) as raised:
        guard.capture_baseline()

    assert "101" not in str(raised.value)
    assert "private-start-a" not in str(raised.value)
    assert "private-start-b" not in str(raised.value)


def test_live_runtime_shares_one_subsecond_deadline_across_all_capture_io() -> None:
    clock = _Clock()

    def slow_http(
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, bytes]:
        if timeout_seconds >= 1.0:
            raise AssertionError("HTTP received the callback budget instead of a slice")
        clock.advance(0.3)
        return _http_fake(clock)(url, headers, timeout_seconds, max_response_bytes)

    guard = RuntimeGuard(
        _drover_config(),
        minimum_samples=1,
        http_get=slow_http,
        listener_identity=_listener_fake,
        clock=clock,
        swap_used=lambda _deadline: 100 * _MIB,
        wait=lambda seconds: clock.advance(seconds),
    )
    started = clock()

    with pytest.raises(BackupRuntimeError, match=r"^archive backup preflight failed$"):
        guard.capture_baseline()

    assert clock() - started < 1.0


def test_live_runtime_maps_all_http_and_parsing_values_to_a_fixed_error() -> None:
    clock = _Clock()
    private_payload = b'{"private-path":"/private/path"}'

    def malformed_http(
        _url: str,
        _headers: Mapping[str, str],
        _timeout_seconds: float,
        _max_response_bytes: int,
    ) -> tuple[int, bytes]:
        return 503, private_payload

    guard = RuntimeGuard(
        _drover_config(),
        minimum_samples=1,
        http_get=malformed_http,
        listener_identity=_listener_fake,
        clock=clock,
        swap_used=lambda _deadline: 100 * _MIB,
        wait=lambda seconds: clock.advance(seconds),
    )

    with pytest.raises(
        BackupRuntimeError, match=r"^archive backup preflight failed$"
    ) as raised:
        guard.capture_baseline()

    assert private_payload.decode() not in str(raised.value)
    assert "/private/path" not in str(raised.value)


def test_bounded_http_get_uses_real_loopback_status_body_and_authorization() -> None:
    reply = _HTTPReply(status=202, body=b"accepted")
    with _loopback_http_server([reply]) as (root, requests):
        status, body = runtime_module._bounded_http_get(
            f"{root}/runtime-check",
            {"Authorization": "Bearer private-test-token"},
            0.5,
            len(reply.body),
        )

    assert status == 202
    assert body == b"accepted"
    assert requests == [("/runtime-check", "Bearer private-test-token")]


def test_bounded_http_get_caps_a_real_loopback_response_body() -> None:
    with _loopback_http_server([_HTTPReply(status=200, body=b"oversized")]) as (
        root,
        _requests,
    ):
        with pytest.raises(
            BackupRuntimeError, match=r"^archive backup preflight failed$"
        ):
            runtime_module._bounded_http_get(f"{root}/body", {}, 0.5, 4)


def test_live_runtime_rejects_a_real_loopback_http_status_without_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_body = b"private status body"
    with _loopback_http_server([_HTTPReply(status=503, body=private_body)]) as (
        root,
        requests,
    ):
        port = int(root.rsplit(":", 1)[1])
        config = dataclasses.replace(_drover_config(), metrics_http_port=port)
        monkeypatch.setattr(runtime_module, "_HARNESS_PORT", port)
        guard = RuntimeGuard(
            config,
            minimum_samples=1,
            listener_identity=lambda listener_port, _deadline: f"owner-{listener_port}",
            swap_used=lambda _deadline: 0,
        )

        with pytest.raises(
            BackupRuntimeError, match=r"^archive backup preflight failed$"
        ) as raised:
            guard.capture_baseline()

    assert requests == [("/healthz", f"Bearer {_PRIVATE_TOKEN}")]
    assert private_body.decode("ascii") not in str(raised.value)


def test_live_runtime_uses_real_loopback_auth_only_for_hub_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = [
        _HTTPReply(status=200, body=b"ok\n"),
        _HTTPReply(status=200, body=b'{"ready":true}'),
        _HTTPReply(status=200, body=b"drover_harness_dropped_events_total 7\n"),
        _HTTPReply(status=200, body=b'{"ok":true,"host_id":"private-host"}'),
    ]
    with _loopback_http_server(replies) as (root, requests):
        port = int(root.rsplit(":", 1)[1])
        config = dataclasses.replace(_drover_config(), metrics_http_port=port)
        monkeypatch.setattr(runtime_module, "_HARNESS_PORT", port)
        guard = RuntimeGuard(
            config,
            minimum_samples=1,
            listener_identity=lambda listener_port, _deadline: f"owner-{listener_port}",
            swap_used=lambda _deadline: 0,
        )

        guard.capture_baseline()

    authorization = f"Bearer {_PRIVATE_TOKEN}"
    assert requests == [
        ("/healthz", authorization),
        ("/readyz", authorization),
        ("/metrics", authorization),
        ("/healthz", None),
    ]


def test_live_runtime_shares_its_deadline_across_real_loopback_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = [
        _HTTPReply(status=200, body=b"ok\n", delay_seconds=0.05),
        _HTTPReply(status=200, body=b'{"ready":true}', delay_seconds=0.05),
        _HTTPReply(
            status=200,
            body=b"drover_harness_dropped_events_total 7\n",
            delay_seconds=0.5,
        ),
    ]
    with _loopback_http_server(replies) as (root, requests):
        port = int(root.rsplit(":", 1)[1])
        config = dataclasses.replace(_drover_config(), metrics_http_port=port)
        monkeypatch.setattr(runtime_module, "_HARNESS_PORT", port)
        monkeypatch.setattr(runtime_module, "_CAPTURE_BUDGET_SECONDS", 0.35)
        monkeypatch.setattr(runtime_module, "_HTTP_SLICE_SECONDS", 0.5)
        guard = RuntimeGuard(
            config,
            minimum_samples=1,
            listener_identity=lambda listener_port, _deadline: f"owner-{listener_port}",
            swap_used=lambda _deadline: 0,
        )
        started = time.monotonic()

        with pytest.raises(
            BackupRuntimeError, match=r"^archive backup preflight failed$"
        ):
            guard.capture_baseline()
        elapsed = time.monotonic() - started

    assert requests[:2] == [
        ("/healthz", f"Bearer {_PRIVATE_TOKEN}"),
        ("/readyz", f"Bearer {_PRIVATE_TOKEN}"),
    ]
    assert len(requests) < 4
    assert elapsed < 0.5


def test_native_process_start_is_stable_and_fails_after_process_exit() -> None:
    first = runtime_module._native_process_start(os.getpid(), time.monotonic() + 1.0)
    second = runtime_module._native_process_start(os.getpid(), time.monotonic() + 1.0)
    assert first == second

    process = subprocess.Popen(
        ("/bin/sleep", "0.2"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        child_token = runtime_module._native_process_start(
            process.pid, time.monotonic() + 1.0
        )
        assert child_token
        process.wait(timeout=1.0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1.0)

    with pytest.raises(BackupRuntimeError, match=r"^archive backup preflight failed$"):
        runtime_module._native_process_start(process.pid, time.monotonic() + 1.0)


def test_darwin_process_start_rejects_a_native_pid_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("native libproc PID validation is available only on Darwin")

    class MismatchedProcPidInfo:
        argtypes: list[object] = []
        restype: object | None = None

        def __call__(
            self,
            pid: int,
            _flavor: int,
            _argument: int,
            address: object,
            size: int,
        ) -> int:
            info = ctypes.cast(
                address, ctypes.POINTER(runtime_module._ProcBSDInfo)
            ).contents
            info.pbi_pid = pid + 1
            info.pbi_start_tvsec = 1
            info.pbi_start_tvusec = 1
            return size

    class MismatchedLibproc:
        proc_pidinfo = MismatchedProcPidInfo()

    monkeypatch.setattr(
        runtime_module.ctypes, "CDLL", lambda *_args, **_kwargs: MismatchedLibproc()
    )

    with pytest.raises(BackupRuntimeError, match=r"^archive backup preflight failed$"):
        runtime_module._darwin_process_start(os.getpid())


def test_linux_process_start_parser_rejects_an_embedded_pid_mismatch() -> None:
    parser = getattr(runtime_module, "_parse_linux_process_start", None)
    assert parser is not None
    stat_bytes = b"321 (private command) " + b" ".join(
        [b"S"] + [b"1"] * 18 + [b"12345"]
    )

    assert parser(321, stat_bytes) == "12345"
    with pytest.raises(BackupRuntimeError, match=r"^archive backup preflight failed$"):
        parser(654, stat_bytes)


def test_bounded_command_times_out_and_reaps_the_real_process() -> None:
    started = time.monotonic()
    with pytest.raises(BackupRuntimeError, match=r"^archive backup preflight failed$"):
        runtime_module._bounded_command_output(
            ("/bin/sleep", "60"),
            time.monotonic() + 0.1,
            clock=time.monotonic,
        )

    assert time.monotonic() - started < 0.5


def test_bounded_command_never_signals_a_group_after_reaping_its_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not all(
        hasattr(os, name)
        for name in ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT")
    ):
        pytest.skip("waitid WNOWAIT reservation check is unavailable")
    real_killpg = os.killpg
    real_waitid = os.waitid
    signals: list[int] = []

    def signal_only_while_reserved(process_group: int, signal_number: int) -> None:
        try:
            real_waitid(
                os.P_PID,
                process_group,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError as error:
            raise AssertionError("process group signaled after leader reap") from error
        signals.append(signal_number)
        real_killpg(process_group, signal_number)

    monkeypatch.setattr(runtime_module.os, "killpg", signal_only_while_reserved)

    output = runtime_module._bounded_command_output(
        ("/bin/echo", "bounded-output"),
        time.monotonic() + 1.0,
        clock=time.monotonic,
    )

    assert output == b"bounded-output\n"
    assert signals


@pytest.mark.parametrize("observer_failure", ["child_process", "os_error"])
def test_bounded_command_never_signals_after_observer_loses_child_ownership(
    monkeypatch: pytest.MonkeyPatch, observer_failure: str
) -> None:
    """Patched at the portable observer seam, not `os.waitid`: macOS has no
    waitid at all, so the platform primitive is no longer the contract."""
    signals: list[tuple[str, int, int]] = []

    def reap_then_fail(process: object) -> bool:
        os.waitpid(process.pid, 0)
        if observer_failure == "child_process":
            raise runtime_module.LeaderGoneError
        raise runtime_module.LeaderObservationError

    def forbidden_killpg(process_group: int, signal_number: int) -> None:
        signals.append(("group", process_group, signal_number))
        raise ProcessLookupError

    def forbidden_kill(pid: int, signal_number: int) -> None:
        signals.append(("process", pid, signal_number))
        raise ProcessLookupError

    monkeypatch.setattr(runtime_module, "observe_leader_exit_unreaped", reap_then_fail)
    monkeypatch.setattr(runtime_module.os, "killpg", forbidden_killpg)
    monkeypatch.setattr(runtime_module.os, "kill", forbidden_kill)

    with pytest.raises(
        BackupRuntimeError, match=r"^archive backup preflight failed$"
    ) as raised:
        runtime_module._bounded_command_output(
            ("/bin/echo", "private-observer-output"),
            time.monotonic() + 1.0,
            clock=time.monotonic,
        )

    assert signals == []
    assert "private-observer-output" not in str(raised.value)


def test_bounded_command_signals_when_observer_fails_but_child_is_still_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity_path = tmp_path / "live-helper-pid"
    code = (
        "import os,time; "
        f"open({str(identity_path)!r}, 'w', encoding='ascii').write(str(os.getpid())); "
        "os.close(1); time.sleep(60)"
    )
    real_killpg = os.killpg
    signals: list[tuple[int, int]] = []
    process_group = 0

    def failed_observation(_process: object) -> bool:
        raise runtime_module.LeaderObservationError

    def recording_killpg(process_group: int, signal_number: int) -> None:
        signals.append((process_group, signal_number))
        real_killpg(process_group, signal_number)

    monkeypatch.setattr(
        runtime_module, "observe_leader_exit_unreaped", failed_observation
    )
    monkeypatch.setattr(runtime_module.os, "killpg", recording_killpg)

    try:
        with pytest.raises(
            BackupRuntimeError, match=r"^archive backup preflight failed$"
        ):
            runtime_module._bounded_command_output(
                (sys.executable, "-c", code),
                time.monotonic() + 1.0,
                clock=time.monotonic,
            )
        process_group = int(_wait_for_path(identity_path))

        assert signals == [(process_group, signal.SIGKILL)]
        assert _wait_for_process_group_exit(process_group)
    finally:
        if process_group and _process_group_exists(process_group):
            real_killpg(process_group, signal.SIGKILL)
            _wait_for_process_group_exit(process_group)


@pytest.mark.parametrize("failure", ["gone", "error"])
def test_tool_leader_observation_fails_closed_without_reaping_fallback(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Both observer outcomes fail closed; neither may fall back to a reaping
    `poll()`. The old "waitid missing" case is now a supported platform
    (macOS) handled inside the observer, not a failure to simulate."""
    observer = getattr(runtime_module, "_tool_leader_exited_without_reap", None)
    assert observer is not None

    class UnreapedProcess:
        pid = 101
        returncode = None

        def poll(self) -> int:
            raise AssertionError("poll would reap the reserved leader")

    def failed_observation(_process: object) -> bool:
        if failure == "gone":
            raise runtime_module.LeaderGoneError
        raise runtime_module.LeaderObservationError

    monkeypatch.setattr(
        runtime_module, "observe_leader_exit_unreaped", failed_observation
    )

    with pytest.raises(BackupRuntimeError, match=r"^archive backup preflight failed$"):
        observer(UnreapedProcess())


def test_tool_cleanup_maps_reap_failure_without_popen_signal_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReapFailureProcess:
        pid = 101
        returncode = None
        wait_calls = 0

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            raise AssertionError("Popen.kill polls and may reap the leader")

        def wait(self, timeout: float) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired("private-test-command", timeout)
            raise OSError

    process = ReapFailureProcess()
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        runtime_module.os,
        "killpg",
        lambda process_group, signal_number: signals.append(
            (process_group, signal_number)
        ),
    )

    with pytest.raises(BackupRuntimeError, match=r"^archive backup preflight failed$"):
        runtime_module._reap_tool(process)

    assert signals == [(101, signal.SIGKILL), (101, signal.SIGKILL)]
    assert process.wait_calls == 2


def test_bounded_command_caps_real_process_output() -> None:
    command = (
        sys.executable,
        "-c",
        "import os; os.write(1, b'x' * 70000)",
    )

    with pytest.raises(BackupRuntimeError, match=r"^archive backup preflight failed$"):
        runtime_module._bounded_command_output(
            command,
            time.monotonic() + 1.0,
            clock=time.monotonic,
        )


def test_bounded_command_kills_an_orphaned_real_process_group(
    tmp_path: Path,
) -> None:
    identity_path = tmp_path / "process-identities"
    code = (
        "import os,time; "
        "child=os.fork(); "
        f"path={str(identity_path)!r}; "
        "(time.sleep(60) if child == 0 else "
        "(open(path, 'w', encoding='ascii').write(f'{os.getpid()} {child}'), "
        "os._exit(0)))"
    )
    process_group = 0
    try:
        with pytest.raises(
            BackupRuntimeError, match=r"^archive backup preflight failed$"
        ):
            runtime_module._bounded_command_output(
                (sys.executable, "-c", code),
                time.monotonic() + 0.3,
                clock=time.monotonic,
            )
        process_group, _child = map(int, _wait_for_path(identity_path).split())

        assert _wait_for_process_group_exit(process_group)
    finally:
        if process_group and _process_group_exists(process_group):
            os.killpg(process_group, signal.SIGKILL)
            _wait_for_process_group_exit(process_group)


def test_real_ephemeral_listener_has_one_stable_opaque_owner() -> None:
    if not Path(runtime_module._LSOF_PATH).is_file():
        pytest.skip(f"listener discovery tool unavailable on {sys.platform}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        first = runtime_module._listener_process_identity(
            port,
            time.monotonic() + 2.0,
            lambda command, deadline: runtime_module._bounded_command_output(
                command, deadline, clock=time.monotonic
            ),
            runtime_module._native_process_start,
        )
        second = runtime_module._listener_process_identity(
            port,
            time.monotonic() + 2.0,
            lambda command, deadline: runtime_module._bounded_command_output(
                command, deadline, clock=time.monotonic
            ),
            runtime_module._native_process_start,
        )

    assert first == second
    assert len(first) == 64
    assert str(os.getpid()) not in first


def test_direct_platform_swap_read_is_bounded_and_nonnegative() -> None:
    started = time.monotonic()
    used = runtime_module._system_swap_used(
        time.monotonic() + 1.0,
        lambda command, deadline: runtime_module._bounded_command_output(
            command, deadline, clock=time.monotonic
        ),
    )

    assert isinstance(used, int)
    assert used >= 0
    assert time.monotonic() - started < 1.0
