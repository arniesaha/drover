"""Rollback is the manual escape hatch; it must be boring and obvious.

The watchdog handles a version that cannot come up at all. This is for the
other case: it starts, registers, and is still wrong.
"""

from __future__ import annotations

from click.testing import CliRunner

from drover.server.__main__ import main
from drover.server.runtime import RuntimeLayout


def _installed(layout, version):
    binary = layout.version_dir(version) / "bin" / "drover-server"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)


def test_rollback_flips_to_the_previous_version(tmp_path, monkeypatch):
    layout = RuntimeLayout(tmp_path / ".drover")
    for version in ("0.1.3", "0.1.4"):
        _installed(layout, version)
    layout.flip("0.1.4")
    monkeypatch.setenv("HOME", str(tmp_path))

    result = CliRunner().invoke(main, ["rollback"])
    assert result.exit_code == 0, result.output
    assert layout.active_version() == "0.1.3"
    assert "0.1.3" in result.output


def test_rollback_to_an_explicit_version(tmp_path, monkeypatch):
    layout = RuntimeLayout(tmp_path / ".drover")
    for version in ("0.1.1", "0.1.3", "0.1.4"):
        _installed(layout, version)
    layout.flip("0.1.4")
    monkeypatch.setenv("HOME", str(tmp_path))

    result = CliRunner().invoke(main, ["rollback", "--to", "0.1.1"])
    assert result.exit_code == 0, result.output
    assert layout.active_version() == "0.1.1"


def test_rollback_refuses_an_uninstalled_version(tmp_path, monkeypatch):
    layout = RuntimeLayout(tmp_path / ".drover")
    _installed(layout, "0.1.4")
    layout.flip("0.1.4")
    monkeypatch.setenv("HOME", str(tmp_path))

    result = CliRunner().invoke(main, ["rollback", "--to", "9.9.9"])
    assert result.exit_code != 0
    assert layout.active_version() == "0.1.4", "must not strand the host"
    assert "9.9.9" in result.output


def test_rollback_with_nothing_to_roll_back_to_says_so(tmp_path, monkeypatch):
    layout = RuntimeLayout(tmp_path / ".drover")
    _installed(layout, "0.1.4")
    layout.flip("0.1.4")
    monkeypatch.setenv("HOME", str(tmp_path))

    result = CliRunner().invoke(main, ["rollback"])
    assert result.exit_code != 0
    assert "no earlier version" in result.output.lower()
    assert layout.active_version() == "0.1.4"


def test_rollback_clears_a_pending_marker(tmp_path, monkeypatch):
    """Otherwise the next start would see a marker for a flip we just undid."""
    layout = RuntimeLayout(tmp_path / ".drover")
    for version in ("0.1.3", "0.1.4"):
        _installed(layout, version)
    layout.flip("0.1.4")
    layout.write_marker("0.1.3", "0.1.4")
    monkeypatch.setenv("HOME", str(tmp_path))

    CliRunner().invoke(main, ["rollback"])
    assert layout.read_marker() is None


def test_update_check_reports_state_without_changing_anything(tmp_path, monkeypatch):
    layout = RuntimeLayout(tmp_path / ".drover")
    for version in ("0.1.3", "0.1.4"):
        _installed(layout, version)
    layout.flip("0.1.4")
    monkeypatch.setenv("HOME", str(tmp_path))

    import drover.server.__main__ as server_main

    monkeypatch.setattr(
        server_main.UpdatePlanner, "refresh", lambda self: None, raising=True
    )

    result = CliRunner().invoke(main, ["update", "--check"])
    assert result.exit_code == 0, result.output
    assert "0.1.4" in result.output
    assert "0.1.3" in result.output
    assert layout.active_version() == "0.1.4", "--check must not change anything"


def test_update_check_shows_a_pin(tmp_path, monkeypatch):
    layout = RuntimeLayout(tmp_path / ".drover")
    _installed(layout, "0.1.4")
    layout.flip("0.1.4")
    monkeypatch.setenv("HOME", str(tmp_path))

    config = tmp_path / "config.toml"
    config.write_text('[update]\npinned_version = "0.1.3"\n', encoding="utf-8")

    import drover.server.__main__ as server_main

    monkeypatch.setattr(
        server_main.UpdatePlanner, "refresh", lambda self: None, raising=True
    )

    result = CliRunner().invoke(main, ["--config", str(config), "update", "--check"])
    assert result.exit_code == 0, result.output
    assert "pinned" in result.output.lower()
    assert "0.1.3" in result.output
