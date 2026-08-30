"""Live-state and same-host single-flight gates for Pond backups."""

from __future__ import annotations

import dataclasses
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

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
            return f"p{pid}\n".encode()

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


@pytest.mark.parametrize("owners", [b"", b"p101\np202\n"])
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
        return b"p101\n"

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
