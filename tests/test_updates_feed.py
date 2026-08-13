"""Release feed parsing and artifact installation, with no real network.

Everything here runs on a timer in production, so the governing rule is that
a bad or unreachable feed returns None rather than raising: a thread that dies
on a malformed release stops updating the fleet silently and forever.
"""

from __future__ import annotations

import io
import json

from drover.config import default_config
from drover.server.updates import (
    LOCK_NAME,
    MANIFEST_NAME,
    ReleaseArtifact,
    fetch_latest_release,
    install_version,
)
from drover.server.runtime import RuntimeLayout


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
