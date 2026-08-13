"""The heartbeat carries the target version back to each host.

The hub-to-host control channel already exists: `_post_central_json` parses
the registration response and applies `content_consent` from it. This adds
`target_version` to the same body rather than inventing a second channel, so
the only real change is that the parsed body is returned instead of discarded.
"""

from __future__ import annotations

import dataclasses
import io
import json

from drover.config import default_config
from drover.server.harness import daemon as daemon_module
from drover.server.runtime import RuntimeLayout
from drover.server.update_planner import UpdatePlanner
from drover.server.updates import ReleaseArtifact


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _State:
    """Only the attributes the registration path reads."""

    def __init__(self, central_url="http://127.0.0.1:7080"):
        self.central_url = central_url
        self.host_id = "build-mac"
        self.display_name = "Build Mac"
        self.kind = "macos"
        self.local_url = None
        self.tailscale_url = None
        self.host_token = "secret"
        self.relay = False
        self.content_consent = None

    def capabilities(self):
        return {}


def _urlopen_returning(payload: dict):
    def open_url(request, timeout=0):
        return _Response(json.dumps(payload).encode())

    return open_url


def test_post_central_json_returns_the_parsed_body(monkeypatch):
    """Previously this returned bool and dropped the body on the floor."""
    monkeypatch.setattr(
        daemon_module, "urlopen", _urlopen_returning({"target_version": "0.1.4"})
    )
    body = daemon_module._post_central_json(
        _State(), "/harness/hosts", {"host_id": "h"}
    )
    assert body == {"target_version": "0.1.4"}


def test_post_central_json_returns_none_without_a_central_url():
    assert daemon_module._post_central_json(_State(central_url=None), "/x", {}) is None


def test_post_central_json_returns_none_on_transport_failure(monkeypatch):
    def boom(request, timeout=0):
        raise OSError("no route")

    monkeypatch.setattr(daemon_module, "urlopen", boom)
    assert daemon_module._post_central_json(_State(), "/x", {}) is None


def test_an_empty_body_is_a_dict_not_a_failure(monkeypatch):
    """A hub with no target to publish answers {}. That is success, and a
    caller must not read it as an unreachable hub."""
    monkeypatch.setattr(daemon_module, "urlopen", _urlopen_returning({}))
    assert daemon_module._post_central_json(_State(), "/x", {}) == {}


def test_registration_payload_reports_the_running_version(monkeypatch):
    captured = {}

    def capture(state, path, payload):
        captured.update(payload)
        return {}

    monkeypatch.setattr(daemon_module, "_post_central_json", capture)
    daemon_module.register_daemon_host_remote(_State())
    assert "agent_version" in captured
    assert captured["agent_version"], "the hub cannot detect skew without this"


def test_registration_returns_the_body_for_the_heartbeat_loop(monkeypatch):
    monkeypatch.setattr(
        daemon_module,
        "_post_central_json",
        lambda state, path, payload: {"target_version": "0.1.4"},
    )
    assert daemon_module.register_daemon_host_remote(_State()) == {
        "target_version": "0.1.4"
    }


def test_registration_without_a_central_url_is_none():
    assert daemon_module.register_daemon_host_remote(_State(central_url=None)) is None


# --- hub side -----------------------------------------------------------------


def _artifact(version="0.1.4"):
    return ReleaseArtifact(
        version=version,
        wheel_url=f"https://x.test/drover-{version}-py3-none-any.whl",
        wheel_sha256="aa" * 32,
        lock_url="https://x.test/requirements.lock.txt",
        lock_sha256="bb" * 32,
    )


def test_planner_payload_merges_into_a_registration_response(tmp_path):
    """What the hub adds to the body harnessd already receives."""
    planner = UpdatePlanner(
        default_config(), RuntimeLayout(tmp_path), fetcher=lambda repo: _artifact()
    )
    planner.refresh()

    body = {"host": {"host_id": "build-mac"}, "content_consent": {"enabled": False}}
    body.update(planner.as_heartbeat_payload())

    assert body["target_version"] == "0.1.4"
    assert body["host"]["host_id"] == "build-mac", "existing keys survive"
    assert body["content_consent"] == {"enabled": False}


def test_no_target_leaves_the_registration_response_untouched(tmp_path):
    cfg = dataclasses.replace(default_config(), update_enabled=False)
    planner = UpdatePlanner(
        cfg, RuntimeLayout(tmp_path), fetcher=lambda repo: _artifact()
    )
    planner.refresh()

    body = {"host": {"host_id": "build-mac"}}
    body.update(planner.as_heartbeat_payload())
    assert body == {"host": {"host_id": "build-mac"}}
