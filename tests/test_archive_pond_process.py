"""Shared, bounded Pond subprocess supervision contract."""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import stat
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
elif mode == "overflow_stdout":
    sys.stdout.buffer.write(b"x" * (32 * 1024 * 1024 + 1))
    sys.stdout.buffer.flush()
    time.sleep(30)
elif mode == "overflow_artifact":
    artifact = Path(os.environ["FAKE_ARTIFACT"])
    artifact.write_bytes(b"x" * (32 * 1024 * 1024 + 1))
    artifact.chmod(0o600)
    time.sleep(30)
elif mode in {"orphan", "stubborn_orphan"}:
    child_code = r'''import os
from pathlib import Path
import signal
import time

pid_path = Path(os.environ["FAKE_DESCENDANT_PID"])
marker_path = Path(os.environ["FAKE_DESCENDANT_MARKER"])
stubborn = os.environ.get("FAKE_DESCENDANT_STUBBORN") == "1"

def terminate(_signum, _frame):
    marker_path.write_text("term", encoding="utf-8")
    if not stubborn:
        raise SystemExit(0)

signal.signal(signal.SIGTERM, terminate)
pid_path.write_text(str(os.getpid()), encoding="utf-8")
time.sleep(30)
'''
    subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=sys.stdout,
        stderr=sys.stderr,
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


def test_runner_uses_the_canonical_binary_directly_in_a_new_process_group(
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
    assert calls == [
        {
            "argv": [str(binary.resolve()), "inspect", _PRIVATE_REMOTE],
            "secret": _PRIVATE_ENVIRONMENT,
        }
    ]
    assert observation["pid"] == observation["pgid"] == observation["sid"]
    assert not (tmp_path / "no-shell-injection").exists()
    assert stat.S_IMODE(run_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.stdout_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.stderr_path.stat().st_mode) == 0o600


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
                ),
            )
        descendant_pid = int(pid_path.read_text(encoding="utf-8"))
        _wait_for_pid_exit(descendant_pid)
        assert marker_path.read_text(encoding="utf-8") == "term"
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
