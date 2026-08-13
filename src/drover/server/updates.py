"""Read the release feed and install a verified version beside the current one.

Nothing here flips a symlink. Installing and activating are deliberately
separate: a host installs as soon as it learns a version exists, and activates
only once it has no live work, which can be hours later.

Every function that runs on a timer returns a falsy value rather than raising.
A thread that dies on one malformed release would stop updating the fleet
silently and permanently, which is worse than skipping a cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from drover.server.runtime import RuntimeLayout

log = logging.getLogger("drover.update")

WHEEL_RE = re.compile(r"^drover-.*-py3-none-any\.whl$")
LOCK_NAME = "requirements.lock.txt"
MANIFEST_NAME = "SHA256SUMS.txt"
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
FEED_TIMEOUT_SECONDS = 15
DOWNLOAD_TIMEOUT_SECONDS = 120
INSTALL_TIMEOUT_SECONDS = 900


@dataclass(frozen=True)
class ReleaseArtifact:
    version: str
    wheel_url: str
    wheel_sha256: str
    lock_url: str
    lock_sha256: str


def _parse_manifest(text: str) -> dict[str, str]:
    """Digest by filename, accepting the spellings GNU and BSD tools emit."""
    digests: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts[0].lower(), parts[1].lstrip("*./")
        if _HEX_RE.match(digest):
            digests[name] = digest
    return digests


def fetch_latest_release(repo: str, *, opener=urlopen) -> ReleaseArtifact | None:
    """Latest release, or None for any failure.

    A release missing any of the three artifacts is ignored rather than
    partially trusted. That is not hypothetical: v0.1.0 was tagged before the
    release workflow existed and carries no assets at all, so this is the
    path that keeps such a tag from looking installable.
    """
    try:
        with opener(
            f"https://api.github.com/repos/{repo}/releases/latest",
            timeout=FEED_TIMEOUT_SECONDS,
        ) as response:
            feed = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - a timer must not die on a bad feed
        log.debug("release feed unreachable or unparseable", exc_info=True)
        return None

    if not isinstance(feed, dict):
        return None
    assets = {
        asset.get("name"): asset.get("browser_download_url")
        for asset in feed.get("assets") or []
        if isinstance(asset, dict)
    }
    wheel_name = next((name for name in assets if WHEEL_RE.match(name or "")), None)
    if wheel_name is None or LOCK_NAME not in assets or MANIFEST_NAME not in assets:
        log.debug("release %s has no complete artifact set", feed.get("tag_name"))
        return None

    try:
        with opener(assets[MANIFEST_NAME], timeout=FEED_TIMEOUT_SECONDS) as response:
            digests = _parse_manifest(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        log.debug("checksum manifest unreachable", exc_info=True)
        return None
    if wheel_name not in digests or LOCK_NAME not in digests:
        log.warning("release manifest is missing an entry; ignoring this release")
        return None

    return ReleaseArtifact(
        version=str(feed.get("tag_name") or "").lstrip("v"),
        wheel_url=assets[wheel_name],
        wheel_sha256=digests[wheel_name],
        lock_url=assets[LOCK_NAME],
        lock_sha256=digests[LOCK_NAME],
    )


def _download_verified(url: str, expected: str, destination: Path, *, opener) -> bool:
    try:
        with opener(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            payload = response.read()
    except Exception:  # noqa: BLE001
        log.warning("could not download %s", url)
        return False
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        log.error("checksum mismatch for %s; refusing to install", url)
        return False
    destination.write_bytes(payload)
    return True


def install_version(
    layout: RuntimeLayout,
    artifact: ReleaseArtifact,
    *,
    runner=subprocess.run,
    opener=urlopen,
) -> bool:
    """Install into ``runtime/<version>``. Does not flip. Returns success.

    Already-installed and smoke-clean is treated as success, so a host that
    installed a version and then waited hours for quiesce does not re-download
    it on every heartbeat.

    Anything left of a failed attempt is cleared, both before starting and
    after failing. `uv venv` refuses a directory that already exists, so a
    half-built tree left behind makes every retry fail on the wreckage instead
    of on the real problem — and reports the wrong cause while doing it. Worse,
    that tree still counts as an installed version, so it can be kept by
    `prune` in place of a good one.
    """
    target = layout.version_dir(artifact.version)
    if target.exists() and layout.smoke_test(artifact.version):
        return True

    _discard(target)
    if _install_into(layout, artifact, runner=runner, opener=opener):
        return True
    _discard(target)
    return False


def _discard(target: Path) -> None:
    """Remove a version tree, best effort. A failure here is not fatal."""
    if not target.exists():
        return
    try:
        shutil.rmtree(target)
    except OSError:
        log.warning("could not clear %s; a retry may fail on it", target)


def _install_into(
    layout: RuntimeLayout,
    artifact: ReleaseArtifact,
    *,
    runner,
    opener,
) -> bool:
    target = layout.version_dir(artifact.version)
    with tempfile.TemporaryDirectory() as work_dir:
        work = Path(work_dir)
        wheel = work / Path(artifact.wheel_url).name
        lock = work / LOCK_NAME
        # Verify before uv ever sees these paths: an unverified artifact is
        # an artifact we do not run, and uv would happily install one.
        if not _download_verified(
            artifact.wheel_url, artifact.wheel_sha256, wheel, opener=opener
        ):
            return False
        if not _download_verified(
            artifact.lock_url, artifact.lock_sha256, lock, opener=opener
        ):
            return False

        python = target / "bin" / "python"
        steps = [
            ["uv", "venv", str(target)],
            # Dependencies are hash-pinned. The wheel goes in --no-deps
            # because it has already been verified against the manifest.
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--require-hashes",
                "-r",
                str(lock),
            ],
            ["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
        ]
        for command in steps:
            try:
                result = runner(
                    command,
                    capture_output=True,
                    timeout=INSTALL_TIMEOUT_SECONDS,
                    check=False,
                )
            except Exception:  # noqa: BLE001
                log.exception("install step failed: %s", " ".join(command))
                return False
            if getattr(result, "returncode", 1) != 0:
                log.error("install step returned nonzero: %s", " ".join(command))
                return False

    # uv succeeding is not the same as the result being runnable.
    if not layout.smoke_test(artifact.version):
        log.error("%s installed but failed its smoke test", artifact.version)
        return False
    return True
