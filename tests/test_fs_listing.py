"""Directory browser policy and daemon HTTP contracts."""

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlencode

import pytest
from test_fs_completion import TOKEN, base_url  # reuse isolated daemon fixture


def listing(base_url, path="", *, token=TOKEN, host_id="test-host", query=""):
    params = urlencode({"path": path, "host_id": host_id, "filter": query})
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(f"{base_url}/fs/list?{params}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as response:
        return response.code, json.load(response)


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("DROVER_FOLDER_ROOTS", str(root))
    return root


def test_entry_lists_configured_roots_only(base_url, root):
    status, body = listing(base_url)
    assert status == 200
    assert body["roots"] == [{"name": "root", "path": str(root)}]
    assert body["path"] is None
    assert body["entries"] == []


def test_allowed_root_directories_only_and_git_metadata(base_url, root):
    (root / "code" / ".git").mkdir(parents=True)
    (root / "worktree").mkdir()
    (root / "worktree" / ".git").write_text("contents must never be read")
    (root / "empty").mkdir()
    (root / "notes").write_text("private contents")
    (root / ".secret").mkdir()
    status, body = listing(base_url, str(root))
    assert status == 200
    assert body["parent"] is None
    assert [entry["name"] for entry in body["entries"]] == ["code", "empty", "worktree"]
    assert all(entry["is_dir"] and not entry["hidden"] for entry in body["entries"])
    assert [entry["is_git_repo"] for entry in body["entries"]] == [True, False, True]
    assert body["truncated"] is False
    _, child = listing(base_url, str(root / "empty"))
    assert child["parent"] == str(root)
    assert child["entries"] == []


@pytest.mark.parametrize("suffix", ["/..", "/child/../", "/../root"])
def test_traversal_refused(base_url, root, suffix):
    status, body = listing(base_url, str(root) + suffix)
    assert status == 403
    assert body["error"] == "permission_denied"


def test_outside_and_sibling_prefix_refused(base_url, root):
    sibling = root.parent / "root-extra"
    sibling.mkdir()
    for path in [root.parent, sibling]:
        assert listing(base_url, str(path))[0] == 403


def test_symlinks_refused_even_inside_root(base_url, root):
    (root / "safe").mkdir()
    (root / "escape").symlink_to(root.parent, target_is_directory=True)
    (root / "alias").symlink_to(root / "safe", target_is_directory=True)
    for name in ["escape", "alias"]:
        assert listing(base_url, str(root / name))[0] == 403
    _, body = listing(base_url, str(root))
    assert [entry["name"] for entry in body["entries"]] == ["safe"]


def test_non_directory_and_missing_refused(base_url, root):
    (root / "file").write_text("private")
    assert listing(base_url, str(root / "file")) == (400, {"error": "not_directory"})
    assert listing(base_url, str(root / "missing")) == (400, {"error": "not_found"})


@pytest.mark.parametrize("path", ["relative", "~/../", "\x00invalid"])
def test_invalid_paths_refused(base_url, root, path):
    assert listing(base_url, path)[0] in {400, 403}


def test_hidden_folder_cannot_be_opened_directly(base_url, root):
    (root / ".hidden").mkdir()
    assert listing(base_url, str(root / ".hidden"))[0] == 403


def test_large_directory_capped_and_filter_before_cap(base_url, root):
    for index in range(110):
        (root / f"folder{index:03}").mkdir()
    _, body = listing(base_url, str(root))
    assert len(body["entries"]) == 100
    assert body["truncated"] is True
    _, filtered = listing(base_url, str(root), query="DER109")
    assert [entry["name"] for entry in filtered["entries"]] == ["folder109"]
    assert filtered["truncated"] is False


def test_scan_budget_is_bounded(base_url, root, monkeypatch):
    from drover.server.harness import folders

    monkeypatch.setattr(folders, "MAX_SCANNED", 3)
    for index in range(5):
        (root / f"folder{index}").mkdir()
    _, body = listing(base_url, str(root))
    assert len(body["entries"]) <= 3
    assert body["truncated"] is True


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root permissions"
)
def test_permission_denied(base_url, root):
    locked = root / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert listing(base_url, str(locked)) == (403, {"error": "permission_denied"})
    finally:
        locked.chmod(0o700)


def test_listing_requires_authentication_and_host_binding(base_url, root):
    for token in [None, "invalid"]:
        assert listing(base_url, token=token)[0] == 401
    for host_id in ["other-host", ""]:
        assert listing(base_url, host_id=host_id)[0] == 403


def test_default_root_is_home(base_url, root, monkeypatch):
    monkeypatch.delenv("DROVER_FOLDER_ROOTS")
    monkeypatch.setenv("HOME", str(root))
    assert listing(base_url)[1]["roots"][0]["path"] == str(root)


def test_empty_root_configuration_fails_closed(base_url, root, monkeypatch):
    monkeypatch.setenv("DROVER_FOLDER_ROOTS", "")
    assert listing(base_url)[1]["roots"] == []
    assert listing(base_url, str(root))[0] == 403


def test_tokenless_daemon_refuses_listing(base_url, root, monkeypatch):
    from drover.server.harness.daemon import HarnessRequestHandler

    monkeypatch.setattr(HarnessRequestHandler, "_authorized", lambda self: True)
    # Test the additional fail-closed guard using the HTTP handler's state.
    original = HarnessRequestHandler._fs_list

    def without_token(self, query):
        monkeypatch.setattr(self.server.state, "api_token", "")
        original(self, query)

    monkeypatch.setattr(HarnessRequestHandler, "_fs_list", without_token)
    assert listing(base_url)[0] == 401


def test_ancestor_symlink_is_refused(base_url, root):
    (root / "real" / "child").mkdir(parents=True)
    (root / "alias").symlink_to(root / "real", target_is_directory=True)
    assert listing(base_url, str(root / "alias" / "child"))[0] == 403


def test_symlink_swap_during_open_never_lists_outside(base_url, root, monkeypatch):
    from drover.server.harness import folders

    (root / "target").mkdir()
    outside = root.parent / "outside"
    (outside / "private-folder").mkdir(parents=True)
    original_open = folders.os.open
    swapped = False

    def swap_then_open(path, flags, **kwargs):
        nonlocal swapped
        if path == "target" and not swapped:
            swapped = True
            (root / "target").rmdir()
            (root / "target").symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, **kwargs)

    monkeypatch.setattr(folders.os, "open", swap_then_open)
    status, body = listing(base_url, str(root / "target"))
    assert status == 403
    assert "private-folder" not in json.dumps(body)


def test_git_marker_symlink_does_not_grant_repo_badge(base_url, root):
    (root / "project").mkdir()
    marker = root.parent / "git-marker"
    marker.write_text("private")
    (root / "project" / ".git").symlink_to(marker)
    _, body = listing(base_url, str(root))
    assert body["entries"][0]["is_git_repo"] is False


def test_replacing_allowed_root_with_symlink_does_not_expand_authority(base_url, root):
    outside = root.parent / "outside"
    (outside / "private-folder").mkdir(parents=True)
    root.rmdir()
    root.symlink_to(outside, target_is_directory=True)
    _, entry = listing(base_url)
    assert str(outside) not in json.dumps(entry)
    assert listing(base_url, str(outside))[0] == 403
    assert listing(base_url, str(root))[0] == 403


def test_child_deleted_during_scan_does_not_fail_parent(base_url, root, monkeypatch):
    from drover.server.harness import folders

    (root / "target").mkdir()
    original_open = folders.os.open
    deleted = False

    def delete_then_open(path, flags, **kwargs):
        nonlocal deleted
        if path == "target" and not deleted:
            deleted = True
            (root / "target").rmdir()
        return original_open(path, flags, **kwargs)

    monkeypatch.setattr(folders.os, "open", delete_then_open)
    status, body = listing(base_url, str(root))
    assert status == 200
    assert body["entries"] == []
