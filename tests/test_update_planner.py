"""What version should the fleet be on? Policy only: no threads, no sleeping.

Kept separate from the thread that acts on the decision so the policy is
testable directly, and so a pin takes effect on the next heartbeat rather than
the next poll.
"""

from __future__ import annotations

import dataclasses

from drover.config import default_config
from drover.server.runtime import RuntimeLayout
from drover.server.update_planner import UpdatePlanner
from drover.server.updates import ReleaseArtifact


def _artifact(version="0.1.4"):
    return ReleaseArtifact(
        version=version,
        wheel_url=f"https://x.test/drover-{version}-py3-none-any.whl",
        wheel_sha256="aa" * 32,
        lock_url="https://x.test/requirements.lock.txt",
        lock_sha256="bb" * 32,
    )


def _installed(layout, version):
    binary = layout.version_dir(version) / "bin" / "drover-server"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)


def _planner(tmp_path, cfg=None, artifact=None):
    return UpdatePlanner(
        cfg or default_config(),
        RuntimeLayout(tmp_path),
        fetcher=lambda repo: artifact,
    )


def test_no_release_means_no_target(tmp_path):
    planner = _planner(tmp_path, artifact=None)
    planner.refresh()
    assert planner.target() is None
    assert planner.as_heartbeat_payload() == {}


def test_a_newer_release_becomes_the_target(tmp_path):
    planner = _planner(tmp_path, artifact=_artifact("0.1.4"))
    planner.refresh()
    assert planner.target().version == "0.1.4"


def test_heartbeat_payload_carries_the_artifact(tmp_path):
    planner = _planner(tmp_path, artifact=_artifact("0.1.4"))
    planner.refresh()
    payload = planner.as_heartbeat_payload()
    assert payload["target_version"] == "0.1.4"
    assert payload["artifact"]["sha256"] == "aa" * 32
    assert payload["artifact"]["lock_sha256"] == "bb" * 32
    assert payload["artifact"]["url"].endswith(".whl")


def test_a_pin_overrides_the_feed(tmp_path):
    cfg = dataclasses.replace(default_config(), update_pinned_version="0.1.2")
    planner = _planner(tmp_path, cfg=cfg, artifact=_artifact("0.1.4"))
    planner.refresh()
    assert planner.target().version == "0.1.2"


def test_a_pin_publishes_no_artifact(tmp_path):
    """A pinned version may predate the feed's latest, so there is no
    artifact to hand out. The host installs it only if it already has it."""
    cfg = dataclasses.replace(default_config(), update_pinned_version="0.1.2")
    planner = _planner(tmp_path, cfg=cfg, artifact=_artifact("0.1.4"))
    planner.refresh()
    assert planner.as_heartbeat_payload() == {"target_version": "0.1.2"}


def test_a_pin_works_without_ever_reaching_the_feed(tmp_path):
    def boom(repo):
        raise AssertionError("a pinned fleet must not need the network")

    cfg = dataclasses.replace(default_config(), update_pinned_version="0.1.2")
    planner = UpdatePlanner(cfg, RuntimeLayout(tmp_path), fetcher=boom)
    planner.refresh()
    assert planner.target().version == "0.1.2"


def test_a_leading_v_on_a_pin_is_tolerated(tmp_path):
    cfg = dataclasses.replace(default_config(), update_pinned_version="v0.1.2")
    planner = _planner(tmp_path, cfg=cfg, artifact=None)
    planner.refresh()
    assert planner.target().version == "0.1.2"


def test_disabled_updates_publish_no_target(tmp_path):
    cfg = dataclasses.replace(default_config(), update_enabled=False)
    planner = _planner(tmp_path, cfg=cfg, artifact=_artifact("0.1.4"))
    planner.refresh()
    assert planner.target() is None
    assert planner.as_heartbeat_payload() == {}


def test_an_older_release_is_never_a_target(tmp_path):
    layout = RuntimeLayout(tmp_path)
    _installed(layout, "0.1.9")
    layout.flip("0.1.9")
    planner = UpdatePlanner(
        default_config(), layout, fetcher=lambda repo: _artifact("0.1.4")
    )
    planner.refresh()
    assert planner.target() is None, "downgrade is never automatic"


def test_the_same_version_is_not_a_target(tmp_path):
    layout = RuntimeLayout(tmp_path)
    _installed(layout, "0.1.4")
    layout.flip("0.1.4")
    planner = UpdatePlanner(
        default_config(), layout, fetcher=lambda repo: _artifact("0.1.4")
    )
    planner.refresh()
    assert planner.target() is None


def test_refresh_survives_a_fetcher_that_raises(tmp_path):
    def boom(repo):
        raise OSError("no network")

    planner = UpdatePlanner(default_config(), RuntimeLayout(tmp_path), fetcher=boom)
    planner.refresh()
    assert planner.target() is None


def test_an_unreachable_feed_keeps_the_previous_target(tmp_path):
    """A blip must not retract a target hosts are already converging on."""
    calls = {"n": 0}

    def flaky(repo):
        calls["n"] += 1
        return _artifact("0.1.4") if calls["n"] == 1 else None

    planner = UpdatePlanner(default_config(), RuntimeLayout(tmp_path), fetcher=flaky)
    planner.refresh()
    assert planner.target().version == "0.1.4"
    planner.refresh()
    assert planner.target().version == "0.1.4", "a failed poll is not a retraction"


def test_disabling_updates_does_retract_a_target(tmp_path):
    """Unlike a blip, turning updates off is a deliberate stop."""
    planner = _planner(tmp_path, artifact=_artifact("0.1.4"))
    planner.refresh()
    assert planner.target() is not None

    planner._cfg = dataclasses.replace(default_config(), update_enabled=False)
    planner.refresh()
    assert planner.target() is None
