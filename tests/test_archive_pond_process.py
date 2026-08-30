"""Shared, bounded Pond subprocess supervision contract."""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from drover.server.archive import pond_process as pond_process_module
from drover.server.archive.pond_process import (
    PondProcessError,
    ResourceLimits,
    ResourceSample,
    pond_child_environment,
    require_pinned_pond,
    run_pond_process,
    sample_process_group,
)

_MIB = 1024 * 1024
_PRIVATE_REMOTE = "s3+https://private-account.example/private-generation"
_PRIVATE_ENVIRONMENT = "private-environment-value"

_FAKE_POND = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

record_path = Path(os.environ["FAKE_RUN_RECORD"])
try:
    calls = json.loads(record_path.read_text(encoding="utf-8"))
except FileNotFoundError:
    calls = []
calls.append({"argv": sys.argv, "secret": os.environ.get("PRIVATE_CHILD_VALUE")})
record_path.write_text(json.dumps(calls), encoding="utf-8")

mode = sys.argv[1]
if mode == "inspect":
    print(json.dumps({
        "argv": sys.argv,
        "pid": os.getpid(),
        "pgid": os.getpgid(0),
        "sid": os.getsid(0),
    }))
elif mode == "wait":
    time.sleep(0.3)
elif mode == "nonzero":
    print("private child stdout")
    print("private child stderr", file=sys.stderr)
    raise SystemExit(17)
elif mode == "write_immediate":
    Path(os.environ["FAKE_IMMEDIATE_OUTPUT"]).write_text("created", encoding="utf-8")
elif mode == "overflow_stdout":
    sys.stdout.buffer.write(b"x" * (32 * 1024 * 1024 + 1))
    sys.stdout.buffer.flush()
    time.sleep(30)
elif mode == "overflow_artifact":
    artifact = Path(os.environ["FAKE_ARTIFACT"])
    artifact.write_bytes(b"x" * (32 * 1024 * 1024 + 1))
    artifact.chmod(0o600)
    time.sleep(30)
elif mode in {"orphan", "stubborn_orphan", "artifact_mutating_orphan"}:
    if mode == "artifact_mutating_orphan":
        artifact = Path(os.environ["FAKE_ARTIFACT"])
        artifact.write_text("initial", encoding="utf-8")
        artifact.chmod(0o600)
    leader_pgid_path = os.environ.get("FAKE_LEADER_PGID")
    if leader_pgid_path:
        Path(leader_pgid_path).write_text(str(os.getpgid(0)), encoding="utf-8")
    child_code = r'''import os
from pathlib import Path
import signal
import time

pid_path = Path(os.environ["FAKE_DESCENDANT_PID"])
marker_path = Path(os.environ["FAKE_DESCENDANT_MARKER"])
stubborn = os.environ.get("FAKE_DESCENDANT_STUBBORN") == "1"

def terminate(_signum, _frame):
    marker_path.write_text("term", encoding="utf-8")
    artifact_path = os.environ.get("FAKE_ARTIFACT")
    if artifact_path:
        Path(artifact_path).chmod(0o644)
    if not stubborn:
        raise SystemExit(0)

signal.signal(signal.SIGTERM, terminate)
pid_path.write_text(str(os.getpid()), encoding="utf-8")
pgid_path = os.environ.get("FAKE_DESCENDANT_PGID")
if pgid_path:
    Path(pgid_path).write_text(str(os.getpgid(0)), encoding="utf-8")
time.sleep(30)
'''
    subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.DEVNULL if mode == "artifact_mutating_orphan" else sys.stdout,
        stderr=subprocess.DEVNULL if mode == "artifact_mutating_orphan" else sys.stderr,
        env=os.environ,
    )
    deadline = time.monotonic() + 2
    while not Path(os.environ["FAKE_DESCENDANT_PID"]).exists():
        if time.monotonic() >= deadline:
            raise SystemExit(9)
        time.sleep(0.01)
else:
    raise SystemExit(8)
"""


@pytest.fixture
def fake_pond(tmp_path: Path) -> tuple[Path, Path]:
    binary = tmp_path / "pond; no-shell-injection"
    binary.write_text(textwrap.dedent(_FAKE_POND), encoding="utf-8")
    binary.chmod(0o700)
    record = tmp_path / "calls.json"
    return binary, record


def _environment(record: Path, **overrides: str) -> dict[str, str]:
    return {
        "FAKE_RUN_RECORD": str(record),
        "PRIVATE_CHILD_VALUE": _PRIVATE_ENVIRONMENT,
        **overrides,
    }


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_pid_exit(pid: int) -> None:
    deadline = time.monotonic() + 3
    while _pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.01)


def test_runner_uses_the_pinned_private_artifact_directly_in_a_new_process_group(
    fake_pond: tuple[Path, Path], tmp_path: Path
) -> None:
    binary, record = fake_pond
    run_directory = tmp_path / "private-run"

    result = run_pond_process(
        binary,
        ("inspect", _PRIVATE_REMOTE),
        timeout_seconds=5,
        run_directory=run_directory,
        label="copy",
        env=_environment(record),
    )

    observation = json.loads(result.stdout_path.read_text(encoding="utf-8"))
    calls = json.loads(record.read_text(encoding="utf-8"))
    assert len(calls) == 1
    assert calls[0]["argv"][1:] == ["inspect", _PRIVATE_REMOTE]
    assert calls[0]["secret"] == _PRIVATE_ENVIRONMENT
    artifact = Path(calls[0]["argv"][0])
    assert artifact.parent.parent == run_directory
    assert artifact.parent.name.startswith(".drover-pond-tool-")
    assert artifact.name.startswith(".drover-pond-executable-")
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o500
    assert observation["argv"] == calls[0]["argv"]
    assert observation["pid"] == observation["pgid"] == observation["sid"]
    assert not (tmp_path / "no-shell-injection").exists()
    assert stat.S_IMODE(run_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.stdout_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.stderr_path.stat().st_mode) == 0o600


def test_runner_keeps_output_directory_writable_during_spawn_handshake(
    fake_pond: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary, record = fake_pond
    run_directory = tmp_path / "immediate-output-run"
    immediate_output = run_directory / "immediate-output.json"
    real_popen = subprocess.Popen

    def await_immediate_output(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        deadline = time.monotonic() + 1
        while not immediate_output.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return process

    monkeypatch.setattr(pond_process_module.subprocess, "Popen", await_immediate_output)

    result = run_pond_process(
        binary,
        ("write_immediate",),
        timeout_seconds=5,
        run_directory=run_directory,
        label="copy",
        env=_environment(record, FAKE_IMMEDIATE_OUTPUT=str(immediate_output)),
        resource_sampler=lambda _process_group: ResourceSample(1, 2, 3),
    )

    assert result.returncode == 0
    assert immediate_output.read_text(encoding="utf-8") == "created"
    assert stat.S_IMODE(run_directory.stat().st_mode) == 0o700


def test_runner_hardens_only_the_artifact_directory_during_spawn(
    fake_pond: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary, record = fake_pond
    run_directory = tmp_path / "protected-spawn-run"
    real_popen = subprocess.Popen
    parent_modes: list[int] = []
    run_modes: list[int] = []
    artifact_directories: list[Path] = []

    def record_parent_mode(*args, **kwargs):
        artifact = Path(kwargs["executable"])
        artifact_directories.append(artifact.parent)
        parent_modes.append(stat.S_IMODE(artifact.parent.stat().st_mode))
        run_modes.append(stat.S_IMODE(run_directory.stat().st_mode))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(pond_process_module.subprocess, "Popen", record_parent_mode)

    result = run_pond_process(
        binary,
        ("inspect",),
        timeout_seconds=5,
        run_directory=run_directory,
        label="copy",
        env=_environment(record),
        resource_sampler=lambda _process_group: ResourceSample(1, 2, 3),
    )

    assert result.returncode == 0
    assert parent_modes == [0o500]
    assert run_modes == [0o700]
    assert len(artifact_directories) == 1
    assert artifact_directories[0].parent == run_directory
    assert artifact_directories[0].name.startswith(".drover-pond-tool-")
    assert stat.S_IMODE(artifact_directories[0].stat().st_mode) == 0o700
    assert stat.S_IMODE(run_directory.stat().st_mode) == 0o700


def test_runner_restores_and_closes_artifact_directory_after_partial_begin_failure(
    fake_pond: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary, record = fake_pond
    run_directory = tmp_path / "partial-begin-run"
    original = (
        pond_process_module._PinnedPondExecutable._require_artifact_directory_mode
    )
    captured: list[tuple[int, Path]] = []

    def fail_after_hardening(self, mode):
        if mode == 0o500:
            captured.append(
                (self._artifact_directory_descriptor, self._artifact_path.parent)
            )
            raise OSError("private injected begin failure")
        return original(self, mode)

    monkeypatch.setattr(
        pond_process_module._PinnedPondExecutable,
        "_require_artifact_directory_mode",
        fail_after_hardening,
    )

    with pytest.raises(PondProcessError, match=r"^binary$"):
        run_pond_process(
            binary,
            ("inspect",),
            timeout_seconds=5,
            run_directory=run_directory,
            label="copy",
            env=_environment(record),
        )

    assert len(captured) == 1
    descriptor, artifact_directory = captured[0]
    assert stat.S_IMODE(run_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(artifact_directory.stat().st_mode) == 0o700
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert not record.exists()


def test_runner_restores_and_closes_artifact_directory_after_popen_error(
    fake_pond: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary, record = fake_pond
    run_directory = tmp_path / "popen-error-run"
    captured: list[tuple[int, Path, int, int]] = []
    original = pond_process_module._PinnedPondExecutable.begin_spawn

    def capture_hardened_directory(self):
        original(self)
        captured.append(
            (
                self._artifact_directory_descriptor,
                self._artifact_path.parent,
                stat.S_IMODE(self._artifact_path.parent.stat().st_mode),
                stat.S_IMODE(run_directory.stat().st_mode),
            )
        )

    monkeypatch.setattr(
        pond_process_module._PinnedPondExecutable,
        "begin_spawn",
        capture_hardened_directory,
    )
    monkeypatch.setattr(
        pond_process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("private injected")),
    )

    with pytest.raises(PondProcessError, match=r"^subprocess$"):
        run_pond_process(
            binary,
            ("inspect",),
            timeout_seconds=5,
            run_directory=run_directory,
            label="copy",
            env=_environment(record),
        )

    assert len(captured) == 1
    descriptor, artifact_directory, artifact_mode, run_mode = captured[0]
    assert artifact_mode == 0o500
    assert run_mode == 0o700
    assert stat.S_IMODE(artifact_directory.stat().st_mode) == 0o700
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert not record.exists()


def test_retained_executable_pin_detects_a_swap_restore_during_process(
    fake_pond: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    binary, record = fake_pond
    moved = binary.with_name("moved-private-pond")
    swapped = binary.with_name("swapped-private-pond")
    swapped.write_text(textwrap.dedent(_FAKE_POND), encoding="utf-8")
    swapped.chmod(0o700)
    swapped_once = False

    def swap_restore() -> None:
        nonlocal swapped_once
        if swapped_once:
            return
        swapped_once = True
        binary.rename(moved)
        swapped.rename(binary)
        binary.rename(swapped)
        moved.rename(binary)

    with pond_process_module._pin_pond_executable(binary) as executable:
        with pytest.raises(PondProcessError, match=r"^binary$"):
            run_pond_process(
                executable,
                ("wait",),
                timeout_seconds=5,
                run_directory=tmp_path / "retained-pin-run",
                label="copy",
                env=_environment(record),
                progress_callback=swap_restore,
            )


def test_retained_executable_never_runs_a_path_swapped_at_spawn(
    fake_pond: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary, record = fake_pond
    moved = binary.with_name("approved-private-pond")
    unapproved = binary.with_name("unapproved-private-pond")
    marker = tmp_path / "unapproved-executed"
    unapproved.write_text(
        '#!/bin/sh\nprintf unapproved > "$UNAPPROVED_MARKER"\n',
        encoding="utf-8",
    )
    unapproved.chmod(0o700)
    real_popen = subprocess.Popen
    swapped = False

    def swap_before_spawn(*args, **kwargs):
        nonlocal swapped
        assert not swapped
        swapped = True
        binary.rename(moved)
        unapproved.rename(binary)
        try:
            process = real_popen(*args, **kwargs)
            deadline = time.monotonic() + 1
            while (
                not marker.exists()
                and not record.exists()
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            return process
        finally:
            binary.rename(unapproved)
            moved.rename(binary)

    monkeypatch.setattr(pond_process_module.subprocess, "Popen", swap_before_spawn)

    with pond_process_module._pin_pond_executable(binary) as executable:
        with pytest.raises(PondProcessError, match=r"^binary$"):
            run_pond_process(
                executable,
                ("inspect", _PRIVATE_REMOTE),
                timeout_seconds=5,
                run_directory=tmp_path / "spawn-pin-run",
                label="copy",
                env=_environment(record, UNAPPROVED_MARKER=str(marker)),
            )

    assert swapped
    assert not marker.exists()
    calls = json.loads(record.read_text(encoding="utf-8"))
    assert len(calls) == 1
    assert calls[0]["argv"][1:] == ["inspect", _PRIVATE_REMOTE]
    assert calls[0]["secret"] == _PRIVATE_ENVIRONMENT
    artifact = Path(calls[0]["argv"][0])
    assert artifact.parent.parent == tmp_path / "spawn-pin-run"
    assert artifact.parent.name.startswith(".drover-pond-tool-")
    assert artifact.name.startswith(".drover-pond-executable-")
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o500


@pytest.mark.parametrize("kind", ["missing", "not_executable"])
def test_require_pinned_pond_rejects_an_invalid_binary_without_disclosing_it(
    fake_pond: tuple[Path, Path], tmp_path: Path, kind: str
) -> None:
    binary, _ = fake_pond
    if kind == "missing":
        binary = tmp_path / "private-missing-pond"
    else:
        binary.chmod(0o600)

    with pytest.raises(PondProcessError, match=r"^binary$") as raised:
        require_pinned_pond(binary)

    assert str(binary) not in str(raised.value)


def test_runner_preserves_a_nonzero_exit_once_and_retains_only_private_evidence(
    fake_pond: tuple[Path, Path], tmp_path: Path
) -> None:
    binary, record = fake_pond

    result = run_pond_process(
        binary,
        ("nonzero", _PRIVATE_REMOTE),
        timeout_seconds=5,
        run_directory=tmp_path / "run",
        label="verify",
        env=_environment(record),
    )

    assert result.returncode == 17
    assert len(json.loads(record.read_text(encoding="utf-8"))) == 1
    assert result.stdout_path.read_text(encoding="utf-8") == "private child stdout\n"
    assert result.stderr_path.read_text(encoding="utf-8") == "private child stderr\n"
    assert {field.name for field in dataclasses.fields(result)} == {
        "returncode",
        "duration_ms",
        "peak_rss_bytes",
        "peak_physical_bytes",
        "swap_delta_bytes",
        "stdout_path",
        "stderr_path",
    }
    assert _PRIVATE_REMOTE not in repr(result)
    assert _PRIVATE_ENVIRONMENT not in repr(result)


@pytest.mark.parametrize(
    ("mode", "artifact"),
    [("overflow_stdout", False), ("overflow_artifact", True)],
)
def test_runner_caps_each_child_stream_and_artifact_at_32_mib(
    fake_pond: tuple[Path, Path], tmp_path: Path, mode: str, artifact: bool
) -> None:
    binary, record = fake_pond
    run_directory = tmp_path / "run"
    artifact_path = run_directory / "private-artifact.ndjson" if artifact else None
    environment = _environment(record)
    if artifact_path is not None:
        environment["FAKE_ARTIFACT"] = str(artifact_path)

    with pytest.raises(PondProcessError, match=r"^size$") as raised:
        run_pond_process(
            binary,
            (mode, _PRIVATE_REMOTE),
            timeout_seconds=5,
            run_directory=run_directory,
            label="bounded",
            env=environment,
            artifact_path=artifact_path,
        )

    assert _PRIVATE_REMOTE not in str(raised.value)
    assert len(json.loads(record.read_text(encoding="utf-8"))) == 1
    assert (run_directory / "bounded.stdout").stat().st_size <= 32 * _MIB
    assert (run_directory / "bounded.stderr").stat().st_size <= 32 * _MIB


def test_timeout_terms_then_kills_a_stubborn_orphan_after_its_leader_exits(
    fake_pond: tuple[Path, Path], tmp_path: Path
) -> None:
    binary, record = fake_pond
    pid_path = tmp_path / "descendant.pid"
    marker_path = tmp_path / "descendant.marker"
    leader_pgid_path = tmp_path / "leader.pgid"
    descendant_pgid_path = tmp_path / "descendant.pgid"
    descendant_pid: int | None = None
    try:
        with pytest.raises(PondProcessError, match=r"^timeout$"):
            run_pond_process(
                binary,
                ("stubborn_orphan",),
                timeout_seconds=0.5,
                run_directory=tmp_path / "run",
                label="timeout",
                env=_environment(
                    record,
                    FAKE_DESCENDANT_PID=str(pid_path),
                    FAKE_DESCENDANT_MARKER=str(marker_path),
                    FAKE_DESCENDANT_STUBBORN="1",
                    FAKE_LEADER_PGID=str(leader_pgid_path),
                    FAKE_DESCENDANT_PGID=str(descendant_pgid_path),
                ),
            )
        descendant_pid = int(pid_path.read_text(encoding="utf-8"))
        _wait_for_pid_exit(descendant_pid)
        assert marker_path.read_text(encoding="utf-8") == "term"
        assert descendant_pgid_path.read_text(encoding="utf-8") == (
            leader_pgid_path.read_text(encoding="utf-8")
        )
        assert not _pid_exists(descendant_pid)
    finally:
        if descendant_pid is not None and _pid_exists(descendant_pid):
            os.kill(descendant_pid, signal.SIGKILL)


@pytest.mark.parametrize("breach", ["rss", "physical", "swap", "unavailable"])
def test_resource_breach_kills_the_whole_group_and_returns_no_child_text(
    fake_pond: tuple[Path, Path], tmp_path: Path, breach: str
) -> None:
    binary, record = fake_pond
    pid_path = tmp_path / "descendant.pid"
    marker_path = tmp_path / "descendant.marker"
    descendant_pid: int | None = None
    limits = ResourceLimits(
        max_rss_bytes=100,
        max_physical_bytes=200,
        max_swap_growth_bytes=300,
    )

    def sample(_pgid: int) -> ResourceSample:
        if not pid_path.exists():
            return ResourceSample(50, 150, 100)
        if breach == "rss":
            return ResourceSample(101, 150, 100)
        if breach == "physical":
            return ResourceSample(50, 201, 100)
        if breach == "swap":
            return ResourceSample(50, 150, 401)
        return ResourceSample(50, None, 100)

    try:
        with pytest.raises(PondProcessError, match=r"^resource$") as raised:
            run_pond_process(
                binary,
                ("orphan", _PRIVATE_REMOTE),
                timeout_seconds=5,
                run_directory=tmp_path / "run",
                label="copy",
                env=_environment(
                    record,
                    FAKE_DESCENDANT_PID=str(pid_path),
                    FAKE_DESCENDANT_MARKER=str(marker_path),
                ),
                resource_limits=limits,
                resource_sampler=sample,
            )
        descendant_pid = int(pid_path.read_text(encoding="utf-8"))
        _wait_for_pid_exit(descendant_pid)
        assert marker_path.read_text(encoding="utf-8") == "term"
        assert not _pid_exists(descendant_pid)
        assert _PRIVATE_REMOTE not in str(raised.value)
        assert _PRIVATE_ENVIRONMENT not in str(raised.value)
        assert "private child" not in str(raised.value)
        assert len(json.loads(record.read_text(encoding="utf-8"))) == 1
    finally:
        if descendant_pid is not None and _pid_exists(descendant_pid):
            os.kill(descendant_pid, signal.SIGKILL)


def test_runner_samples_resources_and_calls_progress_throughout_supervision(
    fake_pond: tuple[Path, Path], tmp_path: Path
) -> None:
    binary, record = fake_pond
    samples = [
        ResourceSample(10, 20, 100),
        ResourceSample(30, 40, 110),
        ResourceSample(25, 35, 160),
    ]
    sample_count = 0
    progress_count = 0

    def sample(_pgid: int) -> ResourceSample:
        nonlocal sample_count
        value = samples[min(sample_count, len(samples) - 1)]
        sample_count += 1
        return value

    def progress() -> None:
        nonlocal progress_count
        progress_count += 1

    result = run_pond_process(
        binary,
        ("wait",),
        timeout_seconds=5,
        run_directory=tmp_path / "run",
        label="sampled",
        env=_environment(record),
        resource_sampler=sample,
        progress_callback=progress,
    )

    assert sample_count >= 3
    assert progress_count >= 3
    assert result.peak_rss_bytes == 30
    assert result.peak_physical_bytes == 40
    assert result.swap_delta_bytes == 60


def test_result_keeps_physical_peak_unavailable_after_any_unavailable_sample(
    fake_pond: tuple[Path, Path], tmp_path: Path
) -> None:
    binary, record = fake_pond
    samples = [
        ResourceSample(10, 20, 100),
        ResourceSample(20, None, 110),
        ResourceSample(30, 40, 120),
    ]
    sample_count = 0

    def sample(_pgid: int) -> ResourceSample:
        nonlocal sample_count
        value = samples[min(sample_count, len(samples) - 1)]
        sample_count += 1
        return value

    result = run_pond_process(
        binary,
        ("wait",),
        timeout_seconds=5,
        run_directory=tmp_path / "run",
        label="physical-unavailable",
        env=_environment(record),
        resource_sampler=sample,
    )

    assert sample_count >= 3
    assert result.peak_physical_bytes is None


def test_progress_failure_cleans_the_orphan_group_and_propagates_unchanged(
    fake_pond: tuple[Path, Path], tmp_path: Path
) -> None:
    class ProgressFailure(RuntimeError):
        pass

    binary, record = fake_pond
    pid_path = tmp_path / "descendant.pid"
    marker_path = tmp_path / "descendant.marker"
    descendant_pid: int | None = None

    def progress() -> None:
        if pid_path.exists():
            raise ProgressFailure("fixed progress failure")

    try:
        with pytest.raises(ProgressFailure, match=r"^fixed progress failure$"):
            run_pond_process(
                binary,
                ("orphan",),
                timeout_seconds=5,
                run_directory=tmp_path / "run",
                label="progress",
                env=_environment(
                    record,
                    FAKE_DESCENDANT_PID=str(pid_path),
                    FAKE_DESCENDANT_MARKER=str(marker_path),
                ),
                progress_callback=progress,
            )
        descendant_pid = int(pid_path.read_text(encoding="utf-8"))
        _wait_for_pid_exit(descendant_pid)
        assert marker_path.read_text(encoding="utf-8") == "term"
        assert not _pid_exists(descendant_pid)
    finally:
        if descendant_pid is not None and _pid_exists(descendant_pid):
            os.kill(descendant_pid, signal.SIGKILL)


def test_cleanup_refuses_to_return_while_the_process_group_is_still_live(
    fake_pond: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary, record = fake_pond
    monkeypatch.setattr(pond_process_module, "_TERMINATE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(pond_process_module, "_POLL_SECONDS", 0.001)
    monkeypatch.setattr(
        pond_process_module, "_process_group_exists", lambda _process_group: True
    )

    with pytest.raises(PondProcessError, match=r"^subprocess$") as raised:
        run_pond_process(
            binary,
            ("inspect", _PRIVATE_REMOTE),
            timeout_seconds=5,
            run_directory=tmp_path / "run",
            label="cleanup",
            env=_environment(record),
        )

    assert _PRIVATE_REMOTE not in str(raised.value)


def test_cleanup_refuses_to_return_when_the_leader_cannot_be_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnreapableProcess:
        returncode = None

        def wait(self, timeout: float) -> int:
            raise subprocess.TimeoutExpired("private-command", timeout)

    monkeypatch.setattr(
        pond_process_module, "_signal_process_group", lambda *_args: None
    )
    monkeypatch.setattr(
        pond_process_module, "_process_group_exists", lambda _process_group: False
    )

    with pytest.raises(PondProcessError, match=r"^subprocess$") as raised:
        pond_process_module._stop_process(UnreapableProcess(), 987_654)

    assert "private-command" not in str(raised.value)
    assert "987654" not in str(raised.value)


def test_cleanup_sanitizes_a_second_leader_reap_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReapFailureProcess:
        returncode = None
        waits = 0

        def wait(self, timeout: float) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("private-command", timeout)
            raise OSError("private wait failure")

    monkeypatch.setattr(
        pond_process_module, "_signal_process_group", lambda *_args: None
    )
    monkeypatch.setattr(
        pond_process_module, "_process_group_exists", lambda _process_group: False
    )

    with pytest.raises(PondProcessError, match=r"^subprocess$") as raised:
        pond_process_module._stop_process(ReapFailureProcess(), 987_654)

    assert "private" not in str(raised.value)
    assert "987654" not in str(raised.value)


def test_leader_exit_is_observed_without_reaping_until_group_cleanup() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 2
        while (
            not pond_process_module._leader_exited_without_reap(process)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        assert process.returncode is None
        assert pond_process_module._process_group_exists(process.pid)
        assert pond_process_module._stop_process(process, process.pid) == 0
        assert not pond_process_module._process_group_exists(process.pid)
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def test_process_group_signal_fallback_does_not_poll_or_reap_the_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReservedLeader:
        pid = 987_654

        def send_signal(self, _signal_number: int) -> None:
            raise AssertionError("Popen.send_signal polls and may reap the leader")

    direct_signals: list[tuple[int, int]] = []

    def missing_group(_process_group: int, _signal_number: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(pond_process_module.os, "killpg", missing_group)
    monkeypatch.setattr(
        pond_process_module.os,
        "kill",
        lambda pid, signal_number: direct_signals.append((pid, signal_number)),
    )

    pond_process_module._signal_process_group(ReservedLeader(), 987_654, signal.SIGKILL)

    assert direct_signals == [(987_654, signal.SIGKILL)]


def test_process_group_liveness_probe_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable_probe(_process_group: int, _signal_number: int) -> None:
        raise OSError("private liveness probe failure")

    monkeypatch.setattr(pond_process_module.os, "killpg", unavailable_probe)

    assert pond_process_module._process_group_exists(987_654)


def test_descendant_artifact_mutation_is_revalidated_after_group_cleanup(
    fake_pond: tuple[Path, Path], tmp_path: Path
) -> None:
    binary, record = fake_pond
    run_directory = tmp_path / "run"
    artifact_path = run_directory / "private-artifact.ndjson"
    pid_path = tmp_path / "descendant.pid"
    marker_path = tmp_path / "descendant.marker"
    descendant_pid: int | None = None
    try:
        with pytest.raises(PondProcessError, match=r"^artifact$") as raised:
            run_pond_process(
                binary,
                ("artifact_mutating_orphan", _PRIVATE_REMOTE),
                timeout_seconds=5,
                run_directory=run_directory,
                label="artifact-cleanup",
                env=_environment(
                    record,
                    FAKE_ARTIFACT=str(artifact_path),
                    FAKE_DESCENDANT_PID=str(pid_path),
                    FAKE_DESCENDANT_MARKER=str(marker_path),
                ),
                artifact_path=artifact_path,
            )
        descendant_pid = int(pid_path.read_text(encoding="utf-8"))
        _wait_for_pid_exit(descendant_pid)
        assert marker_path.read_text(encoding="utf-8") == "term"
        assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o644
        assert _PRIVATE_REMOTE not in str(raised.value)
        assert not _pid_exists(descendant_pid)
    finally:
        if descendant_pid is not None and _pid_exists(descendant_pid):
            os.kill(descendant_pid, signal.SIGKILL)


def test_initial_swap_sample_precedes_progress_and_detects_callback_growth(
    fake_pond: tuple[Path, Path], tmp_path: Path
) -> None:
    binary, record = fake_pond
    swap_used = 100
    events: list[str] = []
    limits = ResourceLimits(1_000, 1_000, 300)

    def sample(_process_group: int) -> ResourceSample:
        events.append("sample")
        return ResourceSample(10, 20, swap_used)

    def progress() -> None:
        nonlocal swap_used
        events.append("progress")
        swap_used = 401

    with pytest.raises(PondProcessError, match=r"^resource$"):
        run_pond_process(
            binary,
            ("wait",),
            timeout_seconds=5,
            run_directory=tmp_path / "run",
            label="initial-swap",
            env=_environment(record),
            resource_limits=limits,
            resource_sampler=sample,
            progress_callback=progress,
        )

    assert events[:2] == ["sample", "progress"]


def test_progress_callback_has_a_fixed_elapsed_budget(
    fake_pond: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary, record = fake_pond
    monkeypatch.setattr(pond_process_module, "_MAX_PROGRESS_CALLBACK_SECONDS", 0.02)

    def blocked_progress() -> None:
        time.sleep(0.05)

    started = time.monotonic()
    with pytest.raises(PondProcessError, match=r"^timeout$"):
        run_pond_process(
            binary,
            ("wait",),
            timeout_seconds=5,
            run_directory=tmp_path / "run",
            label="blocked-progress",
            env=_environment(record),
            resource_sampler=lambda _process_group: ResourceSample(1, 1, 1),
            progress_callback=blocked_progress,
        )

    assert time.monotonic() - started < 0.8


def test_progress_callback_completion_rechecks_the_process_deadline(
    fake_pond: tuple[Path, Path], tmp_path: Path
) -> None:
    binary, record = fake_pond

    def blocked_progress() -> None:
        time.sleep(0.05)

    with pytest.raises(PondProcessError, match=r"^timeout$"):
        run_pond_process(
            binary,
            ("wait",),
            timeout_seconds=0.02,
            run_directory=tmp_path / "run",
            label="callback-deadline",
            env=_environment(record),
            resource_sampler=lambda _process_group: ResourceSample(1, 1, 1),
            progress_callback=blocked_progress,
        )


def test_child_environment_removes_an_inherited_remote_storage_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POND_STORAGE_PATH", _PRIVATE_REMOTE)

    child = pond_child_environment(
        {"POND_STORAGE_PATH": _PRIVATE_REMOTE, "SAFE_OVERRIDE": "present"}
    )

    assert "POND_STORAGE_PATH" not in child
    assert child["SAFE_OVERRIDE"] == "present"


def test_linux_sampler_sums_group_rss_and_reports_physical_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text(
        "MemTotal: 1000 kB\nSwapTotal: 100 kB\nSwapFree: 25 kB\n",
        encoding="ascii",
    )
    for pid, pgid, rss_kib in (("101", 77, 12), ("202", 77, 34), ("303", 88, 99)):
        directory = proc / pid
        directory.mkdir()
        directory.joinpath("stat").write_text(
            f"{pid} (pond worker) S 1 {pgid} 0 0 0\n", encoding="ascii"
        )
        directory.joinpath("status").write_text(
            f"Name:\tpond\nVmRSS:\t{rss_kib} kB\n", encoding="ascii"
        )
    monkeypatch.setattr(pond_process_module, "_PROC_ROOT", proc)
    monkeypatch.setattr(sys, "platform", "linux")

    sample = sample_process_group(77)

    assert sample == ResourceSample(
        rss_bytes=(12 + 34) * 1024,
        physical_bytes=None,
        swap_used_bytes=75 * 1024,
    )


def test_darwin_sampler_uses_bounded_absolute_tools_and_phys_footprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def command_output(command: tuple[str, ...]) -> bytes:
        if command == ("/bin/ps", "-axo", "pid=,pgid=,rss="):
            return b"101 77 12\n202 77 34\n303 88 99\n"
        if command == ("/usr/sbin/sysctl", "-n", "vm.swapusage"):
            return b"total = 1024.00M  used = 12.50M  free = 1011.50M\n"
        raise AssertionError(f"unexpected command shape: {command!r}")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(pond_process_module, "_bounded_command_output", command_output)
    monkeypatch.setattr(
        pond_process_module,
        "_darwin_physical_footprint",
        lambda pids: 9000 if pids == (101, 202) else 0,
    )

    sample = sample_process_group(77)

    assert sample == ResourceSample(
        rss_bytes=(12 + 34) * 1024,
        physical_bytes=9000,
        swap_used_bytes=int(12.5 * _MIB),
    )


def test_darwin_physical_footprint_is_unavailable_if_any_member_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProcPidRusage:
        argtypes = None
        restype = None

        def __call__(self, pid: int, _version: int, info_pointer) -> int:
            if pid == 101:
                info_pointer._obj.ri_phys_footprint = 9000
                return 0
            return -1

    class LibProc:
        proc_pid_rusage = ProcPidRusage()

    monkeypatch.setattr(
        pond_process_module.ctypes, "CDLL", lambda *_args, **_kw: LibProc()
    )

    assert pond_process_module._darwin_physical_footprint((101, 202)) is None


def test_darwin_helper_base_exception_still_kills_and_reaps_the_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SamplerAbort(BaseException):
        pass

    real_popen = subprocess.Popen
    started_processes: list[subprocess.Popen[bytes]] = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        started_processes.append(process)
        return process

    def abort_select(*_args, **_kwargs):
        raise SamplerAbort()

    monkeypatch.setattr(pond_process_module.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(pond_process_module, "_select_readable", abort_select)
    process: subprocess.Popen[bytes] | None = None
    try:
        with pytest.raises(SamplerAbort):
            pond_process_module._bounded_command_output(
                (
                    str(Path(sys.executable).resolve()),
                    "-c",
                    "import time; time.sleep(30)",
                )
            )
        process = started_processes[0]
        _wait_for_pid_exit(process.pid)
        assert process.returncode is not None
        assert not _pid_exists(process.pid)
    finally:
        if process is not None and _pid_exists(process.pid):
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def test_darwin_helper_success_with_descendant_still_cleans_the_whole_group(
    tmp_path: Path,
) -> None:
    descendant_path = tmp_path / "helper-descendant.pid"
    command = (
        str(Path(sys.executable).resolve()),
        "-c",
        textwrap.dedent(f"""
            import subprocess
            import sys
            from pathlib import Path

            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            Path({str(descendant_path)!r}).write_text(str(child.pid), encoding="utf-8")
            """),
    )
    descendant_pid: int | None = None
    try:
        assert pond_process_module._bounded_command_output(command) == b""

        descendant_pid = int(descendant_path.read_text(encoding="utf-8"))
        _wait_for_pid_exit(descendant_pid)
        assert not _pid_exists(descendant_pid)
    finally:
        if descendant_pid is not None and _pid_exists(descendant_pid):
            os.kill(descendant_pid, signal.SIGKILL)


def test_darwin_helper_timeout_and_output_cap_both_reap_the_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = subprocess.Popen
    started_processes: list[subprocess.Popen[bytes]] = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        started_processes.append(process)
        return process

    monkeypatch.setattr(pond_process_module.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(pond_process_module, "_SAMPLER_TIMEOUT_SECONDS", 0.05)

    commands = (
        (str(Path(sys.executable).resolve()), "-c", "import time; time.sleep(30)"),
        (
            str(Path(sys.executable).resolve()),
            "-c",
            "import sys; sys.stdout.buffer.write(b'x' * (1024 * 1024 + 1))",
        ),
    )
    for command in commands:
        with pytest.raises(PondProcessError, match=r"^resource$"):
            pond_process_module._bounded_command_output(command)

    for process in started_processes:
        _wait_for_pid_exit(process.pid)
        assert process.returncode is not None
        assert not _pid_exists(process.pid)


def test_darwin_helper_cleanup_reports_an_unreapable_tool_without_private_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnreapableTool:
        pid = 987_654
        returncode = None

        def kill(self) -> None:
            pass

        def wait(self, timeout: float) -> int:
            raise subprocess.TimeoutExpired("private-sampler-command", timeout)

    monkeypatch.setattr(pond_process_module.os, "killpg", lambda *_args: None)

    with pytest.raises(PondProcessError, match=r"^resource$") as raised:
        pond_process_module._stop_sampler_process(UnreapableTool())

    assert "private-sampler-command" not in str(raised.value)
    assert "987654" not in str(raised.value)


def test_darwin_helper_signal_fallback_does_not_poll_or_reap_the_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReservedTool:
        pid = 987_654
        returncode = None

        def kill(self) -> None:
            raise AssertionError("Popen.kill polls and may reap the leader")

        def wait(self, timeout: float) -> int:
            return 0

    direct_signals: list[tuple[int, int]] = []

    def missing_group(_process_group: int, _signal_number: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(pond_process_module.os, "killpg", missing_group)
    monkeypatch.setattr(
        pond_process_module.os,
        "kill",
        lambda pid, signal_number: direct_signals.append((pid, signal_number)),
    )
    monkeypatch.setattr(
        pond_process_module, "_process_group_exists", lambda _process_group: False
    )

    assert pond_process_module._stop_sampler_process(ReservedTool()) == 0
    assert direct_signals == [(987_654, signal.SIGKILL)]
