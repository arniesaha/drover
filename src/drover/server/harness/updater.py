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
from datetime import datetime, timezone
from pathlib import Path

from drover.config import ACTIVATION_IN_PLACE, ACTIVATION_SYMLINK, DroverConfig
from drover.server.runtime import RuntimeLayout, compare_versions
from drover.server.updates import (
    ReleaseArtifact,
    install_cached_into_venv,
    install_version,
)

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


def resolve_activation(cfg) -> tuple[str, str]:
    """The activation mode this host will actually use, and its venv.

    In-place activation with no venv configured is a misconfiguration, not an
    instruction to go looking for one: the whole point of naming it explicitly
    is that the wrong guess overwrites an environment nobody asked us to
    touch. Fall back to the symlink every other host uses and say so.

    The venv is expanded here, once, so everything downstream sees the same
    absolute path. A daemon started by launchd or systemd does not run a
    shell, and an unexpanded `~` would become a directory of that name.
    """
    mode = str(getattr(cfg, "update_activation", "") or ACTIVATION_SYMLINK).strip()
    venv = str(getattr(cfg, "update_in_place_venv", "") or "").strip()
    if venv:
        venv = os.path.expanduser(venv)
    if mode == ACTIVATION_IN_PLACE and not venv:
        log.warning(
            "update.activation is in_place but update.in_place_venv is empty; "
            "activating by symlink instead"
        )
        return ACTIVATION_SYMLINK, ""
    if mode != ACTIVATION_IN_PLACE:
        return ACTIVATION_SYMLINK, ""
    return ACTIVATION_IN_PLACE, venv


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
        in_place_installer=install_cached_into_venv,
    ) -> None:
        self._state = state
        self._layout = layout
        self._cfg = cfg
        self._installer = installer
        self._restarter = restarter
        self._in_place_installer = in_place_installer
        self._activation, self._in_place_venv = resolve_activation(cfg)
        self._lock = threading.Lock()
        self._pending: str | None = None
        self._blocked = False
        self._reason: str | None = None
        # The version the refusal is *about*. Usually the same as _pending, but
        # a failed install clears _pending so the next beat retries, and
        # "blocked, on nothing" tells an operator nothing at all.
        self._blocked_version: str | None = None
        self._observed_at: str | None = None

    # -- state transitions -----------------------------------------------
    # Every write to the reported state goes through one of these, so the
    # invariants (compare-and-set against the current target, and stamping
    # observed_at only on a real transition) hold in one place.

    def _clear_locked(self) -> None:
        self._pending = None
        self._blocked = False
        self._reason = None
        self._blocked_version = None
        self._observed_at = None

    def _record_refusal(
        self, target: str, reason: str, *, clear_pending: bool = False
    ) -> None:
        """Latch a refusal against `target`, keeping when it began.

        `observed_at` is stamped only when the refusal actually changes.
        Restamping it every fifteen-second beat -- which is what this used to
        do -- makes a host that has been refusing for half an hour
        indistinguishable from one that started refusing a moment ago, which
        is the exact question the report exists to answer.
        """
        with self._lock:
            if self._pending != target:
                # The target moved while we were running the checks (a smoke
                # test is a subprocess). Whoever moved it decided later than
                # we did, so their state stands.
                return
            unchanged = (
                self._blocked
                and self._reason == reason
                and self._blocked_version == target
            )
            if not unchanged:
                self._observed_at = datetime.now(timezone.utc).isoformat()
            self._blocked = True
            self._reason = reason
            self._blocked_version = target
            if clear_pending:
                self._pending = None

    def observe(self, heartbeat_body: dict) -> None:
        """React to what the hub said, downloading if we are behind."""
        target = str((heartbeat_body or {}).get("target_version") or "")
        if not target:
            # The hub has no target: either it never had one, or an operator
            # pulled a bad release in response to this very host refusing it.
            # Either way there is nothing left to refuse, and holding the old
            # refusal would report a version nobody is asking for until the
            # daemon restarts. Clearing _pending also stops maybe_activate
            # spawning a smoke-test subprocess on every beat forever.
            with self._lock:
                self._clear_locked()
            return
        active = self._layout.active_version()
        if active and compare_versions(target, active) <= 0:
            # Already there, or the hub is asking for something older, which
            # is never automatic.
            with self._lock:
                self._clear_locked()
            return
        with self._lock:
            if self._pending == target:
                # Already installed and waiting for quiesce. A host can wait
                # hours; re-downloading on every 15s heartbeat would be absurd.
                return
            self._pending = target
            if self._blocked_version != target:
                # A genuinely different version: whatever we refused before is
                # history. Retrying an install of the *same* version keeps the
                # existing refusal (and its timestamp) until we know better.
                self._blocked = False
                self._reason = None
                self._blocked_version = None
                self._observed_at = datetime.now(timezone.utc).isoformat()

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
            # _pending is cleared so the next beat retries the download; the
            # refusal keeps the version so the hub can say which one failed.
            self._record_refusal(target, "install_failed", clear_pending=True)
            return
        with self._lock:
            if self._pending == target and self._reason == "install_failed":
                # An earlier attempt at this same version failed; it just
                # succeeded, so the recorded failure is stale.
                self._blocked = False
                self._reason = None
                self._blocked_version = None
                self._observed_at = datetime.now(timezone.utc).isoformat()

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
            self._record_refusal(target, "smoke_test")
            return False
        if not is_quiescent(self._state):
            self._record_refusal(target, "not_quiescent")
            return False

        previous = self._layout.active_version() or ""
        # Marker first: if the flip or the restart goes wrong, the next start
        # needs to know what to fall back to.
        self._layout.write_marker(previous, target)

        if self._activation == ACTIVATION_IN_PLACE:
            if not self._in_place_installer(self._layout, target, self._in_place_venv):
                # Nothing is flipped and nothing is restarted: this host stays
                # on the version it is running. The marker is deliberately
                # left behind -- if the venv was mangled partway through, the
                # next start that fails to register repairs it by reinstalling
                # `previous`, and an ordinary start just clears it.
                log.error(
                    "could not install %s into %s; staying on %s",
                    target,
                    self._in_place_venv,
                    previous or "unknown",
                )
                self._record_refusal(target, "install_failed")
                return False

        # In in_place mode the symlink is no longer what the services exec --
        # they exec the venv we just installed into. It is flipped anyway
        # because it is the *record* of what is active, and active_version(),
        # the hub's target comparison and prune() all read it. A host that
        # skipped this would reinstall the same version on every heartbeat and
        # prune the wrong trees.
        self._layout.flip(target)
        with self._lock:
            if self._pending == target:
                self._clear_locked()
        log.info("activated %s (was %s); restarting", target, previous or "unknown")
        self._restarter()
        return True

    def status(self) -> dict:
        """Surfaced on the heartbeat so the hub can show a waiting host."""
        with self._lock:
            return {
                "pending_version": self._pending,
                "blocked_version": self._blocked_version,
                "update_blocked": self._blocked,
                "reason": self._reason,
                "observed_at": self._observed_at,
            }


def verify_after_restart(
    layout: RuntimeLayout,
    *,
    registered: bool,
    activation: str = ACTIVATION_SYMLINK,
    in_place_venv: str = "",
    in_place_installer=install_cached_into_venv,
) -> bool:
    """Undo a flip whose new version could not reach the hub.

    Without this, a bad release on a machine reachable only through an
    awkward SSH path is a physical trip. With it, the machine repairs itself
    in ninety seconds.

    The activation mode is passed in rather than read from global config, so
    the rollback path can be exercised in both modes without a config file.
    In in_place mode a flip undoes nothing on its own -- the venv still holds
    the version that could not register -- so rolling back means installing
    the previous version's cached wheel back over it.
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
        if activation == ACTIVATION_IN_PLACE and not in_place_installer(
            layout, previous, in_place_venv
        ):
            # The venv still holds `target`, so flipping the record back would
            # only make active_version() disagree with what is installed, and
            # every later decision reads that record.
            log.error(
                "could not reinstall %s into %s; the record still says %s",
                previous,
                in_place_venv,
                target,
            )
            layout.clear_marker()
            return False
        layout.flip(previous)
    else:
        # Flipping to "" would point current at nothing and leave the host
        # with no runtime at all, which is worse than staying on a version
        # that at least starts.
        log.error("no previous version recorded; leaving the symlink alone")
    layout.clear_marker()
    return False
