"""Decide when a host may activate a newly installed version.

The whole update path hangs off one question: is this machine doing work? If
the answer is unavailable for any reason, the answer is treated as yes. An
update deferred costs a few hours; an update that kills a running agent
mid-turn costs someone's work, and they will not get it back.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from drover.config import DroverConfig
from drover.server.runtime import RuntimeLayout, compare_versions
from drover.server.updates import ReleaseArtifact, install_version

log = logging.getLogger("drover.harnessd.update")

# How long a freshly activated version has to reach the hub before the
# watchdog decides it cannot, and undoes the flip.
REGISTRATION_DEADLINE_SECONDS = 90.0


@dataclass(frozen=True)
class QuiesceReport:
    structured_alive: int
    terminals: int

    @property
    def is_idle(self) -> bool:
        return self.structured_alive == 0 and self.terminals == 0


def quiesce_report(state) -> QuiesceReport:
    """Count live work. Raises if either manager cannot answer."""
    alive = 0
    for session_id in state.structured.session_ids():
        if state.structured.is_alive(session_id):
            alive += 1
    terminals = len(state.pty.list_sessions())
    return QuiesceReport(structured_alive=alive, terminals=terminals)


def is_quiescent(state) -> bool:
    """True only when this host is provably doing nothing.

    Any failure to determine that is reported as busy rather than idle. A
    wedged session manager is exactly the situation where guessing idle would
    flip the symlink out from under a running agent.
    """
    try:
        return quiesce_report(state).is_idle
    except Exception:  # noqa: BLE001 - unknown means busy, never means idle
        log.warning("could not determine session state; treating host as busy")
        return False


# Used only when this process cannot tell what started it. Hosts installed by
# the installer get these names; hosts set up by hand may not, which is the
# whole reason the names are discovered first.
_FALLBACK_SYSTEMD_UNIT = "drover-harnessd.service"
_FALLBACK_LAUNCHD_LABEL = "com.drover.harnessd"
_SELF_CGROUP = Path("/proc/self/cgroup")


def _systemd_unit(cgroup: Path | None = None) -> str:
    """The systemd unit this process is running under.

    systemd does not hand a service its own unit name, but it does put the
    process in a cgroup named after it, so the answer is on disk. Read rather
    than assumed because the storage host's unit is `drover-nas-harnessd`, and
    restarting a unit that does not exist fails silently.
    """
    try:
        text = (cgroup or _SELF_CGROUP).read_text(encoding="utf-8")
    except OSError:
        return _FALLBACK_SYSTEMD_UNIT
    for segment in reversed(text.strip().split("/")):
        name = segment.strip()
        if name.endswith(".service"):
            return name
    return _FALLBACK_SYSTEMD_UNIT


def _launchd_label() -> str:
    """The launchd job label, from the environment launchd gives its jobs.

    launchd sets this to "0" for anything it did not start as a job, which is
    every developer shell, so that case falls back rather than trying to
    kickstart a label that is not one.
    """
    label = os.environ.get("XPC_SERVICE_NAME", "").strip()
    if not label or label == "0":
        return _FALLBACK_LAUNCHD_LABEL
    return label


def default_restarter() -> None:
    """Ask the service manager to restart us; it owns the process, not we."""
    if sys.platform == "darwin":
        target = f"gui/{os.getuid()}/{_launchd_label()}"
        log.info("asking launchd to restart %s", target)
        subprocess.run(["launchctl", "kickstart", "-k", target], check=False)
    else:
        unit = _systemd_unit()
        log.info("asking systemd to restart %s", unit)
        subprocess.run(["systemctl", "--user", "restart", unit], check=False)


class HostUpdater:
    """Install as soon as we hear about a version; activate only when idle.

    Those are deliberately separate. Downloading is cheap and can happen while
    an agent is mid-turn; flipping the symlink and restarting is not, and has
    to wait for the host to be doing nothing, which can be hours later.
    """

    def __init__(
        self,
        state,
        layout: RuntimeLayout,
        cfg: DroverConfig,
        *,
        installer=install_version,
        restarter=default_restarter,
    ) -> None:
        self._state = state
        self._layout = layout
        self._cfg = cfg
        self._installer = installer
        self._restarter = restarter
        self._lock = threading.Lock()
        self._pending: str | None = None
        self._blocked = False

    def observe(self, heartbeat_body: dict) -> None:
        """React to what the hub said, downloading if we are behind."""
        target = str((heartbeat_body or {}).get("target_version") or "")
        if not target:
            return
        active = self._layout.active_version()
        if active and compare_versions(target, active) <= 0:
            # Already there, or the hub is asking for something older, which
            # is never automatic.
            with self._lock:
                self._pending = None
                self._blocked = False
            return
        with self._lock:
            if self._pending == target:
                # Already installed and waiting for quiesce. A host can wait
                # hours; re-downloading on every 15s heartbeat would be absurd.
                return
            self._pending = target

        raw = (heartbeat_body or {}).get("artifact") or {}
        artifact = ReleaseArtifact(
            version=target,
            wheel_url=str(raw.get("url") or ""),
            wheel_sha256=str(raw.get("sha256") or ""),
            lock_url=str(raw.get("lock_url") or ""),
            lock_sha256=str(raw.get("lock_sha256") or ""),
        )
        if not self._installer(self._layout, artifact):
            log.warning("could not install %s; will retry on a later heartbeat", target)
            with self._lock:
                self._pending = None

    def maybe_activate(self) -> bool:
        """Flip and restart, but only if this host is provably idle."""
        with self._lock:
            target = self._pending
        if target is None:
            return False
        if not self._layout.smoke_test(target):
            # The installer may have reported success; this is the last gate
            # before the symlink moves.
            log.warning("%s failed its smoke test; refusing to activate", target)
            return False
        if not is_quiescent(self._state):
            with self._lock:
                self._blocked = True
            return False

        previous = self._layout.active_version() or ""
        # Marker first: if the flip or the restart goes wrong, the next start
        # needs to know what to fall back to.
        self._layout.write_marker(previous, target)
        self._layout.flip(target)
        with self._lock:
            self._blocked = False
        log.info("activated %s (was %s); restarting", target, previous or "unknown")
        self._restarter()
        return True

    def status(self) -> dict:
        """Surfaced on the heartbeat so the app can show a waiting host."""
        with self._lock:
            return {"pending_version": self._pending, "update_blocked": self._blocked}


def verify_after_restart(layout: RuntimeLayout, *, registered: bool) -> bool:
    """Undo a flip whose new version could not reach the hub.

    Without this, a bad release on a machine reachable only through an
    awkward SSH path is a physical trip. With it, the machine repairs itself
    in ninety seconds.
    """
    marker = layout.read_marker()
    if marker is None:
        return True
    previous, target = marker
    if registered:
        layout.clear_marker()
        layout.prune(keep=2)
        return True

    log.error(
        "%s did not register within %.0fs; rolling back to %s",
        target,
        REGISTRATION_DEADLINE_SECONDS,
        previous or "unknown",
    )
    if previous:
        layout.flip(previous)
    else:
        # Flipping to "" would point current at nothing and leave the host
        # with no runtime at all, which is worse than staying on a version
        # that at least starts.
        log.error("no previous version recorded; leaving the symlink alone")
    layout.clear_marker()
    return False
