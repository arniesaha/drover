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
        if not _run_steps(steps, runner=runner):
            return False

        # Cached only now, not straight after verification: `uv venv` refuses a
        # target directory that already exists, so writing the cache inside it
        # any earlier would make every install fail on its own cache. These are
        # still the verified bytes; nothing is fetched twice.
        layout.cache_artifact(artifact.version, wheel, lock)

    # uv succeeding is not the same as the result being runnable.
    if not layout.smoke_test(artifact.version):
        log.error("%s installed but failed its smoke test", artifact.version)
        return False
    return True


def _run_steps(steps: list[list[str]], *, runner) -> bool:
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
    return True


def venv_interpreter(venv: Path | str) -> Path:
    """The interpreter an in-place install writes through.

    One definition, because two things depend on agreeing about it: the
    install refuses a venv without this path, and the startup check warns
    about one. A second copy of the layout would let them drift into
    disagreeing about which venvs are usable.
    """
    return Path(venv) / "bin" / "python"


def install_cached_into_venv(
    layout: RuntimeLayout,
    version: str,
    venv: Path | str,
    *,
    runner=subprocess.run,
) -> bool:
    """Install a version's cached wheel INTO an existing venv. Returns success.

    This is the opposite of everything else in this module, and deliberately
    so. It exists for a host that cannot exec a new venv at all: the macOS hub
    holds a TCC grant keyed to the executable, so a freshly created venv on the
    external volume dies at interpreter startup with EPERM reading its own
    ``pyvenv.cfg``. Keeping the executable path and the interpreter exactly
    where they are is the only activation that survives, which means the new
    version has to be installed over the old one.

    Nothing is downloaded here. The wheel and lock were verified against the
    release manifest at install time and cached with the version; if they are
    missing this refuses rather than reaching for the network, because
    activation runs when the host has finally gone idle and that is the worst
    possible moment to start something that can hang.
    """
    cached = layout.cached_artifact(version)
    if cached is None:
        log.error("%s has no cached artifact; refusing to install it in place", version)
        return False
    python = venv_interpreter(venv)
    if not python.exists():
        # Guessing an interpreter is how the wrong venv gets overwritten.
        log.error("%s is not an interpreter we can install into", python)
        return False

    steps = [
        # Mirrors _install_into exactly, minus the `uv venv`: same hash-pinned
        # dependencies, same already-verified wheel with --no-deps.
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            "-r",
            str(cached.lock),
        ],
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            str(cached.wheel),
        ],
    ]
    if not _run_steps(steps, runner=runner):
        log.error("could not install %s into %s", version, venv)
        return False
    log.info("installed %s into %s in place", version, venv)
    return True
