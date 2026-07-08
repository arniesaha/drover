"""Tests for collect.shipper — rsync wrapper."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from drover.collect.shipper import ShipError, ShipResult, ship_staging


@dataclass
class FakeRunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def test_no_jsonl_files_returns_zero_without_calling_runner(tmp_path: Path) -> None:
    calls: list = []

    def runner(cmd, **kw):  # pragma: no cover — should not be called
        calls.append(cmd)
        return FakeRunResult(returncode=0)

    result = ship_staging(
        staging_dir=tmp_path,
        host="mac-mini.local",
        host_id="nas",
        _runner=runner,
    )
    assert result == ShipResult(files=0, returncode=0, command=None)
    assert calls == []


def test_constructed_command(tmp_path: Path) -> None:
    (tmp_path / "claude_code-r1.jsonl").write_text('{"x":1}\n')
    captured: dict = {}

    def runner(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return FakeRunResult(returncode=0)

    result = ship_staging(
        staging_dir=tmp_path,
        host="mac-mini.local",
        host_id="nas",
        remote_root="~/.nexus/incoming",
        rsync="/usr/bin/rsync",
        _runner=runner,
    )
    cmd = captured["cmd"]
    assert cmd[0] == "/usr/bin/rsync"
    assert "-a" in cmd
    assert "--remove-source-files" in cmd
    # Expect the staging glob (or the dir-with-trailing-slash) and the remote dest
    assert any("mac-mini.local:~/.nexus/incoming/nas/" in part for part in cmd)
    assert result.files == 1
    assert result.returncode == 0


def test_nonzero_return_code_raises_ship_error(tmp_path: Path) -> None:
    (tmp_path / "x.jsonl").write_text("{}\n")

    def runner(cmd, **kw):
        return FakeRunResult(returncode=23, stderr="rsync: connection refused")

    with pytest.raises(ShipError) as exc:
        ship_staging(
            staging_dir=tmp_path,
            host="mac-mini.local",
            host_id="nas",
            _runner=runner,
        )
    assert "23" in str(exc.value)
    assert "connection refused" in str(exc.value)


def test_extra_args_appended(tmp_path: Path) -> None:
    (tmp_path / "x.jsonl").write_text("{}\n")
    captured: dict = {}

    def runner(cmd, **kw):
        captured["cmd"] = cmd
        return FakeRunResult(returncode=0)

    ship_staging(
        staging_dir=tmp_path,
        host="mac-mini.local",
        host_id="nas",
        extra_args=["-e", "ssh -p 2222"],
        _runner=runner,
    )
    cmd = captured["cmd"]
    assert "-e" in cmd
    assert "ssh -p 2222" in cmd
