"""The heartbeat loop must actually hand the hub's answer to the updater.

PR #132 built both ends of fleet auto-update and connected neither: the hub
published a target on the registration response, `HostUpdater` knew what to do
with one, and the heartbeat loop dropped the body on the floor. Every
convergence test passed the whole time, because they drove `HostUpdater`
directly and nothing asserted that the daemon ever calls it.

These tests are about the wire, not the logic on either end of it.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from drover.config import default_config
from drover.server.harness import daemon as daemon_module
from drover.server.harness.updater import HostUpdater
from drover.server.runtime import RuntimeLayout


class _Updater:
    """Records what the heartbeat path does to it."""

    def __init__(self):
        self.observed: list[dict] = []
        self.activations = 0

    def observe(self, body):
        self.observed.append(body)

    def maybe_activate(self):
        self.activations += 1
        return False


def _state(updater=None):
    return SimpleNamespace(
        central_url="http://127.0.0.1:7080",
        updater=updater,
        registered_at_least_once=False,
    )


def test_the_heartbeat_hands_the_hub_body_to_the_updater(monkeypatch):
    """The bug: this body was discarded, so no host ever saw a target."""
    beat = {"target_version": "0.1.4"}
    monkeypatch.setattr(
        daemon_module, "register_daemon_host_remote", lambda state: beat
    )
    updater = _Updater()

    daemon_module._heartbeat_once(_state(updater))

    assert updater.observed == [beat]
    assert updater.activations == 1


def test_a_successful_beat_records_that_we_reached_the_hub(monkeypatch):
    """The rollback watchdog has no other way to know the flip worked."""
    monkeypatch.setattr(daemon_module, "register_daemon_host_remote", lambda state: {})
    state = _state()

    daemon_module._heartbeat_once(state)

    assert state.registered_at_least_once is True


def test_an_unreachable_hub_leaves_the_registration_flag_alone(monkeypatch):
    """An empty body means success; only None means we never got through."""
    monkeypatch.setattr(
        daemon_module, "register_daemon_host_remote", lambda state: None
    )
    updater = _Updater()
    state = _state(updater)

    daemon_module._heartbeat_once(state)

    assert state.registered_at_least_once is False
    assert updater.observed == []


def test_the_daemon_state_carries_an_updater_slot():
    """Set after construction, not passed in: HostUpdater needs the state."""
    fields = {f.name for f in dataclasses.fields(daemon_module.HarnessDaemonState)}
    assert {"updater", "registered_at_least_once"} <= fields


def _run_harnessd_capturing_state(monkeypatch, tmp_path, cfg):
    """Drive run_harnessd far enough to see what it wired, then bail out."""
    captured = {}

    class _State:
        api_token = ""
        host_token = None
        updater = None
        registered_at_least_once = False
        pty = SimpleNamespace(close_all=lambda: None)
        auth = SimpleNamespace(close_all=lambda: None)

    class _Server:
        def serve_forever(self):
            raise RuntimeError("stop")

        def server_close(self):
            pass

    def capture_heartbeat(state):
        captured["state"] = state
        return None

    monkeypatch.setattr(daemon_module, "HarnessDaemonState", lambda **kwargs: _State())
    monkeypatch.setattr(daemon_module, "resolve_daemon_token", lambda token: "token")
    monkeypatch.setattr(daemon_module, "wire_event_pusher", lambda state: None)
    monkeypatch.setattr(daemon_module, "register_daemon_host", lambda state: None)
    monkeypatch.setattr(
        daemon_module, "register_daemon_host_remote", lambda state: None
    )
    monkeypatch.setattr(daemon_module, "start_remote_heartbeat", capture_heartbeat)
    monkeypatch.setattr(
        daemon_module, "create_harness_server", lambda **kwargs: _Server()
    )

    with pytest.raises(RuntimeError, match="stop"):
        daemon_module.run_harnessd(
            host_id="test-host",
            display_name="Test Host",
            kind="mac",
            duckdb_path=tmp_path / "drover.duckdb",
            listen_host="127.0.0.1",
            listen_port=0,
            cfg=cfg,
        )
    return captured["state"]


def test_run_harnessd_wires_an_updater_when_updates_are_enabled(monkeypatch, tmp_path):
    cfg = dataclasses.replace(default_config(), update_enabled=True)

    state = _run_harnessd_capturing_state(monkeypatch, tmp_path, cfg)

    assert isinstance(state.updater, HostUpdater)


def test_run_harnessd_wires_nothing_when_updates_are_disabled(monkeypatch, tmp_path):
    """The config kill switch has to reach the host, not just the hub."""
    cfg = dataclasses.replace(default_config(), update_enabled=False)

    state = _run_harnessd_capturing_state(monkeypatch, tmp_path, cfg)

    assert state.updater is None


def test_the_harnessd_cli_passes_the_config_through(monkeypatch, tmp_path):
    """Without this the daemon gets cfg=None and updates are off everywhere."""
    from drover.server.harness import cli as cli_module

    captured = {}
    cfg = dataclasses.replace(default_config(), update_enabled=True)
    monkeypatch.setattr(cli_module, "resolve_config", lambda path: cfg)
    monkeypatch.setattr(cli_module, "bootstrap_harnessd_schema", lambda c: True)
    monkeypatch.setattr(
        cli_module, "run_harnessd", lambda **kwargs: captured.update(kwargs)
    )

    cli_module.run_harnessd_from_options(
        config_path=str(tmp_path / "config.toml"),
        host_id="test-host",
        display_name=None,
        kind="mac",
        listen="127.0.0.1:0",
        local_url=None,
        tailscale_url=None,
        central_url=None,
        host_token=None,
    )

    assert captured["cfg"] is cfg


def _flipped_layout(tmp_path):
    """A host mid-update: 0.1.4 active, 0.1.3 recorded as the way back."""
    layout = RuntimeLayout(tmp_path)
    for version in ("0.1.3", "0.1.4"):
        binary = layout.version_dir(version) / "bin" / "drover-server"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
    layout.flip("0.1.4")
    layout.write_marker("0.1.3", "0.1.4")
    return layout


def test_the_watchdog_accepts_a_version_that_reaches_the_hub(tmp_path):
    layout = _flipped_layout(tmp_path)
    state = _state()
    state.registered_at_least_once = True
    restarts = []

    daemon_module._rollback_watchdog(
        state,
        layout,
        deadline_seconds=0.05,
        sleep=lambda s: None,
        restarter=restarts.append,
    )

    assert layout.active_version() == "0.1.4"
    assert layout.read_marker() is None
    assert restarts == []


def test_the_watchdog_rolls_back_a_version_that_never_registers(tmp_path):
    """The whole reason this is safe to run on an awkward-to-reach machine."""
    layout = _flipped_layout(tmp_path)
    state = _state()
    restarts = []

    daemon_module._rollback_watchdog(
        state,
        layout,
        deadline_seconds=0.05,
        sleep=lambda s: None,
        restarter=lambda: restarts.append("restart"),
    )

    assert layout.active_version() == "0.1.3"
    assert layout.read_marker() is None
    assert restarts == ["restart"]


def test_the_watchdog_stops_waiting_as_soon_as_registration_lands(tmp_path):
    """It must not burn the full ninety seconds on a host that is fine."""
    layout = _flipped_layout(tmp_path)
    state = _state()
    naps = []

    def sleep(seconds):
        naps.append(seconds)
        state.registered_at_least_once = True

    daemon_module._rollback_watchdog(
        state, layout, deadline_seconds=90.0, sleep=sleep, restarter=lambda: None
    )

    assert len(naps) == 1
    assert layout.active_version() == "0.1.4"
