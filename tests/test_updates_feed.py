"""Release feed parsing and artifact installation, with no real network.

Everything here runs on a timer in production, so the governing rule is that
a bad or unreachable feed returns None rather than raising: a thread that dies
on a malformed release stops updating the fleet silently and forever.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from drover.config import default_config
from drover.server.runtime import RuntimeLayout
from drover.server.updates import (
    LOCK_NAME,
    MANIFEST_NAME,
    ReleaseArtifact,
    fetch_latest_release,
    install_version,
)


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


WHEEL = "drover-0.1.4-py3-none-any.whl"


def _feed(tag="v0.1.4", names=(WHEEL, MANIFEST_NAME, LOCK_NAME)):
    return {
        "tag_name": tag,
        "assets": [
            {"name": name, "browser_download_url": f"https://x.test/{name}"}
            for name in names
        ],
    }


def _manifest(wheel_digest="aa" * 32, lock_digest="bb" * 32):
    return f"{wheel_digest}  {WHEEL}\n{lock_digest}  {LOCK_NAME}\n".encode()


def _opener(feed=None, manifest=None):
    feed = _feed() if feed is None else feed
    manifest = _manifest() if manifest is None else manifest

    def open_url(url, timeout=0):
        if url.endswith("/releases/latest"):
            return _Response(json.dumps(feed).encode())
        return _Response(manifest)

    return open_url


# --- config ------------------------------------------------------------------


def test_config_has_update_defaults():
    cfg = default_config()
    assert cfg.update_enabled is True
    assert cfg.update_check_interval_hours == 6
    assert cfg.update_pinned_version == ""
    assert cfg.update_quiesce_timeout_hours == 6
    assert cfg.update_keep_versions == 2
    assert cfg.update_repo == "arniesaha/drover"


# --- feed ---------------------------------------------------------------------


def test_fetch_latest_parses_the_asset_urls():
    artifact = fetch_latest_release("arniesaha/drover", opener=_opener())
    assert artifact.version == "0.1.4", "the leading v is stripped"
    assert artifact.wheel_url.endswith(WHEEL)
    assert artifact.wheel_sha256 == "aa" * 32
    assert artifact.lock_sha256 == "bb" * 32


def test_a_release_with_no_checksum_manifest_is_ignored():
    """v0.1.0 is exactly this shape: a tag with no artifacts."""
    feed = _feed(names=(WHEEL,))
    assert fetch_latest_release("arniesaha/drover", opener=_opener(feed)) is None


def test_a_release_with_no_wheel_is_ignored():
    feed = _feed(names=(MANIFEST_NAME, LOCK_NAME))
    assert fetch_latest_release("arniesaha/drover", opener=_opener(feed)) is None


def test_a_release_with_no_lockfile_is_ignored():
    feed = _feed(names=(WHEEL, MANIFEST_NAME))
    assert fetch_latest_release("arniesaha/drover", opener=_opener(feed)) is None


def test_an_empty_release_list_is_ignored():
    assert fetch_latest_release("arniesaha/drover", opener=_opener({})) is None


def test_an_unreachable_feed_is_none_not_an_exception():
    def boom(url, timeout=0):
        raise OSError("no network")

    assert fetch_latest_release("arniesaha/drover", opener=boom) is None


def test_malformed_feed_json_is_none():
    def bad(url, timeout=0):
        return _Response(b"{not json")

    assert fetch_latest_release("arniesaha/drover", opener=bad) is None


def test_a_manifest_entry_that_is_not_hex_is_refused():
    manifest = f"notahexdigest  {WHEEL}\ncc{'c' * 62}  {LOCK_NAME}\n".encode()
    assert (
        fetch_latest_release("arniesaha/drover", opener=_opener(None, manifest)) is None
    )


def test_a_manifest_missing_the_wheel_entry_is_refused():
    manifest = f"{'bb' * 32}  {LOCK_NAME}\n".encode()
    assert (
        fetch_latest_release("arniesaha/drover", opener=_opener(None, manifest)) is None
    )


def test_binary_mode_manifest_entries_are_accepted():
    manifest = f"{'aa' * 32}  *{WHEEL}\n{'bb' * 32}  ./{LOCK_NAME}\n".encode()
    artifact = fetch_latest_release("arniesaha/drover", opener=_opener(None, manifest))
    assert artifact is not None
    assert artifact.wheel_sha256 == "aa" * 32


# --- install ------------------------------------------------------------------


def _artifact(payload=b"wheel-bytes", lock=b"lock-bytes"):
    import hashlib

    return (
        ReleaseArtifact(
            version="0.1.4",
            wheel_url=f"https://x.test/{WHEEL}",
            wheel_sha256=hashlib.sha256(payload).hexdigest(),
            lock_url=f"https://x.test/{LOCK_NAME}",
            lock_sha256=hashlib.sha256(lock).hexdigest(),
        ),
        payload,
        lock,
    )


def _download_opener(payload, lock):
    def open_url(url, timeout=0):
        return _Response(lock if url.endswith(LOCK_NAME) else payload)

    return open_url


def test_install_refuses_a_wheel_whose_digest_does_not_match(tmp_path):
    artifact, payload, lock = _artifact()
    tampered = _download_opener(b"tampered", lock)
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        raise AssertionError("must not run uv on an unverified artifact")

    layout = RuntimeLayout(tmp_path)
    assert install_version(layout, artifact, runner=runner, opener=tampered) is False
    assert calls == []


def test_install_refuses_a_lockfile_whose_digest_does_not_match(tmp_path):
    artifact, payload, _ = _artifact()
    tampered = _download_opener(payload, b"tampered")

    def runner(cmd, **kwargs):
        raise AssertionError("must not run uv on an unverified artifact")

    layout = RuntimeLayout(tmp_path)
    assert install_version(layout, artifact, runner=runner, opener=tampered) is False


def test_install_runs_uv_with_require_hashes(tmp_path):
    artifact, payload, lock = _artifact()
    calls = []

    class _Ok:
        returncode = 0

    def runner(cmd, **kwargs):
        calls.append(cmd)
        # Fake what uv would leave behind so the smoke test can pass.
        if cmd[:2] == ["uv", "venv"]:
            binary = RuntimeLayout(tmp_path).executable("drover-server", "0.1.4")
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
        return _Ok()

    layout = RuntimeLayout(tmp_path)
    assert install_version(
        layout, artifact, runner=runner, opener=_download_opener(payload, lock)
    )
    flat = [" ".join(cmd) for cmd in calls]
    assert any("uv venv" in line for line in flat)
    assert any("--require-hashes" in line for line in flat)
    assert any("--no-deps" in line for line in flat)


def test_install_does_not_flip_the_symlink(tmp_path):
    """Installing and activating are separate: a host installs as soon as it
    hears about a version, and activates only once it has no live work."""
    artifact, payload, lock = _artifact()

    class _Ok:
        returncode = 0

    def runner(cmd, **kwargs):
        if cmd[:2] == ["uv", "venv"]:
            binary = RuntimeLayout(tmp_path).executable("drover-server", "0.1.4")
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
        return _Ok()

    layout = RuntimeLayout(tmp_path)
    install_version(
        layout, artifact, runner=runner, opener=_download_opener(payload, lock)
    )
    assert layout.active_version() is None


def test_install_fails_when_uv_fails(tmp_path):
    artifact, payload, lock = _artifact()

    class _Bad:
        returncode = 1

    layout = RuntimeLayout(tmp_path)
    assert (
        install_version(
            layout,
            artifact,
            runner=lambda cmd, **kw: _Bad(),
            opener=_download_opener(payload, lock),
        )
        is False
    )


def test_install_fails_when_the_result_cannot_smoke_test(tmp_path):
    """uv succeeding is not the same as the version being runnable."""
    artifact, payload, lock = _artifact()

    class _Ok:
        returncode = 0

    layout = RuntimeLayout(tmp_path)
    assert (
        install_version(
            layout,
            artifact,
            runner=lambda cmd, **kw: _Ok(),
            opener=_download_opener(payload, lock),
        )
        is False
    ), "no binary was produced, so this must not report success"


def test_a_failed_install_does_not_poison_the_next_attempt(tmp_path):
    """A failure must not leave the version permanently uninstallable.

    Observed live while testing the smoke gate: the first attempt at a bad
    version failed at the gate, as designed. Every attempt after it failed
    earlier and for a different reason —

        ERROR install step returned nonzero: uv venv .../runtime/9.9.9

    because the failed install left its directory behind and `uv venv` refuses
    a directory that already exists. Two consequences, and the second is the
    serious one. The real cause is reported once and then buried under a
    misleading error. And any transient failure -- a network blip, a full
    disk, an interrupted uv -- strands that version on that host forever,
    because the retry can never again reach the code that would have
    succeeded. Recovery means someone opening a shell to delete a directory,
    on the machine the rollback design exists to avoid visiting.
    """
    artifact, payload, lock = _artifact()
    layout = RuntimeLayout(tmp_path)
    attempts = []

    class _Result:
        def __init__(self, returncode):
            self.returncode = returncode

    def runner(cmd, **kwargs):
        attempts.append(cmd)
        if cmd[:2] == ["uv", "venv"]:
            target = Path(cmd[2])
            if target.exists():
                # What uv actually does: refuses rather than clobbering.
                return _Result(1)
            target.mkdir(parents=True)
            # First attempt only: leave the tree half-built, the way an
            # install killed partway through would.
            if len(attempts) <= 1:
                return _Result(1)
            binary = layout.executable("drover-server", artifact.version)
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
        return _Result(0)

    opener = _download_opener(payload, lock)
    assert install_version(layout, artifact, runner=runner, opener=opener) is False

    # The retry must get a clean run at it rather than tripping over the
    # wreckage of the first.
    assert install_version(layout, artifact, runner=runner, opener=opener) is True
    assert layout.smoke_test(artifact.version)


def test_a_failed_install_does_not_count_as_an_installed_version(tmp_path):
    """A half-built tree must not masquerade as a version that is there.

    `installed_versions` lists directories, so wreckage left behind reads as
    installed — which means `prune(keep=2)` can retain it and drop a good
    older version instead. The newest name wins the sort, so the broken one is
    exactly the one that survives.
    """
    artifact, payload, lock = _artifact()
    layout = RuntimeLayout(tmp_path)

    class _Result:
        def __init__(self, returncode):
            self.returncode = returncode

    def runner(cmd, **kwargs):
        if cmd[:2] == ["uv", "venv"]:
            Path(cmd[2]).mkdir(parents=True, exist_ok=True)
            return _Result(1)
        return _Result(0)

    assert (
        install_version(
            layout, artifact, runner=runner, opener=_download_opener(payload, lock)
        )
        is False
    )
    assert artifact.version not in layout.installed_versions()


# --- cached artifacts ---------------------------------------------------------


def _installing_runner(tmp_path, version="0.1.4"):
    """A uv stand-in that leaves behind what a real install would."""

    class _Ok:
        returncode = 0

    def runner(cmd, **kwargs):
        if cmd[:2] == ["uv", "venv"]:
            binary = RuntimeLayout(tmp_path).executable("drover-server", version)
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
        return _Ok()

    return runner


def test_install_caches_the_verified_wheel_and_lock_with_the_version(tmp_path):
    """Each version carries its own rollback material.

    An in-place host activates by installing a wheel into the venv it already
    has, and it does that when it finally goes idle -- which may be hours after
    the download, and must not reach for the network again. Keeping the
    artifacts inside the version tree also means the existing keep=2 prune
    policy retains the previous version's wheel for free, instead of a
    separate cache with a lifetime of its own to get wrong.
    """
    artifact, payload, lock = _artifact()
    layout = RuntimeLayout(tmp_path)

    assert install_version(
        layout,
        artifact,
        runner=_installing_runner(tmp_path),
        opener=_download_opener(payload, lock),
    )

    cached = layout.cached_artifact("0.1.4")
    assert cached is not None
    assert cached.wheel.name == WHEEL
    assert cached.wheel.read_bytes() == payload, "the verified bytes, not a re-fetch"
    assert cached.lock.read_bytes() == lock


def test_a_pruned_version_takes_its_cached_artifacts_with_it(tmp_path):
    """The cache has no lifetime of its own; it is part of the version tree."""
    artifact, payload, lock = _artifact()
    layout = RuntimeLayout(tmp_path)
    install_version(
        layout,
        artifact,
        runner=_installing_runner(tmp_path),
        opener=_download_opener(payload, lock),
    )
    artifact_dir = layout.artifact_dir("0.1.4")
    assert artifact_dir.is_dir()

    for version in ("0.1.5", "0.1.6"):
        binary = layout.executable("drover-server", version)
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    layout.flip("0.1.6")

    assert "0.1.4" in layout.prune(keep=2)
    assert not artifact_dir.exists()
    assert layout.cached_artifact("0.1.4") is None


def test_cached_artifacts_do_not_read_as_an_installed_version(tmp_path):
    """`installed_versions` lists directories; the cache must not join them."""
    artifact, payload, lock = _artifact()
    layout = RuntimeLayout(tmp_path)
    install_version(
        layout,
        artifact,
        runner=_installing_runner(tmp_path),
        opener=_download_opener(payload, lock),
    )
    assert layout.installed_versions() == ["0.1.4"]


def test_a_version_with_no_cache_reads_as_absent_rather_than_raising(tmp_path):
    layout = RuntimeLayout(tmp_path)
    assert layout.cached_artifact("9.9.9") is None
