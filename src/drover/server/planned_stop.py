"""Bounded recovery for a hub intentionally stopped by an operator procedure."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.error import HTTPError
from urllib.request import urlopen

MARKER_NAME = "planned-stop.json"
MAX_PLANNED_STOP_SECONDS = 2 * 60 * 60
RECOVERY_TIMEOUT_SECONDS = 30.0
RECOVERY_POLL_SECONDS = 1.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


class PlannedStopStore:
    """A locked, atomically-written marker in the configured Drover state dir."""

    def __init__(self, state_dir: Path | str):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / MARKER_NAME
        self._lock_path = self.state_dir / ".planned-stop.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _write_unlocked(self, marker: dict) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.state_dir, delete=False
        ) as stream:
            staged = Path(stream.name)
            json.dump(marker, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            staged.chmod(0o600)
            os.replace(staged, self.path)
        finally:
            staged.unlink(missing_ok=True)

    def record(
        self,
        *,
        step: str,
        actor: str,
        expires_in_seconds: int = MAX_PLANNED_STOP_SECONDS,
        now: datetime | None = None,
    ) -> dict:
        if not step.strip() or not actor.strip():
            raise ValueError("planned stop requires a step and actor")
        if not 0 < expires_in_seconds <= MAX_PLANNED_STOP_SECONDS:
            raise ValueError("planned stop expiry must be between 1 second and 2 hours")
        created_at = now or _utc_now()
        marker = {
            "step": step.strip(),
            "actor": actor.strip(),
            "created_at": _timestamp(created_at),
            "expires_at": _timestamp(
                created_at + timedelta(seconds=expires_in_seconds)
            ),
            "attempts": 0,
        }
        with self._locked():
            self._write_unlocked(marker)
        return marker

    def read(self) -> dict | None:
        # Markers are atomically replaced, so an unlocked read can only see a
        # complete old or new marker. Keeping this read side-effect free is
        # important: an unplanned outage must not create recovery state.
        return self._read_unlocked()

    def clear(self) -> None:
        if not self.path.exists():
            return
        with self._locked():
            self.path.unlink(missing_ok=True)

    def claim_recovery(self, *, now: datetime | None = None) -> dict | None:
        """Claim the sole restart attempt, atomically with its marker update."""
        current = now or _utc_now()
        with self._locked():
            marker = self._read_unlocked()
            if not valid_marker(marker, now=current) or marker["attempts"] != 0:
                return None
            marker = {**marker, "attempts": 1}
            self._write_unlocked(marker)
            return marker


def valid_marker(marker: object, *, now: datetime | None = None) -> bool:
    """Accept only a complete, bounded, still-live marker."""
    if not isinstance(marker, dict):
        return False
    if not all(
        isinstance(marker.get(field), str) and marker[field].strip()
        for field in ("step", "actor")
    ):
        return False
    if type(marker.get("attempts")) is not int or marker["attempts"] < 0:
        return False
    created_at = _parse_timestamp(marker.get("created_at"))
    expires_at = _parse_timestamp(marker.get("expires_at"))
    if created_at is None or expires_at is None:
        return False
    current = now or _utc_now()
    duration = (expires_at - created_at).total_seconds()
    if not 0 < duration <= MAX_PLANNED_STOP_SECONDS:
        return False
    return created_at <= current and expires_at > current


def readyz(url: str) -> bool:
    """Readiness is deliberately the only liveness authority used here."""
    try:
        with urlopen(url, timeout=2) as response:
            return response.status == 200
    except (HTTPError, OSError):
        return False


def restart_hub() -> str | None:
    """Ask the platform manager for one restart; never manipulate the process."""
    if sys.platform == "darwin":
        argv = [
            "/bin/launchctl",
            "kickstart",
            "-k",
            f"gui/{os.getuid()}/com.drover.server",
        ]
    else:
        argv = ["systemctl", "--user", "restart", "drover-server.service"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        return f"service restart failed: {exc}"
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        suffix = detail[0] if detail else f"exit {result.returncode}"
        return f"service restart failed: {suffix}"
    return None


def startup_error(log_path: Path, offset: int = 0) -> str:
    """Return the first error line written after the restart was requested."""
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(offset)
            lines = stream.readlines()
    except OSError:
        return "startup error unavailable"
    for line in lines:
        text = " ".join(line.strip().split())
        if text and (
            "error" in text.lower()
            or "exception" in text.lower()
            or "lake_verification_required" in text
        ):
            return text[:500]
    return "startup error unavailable"


def _log_offset(log_path: Path) -> int:
    try:
        return log_path.stat().st_size
    except OSError:
        return 0


def recover_planned_stop(
    store: PlannedStopStore,
    *,
    ready_url: str,
    log_path: Path,
    dry_run: bool = False,
    probe: Callable[[str], bool] = readyz,
    restart: Callable[[], str | None] = restart_hub,
    read_startup_error: Callable[[Path, int], str] = startup_error,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, str]:
    """Return a one-line check-in result, making at most one service request."""
    if probe(ready_url):
        store.clear()
        return 0, "healthy"

    marker = store.read()
    if not valid_marker(marker) or marker["attempts"] >= 1:
        return 1, "unplanned outage: human decision required"
    if dry_run:
        return 0, "dry-run: would restart planned stop"

    if store.claim_recovery() is None:
        return 1, "unplanned outage: human decision required"

    offset = _log_offset(log_path)
    restart_error = restart()
    if restart_error:
        return 1, f"failed: {restart_error}"
    deadline = monotonic() + RECOVERY_TIMEOUT_SECONDS
    while monotonic() <= deadline:
        if probe(ready_url):
            store.clear()
            return 0, "recovered"
        sleep(RECOVERY_POLL_SECONDS)
    return 1, f"failed: {read_startup_error(log_path, offset)}"
