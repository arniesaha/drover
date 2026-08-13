"""Decide what version the fleet should be on.

Kept separate from the thread that acts on the decision, so the policy is
testable without sleeping and so a pin takes effect on the next heartbeat
rather than the next poll.

One rule shapes most of this: a failed poll is not a retraction. Hosts may
already be downloading a target when the feed blips, and withdrawing it would
leave them installing a version the hub no longer admits to wanting.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from drover.config import DroverConfig
from drover.server.runtime import RuntimeLayout, compare_versions
from drover.server.updates import ReleaseArtifact, fetch_latest_release

log = logging.getLogger("drover.update")


@dataclass(frozen=True)
class TargetVersion:
    version: str
    artifact: ReleaseArtifact | None


class UpdatePlanner:
    def __init__(
        self,
        cfg: DroverConfig,
        layout: RuntimeLayout,
        *,
        fetcher=fetch_latest_release,
    ) -> None:
        self._cfg = cfg
        self._layout = layout
        self._fetcher = fetcher
        self._lock = threading.Lock()
        self._target: TargetVersion | None = None

    def refresh(self) -> None:
        # Turning updates off is a deliberate stop, so unlike a failed poll it
        # does retract whatever target was published.
        if not self._cfg.update_enabled:
            with self._lock:
                self._target = None
            return

        pinned = self._cfg.update_pinned_version.strip().lstrip("v")
        if pinned:
            # A pinned fleet never needs the network. The pin may also predate
            # the feed's latest, so there is no artifact to hand out; a host
            # converges to it only if it already has that version installed.
            with self._lock:
                self._target = TargetVersion(version=pinned, artifact=None)
            return

        try:
            artifact = self._fetcher(self._cfg.update_repo)
        except Exception:  # noqa: BLE001 - a timer must not die on a bad feed
            log.debug("release feed lookup failed", exc_info=True)
            artifact = None
        if artifact is None:
            # Leave any existing target in place: hosts may already be
            # installing it, and a blip is not a decision.
            return

        active = self._layout.active_version()
        if active and compare_versions(artifact.version, active) <= 0:
            # Downgrade is never automatic, and the current version is not an
            # update. Either way there is nothing for hosts to converge on.
            with self._lock:
                self._target = None
            return
        with self._lock:
            self._target = TargetVersion(version=artifact.version, artifact=artifact)

    def target(self) -> TargetVersion | None:
        with self._lock:
            return self._target

    def as_heartbeat_payload(self) -> dict:
        """Merged into the response every harnessd already polls for."""
        target = self.target()
        if target is None:
            return {}
        payload: dict = {"target_version": target.version}
        if target.artifact is not None:
            payload["artifact"] = {
                "url": target.artifact.wheel_url,
                "sha256": target.artifact.wheel_sha256,
                "lock_url": target.artifact.lock_url,
                "lock_sha256": target.artifact.lock_sha256,
            }
        return payload
