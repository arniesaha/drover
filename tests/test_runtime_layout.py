"""Versioned runtime directories, the current symlink, and rollback markers.

Nothing is ever upgraded in place: an update installs a whole new venv beside
the old one and moves a symlink. That is the entire reason rollback is cheap,
so these tests pin the properties that make it so.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from drover.server.runtime import RuntimeLayout, compare_versions


def _install(layout: RuntimeLayout, version: str, *, exit_code: int = 0) -> None:
    """Fake an installed runtime with every service-facing entry point."""
    bin_dir = layout.version_dir(version) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("drover", "drover-server", "drover-harnessd"):
        binary = bin_dir / name
        binary.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
        binary.chmod(0o755)


@pytest.mark.parametrize(
    "left,right,expected",
    [
        ("0.1.3", "0.1.4", -1),
        ("0.1.4", "0.1.3", 1),
        ("0.1.4", "0.1.4", 0),
        # Lexical comparison gets this wrong, and it is the exact mistake that
        # would strand a fleet on 0.9.x forever.
        ("0.2.0", "0.10.0", -1),
        ("1.0.0", "0.99.99", 1),
        ("v0.1.4", "0.1.4", 0),
        ("0.1", "0.1.0", 0),
    ],
)
def test_compare_versions(left, right, expected):
    assert compare_versions(left, right) == expected


def test_flip_points_current_at_a_version(tmp_path):
    layout = RuntimeLayout(tmp_path)
    _install(layout, "0.1.3")
    layout.flip("0.1.3")
    assert layout.active_version() == "0.1.3"
    assert layout.current.is_symlink()


def test_runtime_root_can_live_beside_the_in_place_venv(tmp_path):
    layout = RuntimeLayout(tmp_path / "external-config", root=tmp_path / "boot-runtime")
    _install(layout, "0.1.4")

    assert layout.root == tmp_path / "boot-runtime"
    assert layout.smoke_test("0.1.4") is True


def test_flip_replaces_an_existing_symlink(tmp_path):
    layout = RuntimeLayout(tmp_path)
    _install(layout, "0.1.3")
    _install(layout, "0.1.4")
    layout.flip("0.1.3")
    layout.flip("0.1.4")
    assert layout.active_version() == "0.1.4"
    assert os.readlink(layout.current) == "0.1.4", "relative, so the tree can move"


def test_active_version_is_none_before_any_flip(tmp_path):
    assert RuntimeLayout(tmp_path).active_version() is None


def test_installed_versions_are_sorted_newest_last(tmp_path):
    layout = RuntimeLayout(tmp_path)
    for version in ("0.1.10", "0.1.2", "0.1.9"):
        _install(layout, version)
    assert layout.installed_versions() == ["0.1.2", "0.1.9", "0.1.10"]


def test_installed_versions_ignores_the_current_symlink(tmp_path):
    layout = RuntimeLayout(tmp_path)
    _install(layout, "0.1.3")
    layout.flip("0.1.3")
    assert layout.installed_versions() == ["0.1.3"]


def test_smoke_test_passes_for_a_runnable_version(tmp_path):
    layout = RuntimeLayout(tmp_path)
    _install(layout, "0.1.4")
    assert layout.smoke_test("0.1.4") is True


def test_smoke_test_fails_for_a_broken_version(tmp_path):
    layout = RuntimeLayout(tmp_path)
    _install(layout, "0.1.4", exit_code=1)
    assert layout.smoke_test("0.1.4") is False


def test_smoke_test_logs_staged_interpreter_error(tmp_path, caplog):
    layout = RuntimeLayout(tmp_path)
    binary = layout.version_dir("0.1.4") / "bin" / "drover-server"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/bin/sh\necho 'PermissionError: pyvenv.cfg' >&2\nexit 1\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)

    with caplog.at_level("WARNING"):
        assert layout.smoke_test("0.1.4") is False

    assert "PermissionError: pyvenv.cfg" in caplog.text


def test_smoke_test_reports_inaccessible_staged_executable(
    tmp_path, monkeypatch, caplog
):
    layout = RuntimeLayout(tmp_path)
    binary = layout.executable("drover-server", "0.1.4")
    real_stat = type(binary).stat

    def inaccessible(path, *args, **kwargs):
        if path == binary:
            raise PermissionError(1, "Operation not permitted", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(type(binary), "stat", inaccessible)

    with caplog.at_level("WARNING"):
        assert layout.smoke_test("0.1.4") is False

    assert "Operation not permitted" in caplog.text


def test_smoke_test_logs_output_before_timeout(tmp_path, monkeypatch, caplog):
    layout = RuntimeLayout(tmp_path)
    _install(layout, "0.1.4")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 30, stderr=b"staged venv denied\n")

    monkeypatch.setattr(subprocess, "run", timeout)

    with caplog.at_level("WARNING"):
        assert layout.smoke_test("0.1.4") is False

    assert "staged venv denied" in caplog.text


def test_smoke_test_fails_for_a_missing_version(tmp_path):
    assert RuntimeLayout(tmp_path).smoke_test("9.9.9") is False


def test_runtime_verification_rejects_a_console_script_with_its_stage_path(tmp_path):
    layout = RuntimeLayout(tmp_path)
    _install(layout, "0.1.4")
    stage = layout.root / ".0.1.4-stage"
    (layout.version_dir("0.1.4") / "bin" / "drover-harnessd").write_text(
        f"#!{stage}/bin/python\n", encoding="utf-8"
    )

    assert layout.verify_runtime("0.1.4", former_path=stage) is False


def test_runtime_verification_exercises_every_service_entrypoint(tmp_path, monkeypatch):
    layout = RuntimeLayout(tmp_path)
    _install(layout, "0.1.4")
    calls = []

    def record(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", record)

    assert layout.verify_runtime("0.1.4") is True
    assert [Path(command[0]).name for command in calls] == [
        "drover",
        "drover-server",
        "drover-harnessd",
    ]


def test_prune_keeps_the_newest_and_never_the_active_one(tmp_path):
    layout = RuntimeLayout(tmp_path)
    for version in ("0.1.1", "0.1.2", "0.1.3", "0.1.4"):
        _install(layout, version)
    layout.flip("0.1.1")

    removed = layout.prune(keep=2)

    remaining = set(layout.installed_versions())
    assert "0.1.1" in remaining, "the active version is never pruned"
    assert "0.1.4" in remaining
    assert set(removed).isdisjoint({"0.1.1", "0.1.4"})
    for version in removed:
        assert not layout.version_dir(version).exists()


def test_prune_is_a_no_op_when_nothing_is_installed(tmp_path):
    assert RuntimeLayout(tmp_path).prune(keep=2) == []


def test_marker_round_trips(tmp_path):
    layout = RuntimeLayout(tmp_path)
    assert layout.read_marker() is None
    layout.write_marker("0.1.3", "0.1.4")
    assert layout.read_marker() == ("0.1.3", "0.1.4")
    layout.clear_marker()
    assert layout.read_marker() is None


def test_clearing_an_absent_marker_is_not_an_error(tmp_path):
    RuntimeLayout(tmp_path).clear_marker()


def test_corrupt_marker_reads_as_absent(tmp_path):
    """A half-written marker must not wedge a host into rolling back."""
    layout = RuntimeLayout(tmp_path)
    layout.root.mkdir(parents=True, exist_ok=True)
    (layout.root / "pending_verification.json").write_text("{oops", encoding="utf-8")
    assert layout.read_marker() is None


def test_executable_resolves_through_current_by_default(tmp_path):
    layout = RuntimeLayout(tmp_path)
    assert (
        layout.executable("drover-server")
        .as_posix()
        .endswith("runtime/current/bin/drover-server")
    )
    assert (
        layout.executable("drover-server", "0.1.4")
        .as_posix()
        .endswith("runtime/0.1.4/bin/drover-server")
    )
