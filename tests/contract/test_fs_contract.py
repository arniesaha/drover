"""Filesystem endpoint wire contracts, through the launched daemon."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def call(daemon, path, *, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        daemon.url + path,
        data=data,
        headers={
            "Authorization": f"Bearer {daemon.token}",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_fs_requires_correct_host_and_reports_error_codes(daemon):
    root = daemon.root / "home" / "project"
    root.mkdir()
    assert call(daemon, "/fs/list?host_id=other&path=")[0:2] == (
        403,
        {"error": "host mismatch"},
    )
    assert call(daemon, "/fs/list?host_id=contract-host&path=&limit=bogus") == (
        400,
        {"error": "invalid_limit"},
    )
    status, body = call(
        daemon,
        "/fs/create",
        payload={"host_id": "contract-host", "path": str(root), "name": "new-folder"},
    )
    assert status == 201
    assert body == {"name": "new-folder", "path": str(root / "new-folder")}
    assert call(
        daemon,
        "/fs/create",
        payload={"host_id": "contract-host", "path": str(root), "name": "../escape"},
    ) == (400, {"error": "invalid_name"})
    assert call(
        daemon,
        "/fs/create",
        payload={"host_id": "contract-host", "path": str(root), "name": "new-folder"},
    ) == (409, {"error": "folder_exists"})


def test_fs_list_and_exists_shapes(daemon):
    root = daemon.root / "home" / "workspace"
    (root / "one").mkdir(parents=True)
    (root / "two").mkdir()
    status, listing = call(
        daemon, f"/fs/list?host_id=contract-host&path={root}&limit=1"
    )
    assert status == 200
    assert set(listing) == {
        "roots",
        "path",
        "parent",
        "entries",
        "truncated",
        "next_cursor",
    }
    assert listing["truncated"] is True
    assert isinstance(listing["next_cursor"], str)
    status, exists = call(
        daemon, "/fs/exists", payload={"paths": [str(root), str(root / "missing")]}
    )
    assert (status, exists) == (
        200,
        {"exists": {str(root): True, str(root / "missing"): False}},
    )
